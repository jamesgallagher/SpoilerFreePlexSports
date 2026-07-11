"""Organizer stage: place the media file, artwork, and sidecar in the library.

Phase 1 stub: computes and logs the plan but never touches the filesystem.
The real implementation (Phase 4) performs atomic moves, writes Local Media
Assets artwork, the spoiler-free game.json sidecar, and the Unknown Event
placeholder thumb.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sfps.config import Config
from sfps.models import GameGuess, OrganizeResult, SafeEvent

log = logging.getLogger(__name__)

UNKNOWN_DIR_NAME = "Unknown Events"


def plan_target(path: Path, event: SafeEvent | None, config: Config) -> Path:
    """Compute the destination directory for a media file."""
    if event is None:
        return config.library_dir / UNKNOWN_DIR_NAME / path.stem
    season = event.event_date[:4] if event.event_date else "Unknown Season"
    game_dir = f"{event.home_team} vs {event.away_team} {event.event_date}".strip()
    return config.library_dir / event.league / f"Season {season}" / game_dir


def organize(
    path: Path,
    guess: GameGuess,
    event: SafeEvent | None,
    config: Config,
    dry_run: bool,
) -> OrganizeResult:
    """Organize a media file into the library. Stub: plan-only, never moves."""
    target = plan_target(path, event, config)

    if event is None:
        log.info("organize: no matched event -> Unknown Event path")
        log.info("organize: would create %s", target)
        log.info("organize: would move %s -> %s", path.name, target / path.name)
        log.info("organize: would write placeholder thumb (Unknown Event)")
        log.info("organize: would write game.json sidecar (matched=false)")
    else:
        log.info("organize: would create %s", target)
        log.info("organize: would move %s -> %s", path.name, target / path.name)
        log.info("organize: would write thumb/poster/background from event artwork")
        log.info("organize: would write spoiler-free game.json sidecar")

    if not dry_run:
        log.warning("organize: Phase 1 stub - no filesystem changes made (real moves in Phase 4)")

    return OrganizeResult(
        status="planned",
        target_dir=str(target),
        media_file=str(target / path.name),
        sidecar=str(target / "game.json"),
        detail="Phase 1 stub: plan only, nothing written",
    )
