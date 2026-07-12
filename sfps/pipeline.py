"""Pipeline orchestrator: identify -> match -> organize for a single file."""

from __future__ import annotations

import contextlib
import logging
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from sfps import identifier, matcher, organizer, plex
from sfps.config import Config
from sfps.models import GameGuess, OrganizeResult

log = logging.getLogger(__name__)


def _downgrade(guess: GameGuess) -> GameGuess:
    """Downgrade a low-confidence guess so it takes the safe Unknown Event path."""
    return replace(guess, identified=False, notes=f"below confidence threshold; {guess.notes}")


def process_file(path: Path, config: Config, dry_run: bool = False) -> OrganizeResult:
    """Run one media file through the full pipeline."""
    mode = "DRY RUN" if dry_run else "LIVE"
    log.info("=== processing %s [%s] ===", path.name, mode)

    if not path.is_file():
        raise FileNotFoundError(f"not a file: {path}")
    if path.suffix.lower() not in config.media_extensions:
        raise ValueError(
            f"unsupported extension '{path.suffix}' "
            f"(MEDIA_EXTENSIONS={','.join(config.media_extensions)})"
        )

    log.info("[1/3] identify")
    guess = identifier.identify(path, config)
    if guess.identified and guess.confidence < config.min_confidence:
        log.info(
            "identification confidence %.2f below threshold %.2f -> treating as unidentified",
            guess.confidence,
            config.min_confidence,
        )
        guess = _downgrade(guess)

    log.info("[2/3] match")
    hint_date = None
    with contextlib.suppress(OSError):
        hint_date = datetime.fromtimestamp(path.stat().st_mtime).date()
    event = matcher.match(guess, config, hint_date=hint_date)
    if event is None:
        # Teamless events (races, tours) we can name a competition for but not
        # a specific event: file under the competition with its league art
        # rather than dropping to the Unknown Event path (design.md §3.4).
        event = matcher.league_fallback(guess, config, hint_date=hint_date)

    log.info("[3/3] organize")
    result = organizer.organize(path, guess, event, config, dry_run=dry_run)

    if not dry_run and result.status in ("organized", "unknown") and plex.enabled(config):
        log.info("[+] plex partial rescan")
        plex.rescan(config, Path(result.target_dir))

    log.info("=== done: %s -> %s ===", result.status, result.target_dir)
    return result
