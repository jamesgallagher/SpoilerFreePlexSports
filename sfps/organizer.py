"""Organizer stage: place the media file, artwork, and sidecar in the library.

Layout (design.md §3.4, Plex TV library + Local Media Assets):

    /library/<League>/Season <YYYY>/<Home> vs <Away> <date>[ (Highlights)]/
        <League> - <date> - <Home> vs <Away>[ (Highlights)].<ext>
        <League> - <date> - <Home> vs <Away>[ (Highlights)].jpg   # episode thumb
        poster.jpg / background.jpg                               # if available
        game.json                                                 # spoiler-free sidecar

Unmatched files go to /library/Unknown Events/<name>/ with a placeholder
thumb — which still pre-empts Plex's score-revealing frame-grab.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from sfps import artwork, matcher
from sfps.config import Config
from sfps.models import GameGuess, OrganizeResult, SafeEvent

log = logging.getLogger(__name__)

UNKNOWN_DIR_NAME = "Unknown Events"

_VARIANT_SUFFIX = {"highlights": " (Highlights)", "mini": " (Mini)"}
_SIDECAR_VARIANT_TEXT = {
    "full": "Full game",
    "highlights": "Highlights package",
    "mini": "Condensed match",
}


def _sanitize(name: str) -> str:
    """Make a string safe as a file/folder name on Windows and Linux."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or "Unnamed"


def _matchup(event: SafeEvent) -> str:
    if event.home_team and event.away_team:
        return f"{event.home_team} vs {event.away_team}"
    return event.name or "Unknown Matchup"


def build_names(event: SafeEvent, variant: str, extension: str) -> tuple[Path, str]:
    """Relative game directory and episode filename for a matched event."""
    league = _sanitize(event.league or "Unknown League")
    season = event.event_date[:4] if event.event_date else (event.season[:4] or "0000")
    suffix = _VARIANT_SUFFIX.get(variant, "")
    matchup = _sanitize(_matchup(event))
    date = event.event_date or "0000-00-00"

    game_dir = Path(league) / f"Season {season}" / _sanitize(f"{matchup} {date}{suffix}")
    episode = _sanitize(f"{league} - {date} - {matchup}{suffix}") + extension
    return game_dir, episode


def _move_file(src: Path, dest: Path) -> None:
    """Atomic rename, falling back to copy + size-verify + delete across devices."""
    try:
        src.rename(dest)
        return
    except OSError:
        pass
    log.info("organize: cross-device move, copying %s", src.name)
    size = src.stat().st_size
    shutil.copy2(src, dest)
    if dest.stat().st_size != size:
        dest.unlink(missing_ok=True)
        raise OSError(f"copy verification failed for {src} -> {dest}")
    src.unlink()


def _write_sidecar(
    target_dir: Path,
    original_name: str,
    guess: GameGuess,
    event: SafeEvent | None,
    art_status: dict[str, str],
) -> Path:
    """game.json — machine-readable, and spoiler-free by construction: built
    from SafeEvent (no score fields exist to leak)."""
    if event is not None:
        payload = {
            "matched": True,
            "sport": event.sport,
            "league": event.league,
            "season": event.season,
            "round": event.round,
            "home_team": event.home_team,
            "away_team": event.away_team,
            "event_name": event.name,
            "event_date": event.event_date,
            "venue": event.venue,
            "thesportsdb_event_id": event.event_id,
        }
    else:
        payload = {
            "matched": False,
            "sport": guess.sport,
            "league": guess.league,
            "home_team": guess.home_team,
            "away_team": guess.away_team,
            "event_name": guess.event_name,
            "event_date": guess.event_date,
        }
    payload.update(
        {
            "variant": guess.variant,
            "variant_description": _SIDECAR_VARIANT_TEXT.get(guess.variant, "Full game"),
            "identifier": {"source": guess.source, "confidence": guess.confidence},
            "artwork": art_status,
            "original_filename": original_name,
            "processed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "spoiler_free": True,
        }
    )
    sidecar = target_dir / "game.json"
    sidecar.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return sidecar


