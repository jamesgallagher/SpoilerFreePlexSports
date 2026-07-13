"""Retry pass and manual review (design.md §3.6).

TheSportsDB is crowd-sourced: an event (or its artwork) often appears days
after airing. Two passes run periodically from the daemon and on demand via
`sfps retry`:

- retry_unknowns: re-identify + re-match recordings that landed in
  Unknown Events, for up to RETRY_DAYS after processing.
- retry_artwork: upgrade generated/missing artwork on matched games when
  TheSportsDB has gained real event art.

`force_match` backs `sfps review --set-event`: a human says "this recording
is event X", no verification argued.
"""

from __future__ import annotations

import contextlib
import json
import logging
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from sfps import artwork, identifier, matcher, organizer, plex
from sfps.config import Config
from sfps.ledger import FileIdentity, Ledger
from sfps.models import GameGuess, OrganizeResult
from sfps.thesportsdb import TheSportsDBClient, TheSportsDBError

log = logging.getLogger(__name__)

# sidecar artwork key -> SafeEvent artwork kind. Thumb only: the library's
# Plex setup doesn't support poster/backdrop artwork (organizer.ART_PLACEMENT).
_SIDECAR_TO_KIND = {"thumb": "thumb"}


def _within_window(iso: str, days: int) -> bool:
    try:
        ts = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return False
    return datetime.now().astimezone() - ts <= timedelta(days=days)


def _find_media(directory: Path, config: Config) -> Path | None:
    if not directory.is_dir():
        return None
    for path in sorted(directory.iterdir()):
        if path.is_file() and path.suffix.lower() in config.media_extensions:
            return path
    return None


