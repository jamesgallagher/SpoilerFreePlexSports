"""Matcher stage: GameGuess -> verified SafeEvent (or None).

Phase 1 stub. The real implementation (Phase 3) queries TheSportsDB with the
3-step lookup strategy and strips all score/result fields at this boundary
(the spoiler firewall — see design.md §3.3).
"""

from __future__ import annotations

import logging

from sfps.config import Config
from sfps.models import GameGuess, SafeEvent

log = logging.getLogger(__name__)


def match(guess: GameGuess, config: Config) -> SafeEvent | None:
    """Find and verify the event on TheSportsDB. Stub: never matches."""
    if not guess.identified:
        log.info("match: skipped (no identification to match)")
        return None
    log.info(
        "match: %s vs %s on %s (stub - real matcher lands in Phase 3)",
        guess.home_team,
        guess.away_team,
        guess.event_date,
    )
    return None