def _place_artwork(
    event: SafeEvent, guess: GameGuess, target_dir: Path, episode_stem: str, config: Config
) -> dict[str, str]:
    """Download/generate thumb, poster, background into the game directory."""
    status = {"thumb": "none", "poster": "none", "background": "none"}
    thumb_path = target_dir / f"{episode_stem}.jpg"

    with tempfile.TemporaryDirectory(dir=target_dir) as tmp:
        downloaded = (
            matcher.download_artwork(event, Path(tmp), config)
            if config.artwork_mode == "download"
            else {}
        )
        if "thumb" in downloaded:
            shutil.move(downloaded["thumb"], thumb_path)
            status["thumb"] = "downloaded"
        if "poster" in downloaded:
            shutil.move(downloaded["poster"], target_dir / "poster.jpg")
            status["poster"] = "downloaded"
        if "fanart" in downloaded:
            shutil.move(downloaded["fanart"], target_dir / "background.jpg")
            status["background"] = "downloaded"

    if status["thumb"] == "none":
        artwork.generate_card(
            thumb_path,
            _matchup(event),
            subtitle=event.league,
            footer=event.event_date,
        )
        status["thumb"] = "generated"

    if guess.variant in artwork.BADGE_LABELS and artwork.apply_badge(
        thumb_path, guess.variant, config
    ):
        status["thumb"] += "+badge"
    return status


def _log_plan(path: Path, target_dir: Path, episode: str, event: SafeEvent | None) -> None:
    log.info("organize: would create %s", target_dir)
    log.info("organize: would move %s -> %s", path.name, target_dir / episode)
    if event is None:
        log.info("organize: would write Unknown Event placeholder thumb + sidecar")
    else:
        log.info("organize: would place thumb/poster/background + spoiler-free sidecar")


def organize(
    path: Path,
    guess: GameGuess,
    event: SafeEvent | None,
    config: Config,
    dry_run: bool,
) -> OrganizeResult:
    """Organize one media file into the library."""
    if event is None:
        target_dir = config.library_dir / UNKNOWN_DIR_NAME / _sanitize(path.stem)
        episode = path.name
    else:
        game_dir, episode = build_names(event, guess.variant, path.suffix.lower())
        target_dir = config.library_dir / game_dir

    media_target = target_dir / episode
    if dry_run:
        _log_plan(path, target_dir, episode, event)
        return OrganizeResult(
            status="planned",
            target_dir=str(target_dir),
            media_file=str(media_target),
            sidecar=str(target_dir / "game.json"),
            detail="dry run: nothing written",
        )

    if media_target.exists():
        log.error("organize: target already exists: %s", media_target)
        return OrganizeResult(
            status="error",
            target_dir=str(target_dir),
            media_file=str(media_target),
            detail="target file already exists",
        )

    target_dir.mkdir(parents=True, exist_ok=True)
    art_written: list[str] = []

    if event is None:
        thumb_path = target_dir / f"{media_target.stem}.jpg"
        custom = config.config_dir / "unknown-event.jpg"
        if custom.is_file():
            shutil.copyfile(custom, thumb_path)
            art_status = {"thumb": "placeholder-custom"}
        else:
            artwork.generate_card(thumb_path, "Unknown Event", subtitle=path.stem[:70])
            art_status = {"thumb": "placeholder-generated"}
        if guess.variant in artwork.BADGE_LABELS and artwork.apply_badge(
            thumb_path, guess.variant, config
        ):
            art_status["thumb"] += "+badge"
        art_written.append(str(thumb_path))
        status = "unknown"
    else:
        art_status = _place_artwork(event, guess, target_dir, media_target.stem, config)
        art_written = [
            str(target_dir / name)
            for name, key in [
                (f"{media_target.stem}.jpg", "thumb"),
                ("poster.jpg", "poster"),
                ("background.jpg", "background"),
            ]
            if art_status.get(key, "none") != "none"
        ]
        status = "organized"

    sidecar = _write_sidecar(target_dir, path.name, guess, event, art_status)
    _move_file(path, media_target)
    log.info("organize: %s -> %s (artwork: %s)", status, target_dir, art_status)

    return OrganizeResult(
        status=status,
        target_dir=str(target_dir),
        media_file=str(media_target),
        artwork_written=tuple(art_written),
        sidecar=str(sidecar),
        detail=f"artwork: {art_status}",
    )
