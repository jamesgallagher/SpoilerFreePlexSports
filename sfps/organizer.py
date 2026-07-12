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

from sfps import artwork, matcher, metadata
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


def _move_file(src: Path, dest: Path, preserve: bool = False) -> None:
    """Atomic rename (or copy when preserving the original), verified on copy."""
    if not preserve:
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
    if preserve:
        log.info("organize: original preserved at %s", src)
    else:
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
            "title": metadata.display_title(event),
            "summary": metadata.build_summary(event, guess.variant),
            "sport": event.sport,
            "league": event.league,
            "season": event.season,
            "round": event.round,
            "home_team": event.home_team,
            "away_team": event.away_team,
            "event_name": event.name,
            "event_date": event.event_date,
            "venue": event.venue,
            "city": event.city,
            "country": event.country,
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


# artwork kind -> (sidecar status key, filename; None = named after the episode)
ART_PLACEMENT = {
    "thumb": ("thumb", None),
    "poster": ("poster", "poster.jpg"),
    "fanart": ("background", "background.jpg"),
}


def place_downloaded(kind: str, src: Path, target_dir: Path, episode_stem: str) -> Path | None:
    """Move a downloaded artwork file to its Local Media Assets name."""
    placement = ART_PLACEMENT.get(kind)
    if placement is None:
        return None
    _, filename = placement
    dest = target_dir / (filename or f"{episode_stem}.jpg")
    shutil.move(src, dest)
    return dest


def generate_thumb(event: SafeEvent, thumb_path: Path, config: Config) -> str:
    """Generate a spoiler-free thumb: badge matchup card, else neutral text card."""
    urls = matcher.team_badges(event.home_team, event.away_team, config, sport=event.sport)
    if len(urls) == 2:
        with tempfile.TemporaryDirectory(dir=thumb_path.parent) as tmp:
            badges = matcher.download_urls(urls, Path(tmp), config)
            if len(badges) == 2:
                try:
                    artwork.generate_matchup_card(
                        thumb_path,
                        badges["home"],
                        badges["away"],
                        subtitle=_matchup(event),
                        footer=f"{event.league}  ·  {event.event_date}".strip(" ·"),
                    )
                    return "generated-badges"
                except OSError as exc:
                    log.warning("artwork: badge card failed (%s); using text card", exc)
    artwork.generate_card(
        thumb_path, _matchup(event), subtitle=event.league, footer=event.event_date
    )
    return "generated"


def generate_unmatched_thumb(guess: GameGuess, thumb_path: Path, config: Config) -> str:
    """Best-effort spoiler-free thumb for a recording Gemini identified but that
    TheSportsDB could not verify — most often because the competition (e.g. a
    brand-new tournament) simply isn't indexed there yet. Identification not
    matching a database record is not the same as identification failing, so
    this still tries a real team-badge matchup card before falling back to a
    descriptive text card, and only shows a bare "Unknown Event" card when
    nothing at all was identified.
    """
    if guess.home_team and guess.away_team:
        urls = matcher.team_badges(guess.home_team, guess.away_team, config, sport=guess.sport)
        if len(urls) == 2:
            with tempfile.TemporaryDirectory(dir=thumb_path.parent) as tmp:
                badges = matcher.download_urls(urls, Path(tmp), config)
                if len(badges) == 2:
                    try:
                        artwork.generate_matchup_card(
                            thumb_path,
                            badges["home"],
                            badges["away"],
                            subtitle=f"{guess.home_team} vs {guess.away_team}",
                            footer=guess.league or guess.sport,
                        )
                        return "generated-badges-unverified"
                    except OSError as exc:
                        log.warning("artwork: unmatched badge card failed (%s)", exc)
        artwork.generate_card(
            thumb_path,
            f"{guess.home_team} vs {guess.away_team}",
            subtitle=guess.league or guess.sport,
            footer=guess.event_date,
        )
        return "generated-unverified"

    if guess.event_name:
        artwork.generate_card(
            thumb_path,
            guess.event_name,
            subtitle=guess.league or guess.sport,
            footer=guess.event_date,
        )
        return "generated-unverified"

    artwork.generate_card(thumb_path, "Unknown Event", subtitle=thumb_path.stem[:70])
    return "placeholder-generated"


def _place_artwork(
    event: SafeEvent, guess: GameGuess, target_dir: Path, episode_stem: str, config: Config
) -> dict[str, str]:
    """Download/generate thumb, poster, background into the game directory."""
    status = {"thumb": "none", "poster": "none", "background": "none"}
    thumb_path = target_dir / f"{episode_stem}.jpg"

    if config.artwork_mode == "download" and event.artwork:
        with tempfile.TemporaryDirectory(dir=target_dir) as tmp:
            downloaded = matcher.download_artwork(event, Path(tmp), config)
            for kind, src in downloaded.items():
                if place_downloaded(kind, src, target_dir, episode_stem) is not None:
                    status[ART_PLACEMENT[kind][0]] = "downloaded"

    if status["thumb"] == "none":
        status["thumb"] = generate_thumb(event, thumb_path, config)

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
            art_status = {"thumb": generate_unmatched_thumb(guess, thumb_path, config)}
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
        # Enrich the Plex card (NFO agent reads these). Matched events only.
        metadata.write_episode_nfo(event, target_dir / f"{media_target.stem}.nfo", guess.variant)
        if event.league:
            metadata.write_show_nfo(
                event.league, event.sport, target_dir.parent.parent / "tvshow.nfo"
            )
        status = "organized"

    sidecar = _write_sidecar(target_dir, path.name, guess, event, art_status)
    _move_file(path, media_target, preserve=config.preserve_original)
    log.info("organize: %s -> %s (artwork: %s)", status, target_dir, art_status)

    return OrganizeResult(
        status=status,
        target_dir=str(target_dir),
        media_file=str(media_target),
        artwork_written=tuple(art_written),
        sidecar=str(sidecar),
        detail=f"artwork: {art_status}",
    )