def _read_sidecar(directory: Path) -> dict:
    try:
        return json.loads((directory / "game.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _cleanup_unknown_dir(directory: Path) -> None:
    """Remove the placeholder thumb + sidecar left behind after a re-organize."""
    for path in list(directory.glob("*.jpg")) + [directory / "game.json"]:
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
    try:
        directory.rmdir()
    except OSError:
        log.warning("retry: could not remove %s (not empty?)", directory)


def _reorganize(
    media: Path,
    guess: GameGuess,
    event,
    config: Config,
    ledger: Ledger | None,
    old_dir: Path | None,
) -> OrganizeResult:
    identity = FileIdentity.of(media)
    result = organizer.organize(media, guess, event, config, dry_run=False)
    if result.status != "organized":
        log.warning("retry: re-organize of %s failed: %s", media.name, result.detail)
        return result
    if ledger is not None:
        ledger.record(identity, "organized", result.target_dir, "matched on retry/review")
    if old_dir is not None and old_dir != Path(result.target_dir):
        _cleanup_unknown_dir(old_dir)
    plex.rescan(config, Path(result.target_dir))
    return result


# If the placeholder still shows this status, a smarter card (badges or at
# least a descriptive text card) hasn't been tried yet - worth an upgrade
# attempt. Anything else (a custom asset, or a card we already generated our
# best effort for) is left alone.
_UPGRADEABLE_THUMB_STATUSES = {"placeholder-generated"}


def _upgrade_unknown_artwork(directory: Path, guess: GameGuess, config: Config) -> bool:
    """Refresh an Unknown Events thumb in place using a re-identified guess.

    Handles recordings that Gemini identifies correctly but TheSportsDB has
    no record of yet (e.g. a brand-new competition) - those still deserve a
    real team-badge matchup card instead of a bare "Unknown Event" label.
    """
    data = _read_sidecar(directory)
    current = str((data.get("artwork") or {}).get("thumb", ""))
    if current not in _UPGRADEABLE_THUMB_STATUSES:
        return False
    media = _find_media(directory, config)
    if media is None:
        return False

    thumb_path = directory / f"{media.stem}.jpg"
    new_status = organizer.generate_unmatched_thumb(guess, thumb_path, config)
    if new_status == current:
        return False

    art = data.get("artwork") or {}
    art["thumb"] = new_status
    data["artwork"] = art
    (directory / "game.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    log.info("retry: upgraded placeholder art for %s (%s -> %s)", media.name, current, new_status)
    return True


def retry_unknowns(config: Config) -> dict[str, int]:
    """Re-attempt identification+matching for recent Unknown Events entries."""
    ledger = Ledger(config.config_dir / "ledger.db")
    stats = {"eligible": 0, "matched": 0, "artwork_upgraded": 0}
    for entry in ledger.entries(status="unknown"):
        if not _within_window(entry["processed_at"], config.retry_days):
            continue
        target_dir = Path(entry["target"])
        media = _find_media(target_dir, config)
        if media is None:
            continue
        stats["eligible"] += 1
        guess = identifier.identify(media, config)
        if not guess.identified or guess.confidence < config.min_confidence:
            log.info("retry: %s still unidentified", media.name)
            continue
        hint_date = datetime.fromtimestamp(media.stat().st_mtime).date()
        event = matcher.match(guess, config, hint_date=hint_date)
        if event is None:
            event = matcher.league_fallback(guess, config, hint_date=hint_date)
        if event is None:
            log.info("retry: %s identified but still unmatched", media.name)
            if _upgrade_unknown_artwork(target_dir, guess, config):
                stats["artwork_upgraded"] += 1
            continue
        result = _reorganize(media, guess, event, config, ledger, target_dir)
        if result.status == "organized":
            stats["matched"] += 1
    log.info(
        "retry: unknowns pass done - %d eligible, %d matched, %d artwork upgraded",
        stats["eligible"],
        stats["matched"],
        stats["artwork_upgraded"],
    )
    return stats


def retry_artwork(config: Config, client: TheSportsDBClient | None = None) -> dict[str, int]:
    """Upgrade generated/missing artwork on matched games within the window."""
    stats = {"checked": 0, "updated": 0}
    if config.artwork_mode != "download":
        return stats
    own_client = client is None
    try:
        for sidecar_path in sorted(config.library_dir.rglob("game.json")):
            data = _read_sidecar(sidecar_path.parent)
            event_id = data.get("thesportsdb_event_id")
            if not data.get("matched") or not event_id:
                continue
            if not _within_window(str(data.get("processed_at")), config.retry_days):
                continue
            art = data.get("artwork") or {}
            wanted = {
                key
                for key, kind in _SIDECAR_TO_KIND.items()
                if not str(art.get(key, "none")).startswith("downloaded")
            }
            if not wanted:
                continue
            stats["checked"] += 1
            if client is None:
                client = TheSportsDBClient(config)
            try:
                raw = client.lookup_event(str(event_id))
            except TheSportsDBError as exc:
                log.warning("retry: artwork lookup failed (%s)", exc)
                break
            if not raw:
                continue
            event = matcher.to_safe_event(raw)
            urls = {
                _SIDECAR_TO_KIND[key]: event.artwork[_SIDECAR_TO_KIND[key]]
                for key in wanted
                if _SIDECAR_TO_KIND[key] in event.artwork
            }
            if not urls:
                continue
            directory = sidecar_path.parent
            media = _find_media(directory, config)
            if media is None:
                continue
            with tempfile.TemporaryDirectory(dir=directory) as tmp:
                downloaded = matcher.download_urls(urls, Path(tmp), config, client=client)
                for kind, src in downloaded.items():
                    dest = organizer.place_downloaded(kind, src, directory, media.stem)
                    if dest is None:
                        continue
                    key = organizer.ART_PLACEMENT[kind][0]
                    art[key] = "downloaded"
                    needs_badge = kind == "thumb" and data.get("variant") in artwork.BADGE_LABELS
                    if needs_badge and artwork.apply_badge(dest, data["variant"], config):
                        art[key] = "downloaded+badge"
            if downloaded:
                data["artwork"] = art
                data["artwork_updated_at"] = datetime.now().astimezone().isoformat(
                    timespec="seconds"
                )
                sidecar_path.write_text(
                    json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                stats["updated"] += 1
                plex.rescan(config, directory)
    finally:
        if own_client and client is not None:
            client.close()
    log.info("retry: artwork pass done - %d checked, %d updated", *stats.values())
    return stats


def force_match(
    target: Path, event_id: str, config: Config, client: TheSportsDBClient | None = None
) -> OrganizeResult | None:
    """Human override: organize `target` as TheSportsDB event `event_id`."""
    media = target if target.is_file() else _find_media(target, config)
    if media is None:
        log.error("review: no media file found at %s", target)
        return None

    own_client = client is None
    if own_client:
        client = TheSportsDBClient(config)
    try:
        raw = client.lookup_event(event_id)
    finally:
        if own_client:
            client.close()
    if not raw:
        log.error("review: TheSportsDB has no event with id %s", event_id)
        return None

    event = matcher.to_safe_event(raw)
    sidecar = _read_sidecar(media.parent)
    variant = sidecar.get("variant") or identifier.detect_variant(
        sidecar.get("original_filename") or media.name
    )
    guess = GameGuess(
        identified=True,
        sport=event.sport,
        league=event.league,
        home_team=event.home_team,
        away_team=event.away_team,
        event_name=event.name,
        event_date=event.event_date,
        confidence=1.0,
        variant=variant,
        source="review",
        notes=f"forced match to event {event_id}",
    )
    ledger = Ledger(config.config_dir / "ledger.db")
    old_dir = media.parent if (media.parent / "game.json").is_file() else None
    return _reorganize(media, guess, event, config, ledger, old_dir)
