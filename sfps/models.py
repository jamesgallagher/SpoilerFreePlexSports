"""Core data types passed between pipeline stages.

Spoiler firewall note: `SafeEvent` deliberately has NO score, winner, or
match-status fields. TheSportsDB responses contain `intHomeScore` /
`intAwayScore` etc. — those are dropped inside the matcher's API client and
must never be added here. See design.md §1 and §3.3.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class GameGuess:
    """The identifier stage's best interpretation of a filename."""

    identified: bool
    sport: str = ""
    league: str = ""
    home_team: str = ""
    away_team: str = ""
    event_name: str = ""  # non-team events, e.g. "Miami Grand Prix Sprint Qualifying"
    event_date: str = ""  # ISO YYYY-MM-DD, event's local date
    round: str = ""  # e.g. "Matchweek 3", "Week 5", "Game 7"
    confidence: float = 0.0
    variant: str = "full"  # "full" | "highlights" | "mini" (design.md §3.5)
    source: str = ""  # "regex" | "groq" | "gemini" | "review" | "stub"
    notes: str = ""


@dataclass(frozen=True)
class SafeEvent:
    """A verified sports event with all result information stripped."""

    event_id: str
    name: str = ""  # e.g. "Arsenal vs Chelsea", "Miami Grand Prix"
    sport: str = ""
    league: str = ""
    season: str = ""
    round: str = ""
    home_team: str = ""
    away_team: str = ""
    event_date: str = ""  # ISO YYYY-MM-DD
    venue: str = ""
    city: str = ""
    country: str = ""
    # Artwork URLs by kind. Only "thumb" is ever populated: the library's
    # Plex setup doesn't support poster/backdrop artwork.
    artwork: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class OrganizeResult:
    """What the organizer did (or, in dry-run, would have done)."""

    status: str  # "organized" | "unknown" | "planned" | "error"
    target_dir: str = ""
    media_file: str = ""
    artwork_written: tuple[str, ...] = ()
    sidecar: str = ""
    detail: str = ""
