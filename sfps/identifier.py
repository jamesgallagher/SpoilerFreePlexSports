"""Identifier stage: filename + timestamp -> GameGuess.

Phase 1 stub. The real implementation (Phase 2) adds a regex pre-pass and a
Gemini structured-output call with date/timezone reasoning rules.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sfps.config import Config
from sfps.models import GameGuess

log = logging.getLogger(__name__)


def identify(path: Path, config: Config) -> GameGuess:
    """Return the best guess at which game this file is. Stub: never identifies."""
    log.info("identify: %s (stub - real identifier lands in Phase 2)", path.name)
    return GameGuess(
        identified=False,
        source="stub",
        notes="Phase 1 stub: identification not implemented yet",
    )
