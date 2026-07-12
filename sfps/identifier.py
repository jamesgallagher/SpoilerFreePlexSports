"""Identifier stage: filename + timestamp -> GameGuess.

A regex pre-pass extracts recorder timestamps embedded in the filename
(e.g. `_20260502_224400`); an LLM (Groq by default, Gemini optional) then does
the actual interpretation with those hints. Any failure degrades to an
unidentified guess — a wrong match is worse than the Unknown Event path
(see design.md §3.2).
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from sfps import llm
from sfps.config import Config
from sfps.models import GameGuess

log = logging.getLogger(__name__)

# Recorder-style timestamp: ..._20260502_224400.ts
_TS_PATTERN = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})(?!\d)")
# Plain ISO date anywhere in the name: 2026-07-12
_DATE_PATTERN = re.compile(r"(?<!\d)(20\d{2})-(\d{2})-(\d{2})(?!\d)")

# Content-variant tokens (design.md §3.5). Filenames use _, ., - as separators
# (which are word characters or regex specials), so tokenize instead of \b.
_TOKEN_SPLIT = re.compile(r"[^A-Za-z0-9']+")
_HIGHLIGHTS_TOKENS = {"hl", "hls", "highlights"}
_MINI_TOKENS = {"mini"}


def detect_variant(filename: str) -> str:
    """Deterministic content-variant detection from filename tokens."""
    tokens = {t.lower() for t in _TOKEN_SPLIT.split(filename) if t}
    if tokens & _HIGHLIGHTS_TOKENS:
        return "highlights"
    if tokens & _MINI_TOKENS:
        return "mini"
    return "full"

SYSTEM_INSTRUCTION = """\
You identify sports events from DVR/PVR recording filenames.

You receive a raw filename plus optional hints: a timestamp embedded in the
filename (usually the local recording start time) and the file's modified time
(usually the recording end time).

Rules:
- Expand abbreviations: competition codes (EPL, JWC = World Rugby U20
  Championship, etc.), country codes (RSA = South Africa, ENG = England),
  team short names.
- league: the full official competition name as commonly listed in sports
  databases, e.g. "English Premier League", "Formula 1", "Major League Cricket".
- Team sports: fill home_team and away_team with full names. In "A v B" or
  "A vs B", A is usually the home team.
- Non-team events (motorsport, golf, tennis, athletics): leave the team fields
  empty and put the specific session in event_name, e.g.
  "Miami Grand Prix Sprint Qualifying".
- event_date: ISO YYYY-MM-DD local date the event was played. A recording
  timestamp is when recording STARTED; a late-night recording may be of an
  event dated the previous or next day in the venue's timezone. Use year hints
  like '26 in the filename. If you cannot determine a date, leave it empty.
- round: the round/week/stage if evident, e.g. "Sprint Qualifying", "Week 5",
  "Semi Final".
- confidence (0-1): certainty in competition + participants + date combined.
  Go below 0.5 whenever you had to guess the competition.
- identified: true only if there is enough to look this event up in a sports
  database (participants or event_name, plus a league or approximate date).
  When in doubt return identified=false rather than inventing details.
- notes: one short sentence on what was inferred vs read directly.
- NEVER mention or guess scores, winners, or results anywhere in the output.
"""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "identified": {"type": "boolean"},
        "sport": {"type": "string"},
        "league": {"type": "string"},
        "home_team": {"type": "string"},
        "away_team": {"type": "string"},
        "event_name": {"type": "string"},
        "event_date": {"type": "string"},
        "round": {"type": "string"},
        "confidence": {"type": "number"},
        "notes": {"type": "string"},
    },
    "required": ["identified", "confidence"],
}


def extract_timestamp(filename: str) -> datetime | None:
    """Pull a recorder timestamp or ISO date out of a filename, if present."""
    m = _TS_PATTERN.search(filename)
    if m:
        try:
            return datetime(*(int(g) for g in m.groups()))
        except ValueError:
            pass
    m = _DATE_PATTERN.search(filename)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return None


def _build_prompt(filename: str, mtime: datetime | None, config: Config) -> str:
    lines = [f"Filename: {filename}"]
    embedded = extract_timestamp(filename)
    if embedded:
        lines.append(f"Timestamp embedded in filename: {embedded.isoformat()}")
    if mtime:
        lines.append(f"File modified time: {mtime.isoformat()}")
    lines.append(f"Recorder timezone: {config.timezone}")
    return "\n".join(lines)


_ISO_DATE = re.compile(r"^20\d{2}-\d{2}-\d{2}$")


def _clean_date(value: object) -> str:
    """Keep only full ISO dates; partial dates like '2026' become empty."""
    text = str(value or "").strip()
    return text if _ISO_DATE.match(text) else ""


def _parse_response(text: str) -> GameGuess:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("response is not a JSON object")
    return GameGuess(
        identified=bool(data.get("identified", False)),
        sport=str(data.get("sport") or ""),
        league=str(data.get("league") or ""),
        home_team=str(data.get("home_team") or ""),
        away_team=str(data.get("away_team") or ""),
        event_name=str(data.get("event_name") or ""),
        event_date=_clean_date(data.get("event_date")),
        round=str(data.get("round") or ""),
        confidence=max(0.0, min(1.0, float(data.get("confidence", 0.0)))),
        source="llm",  # overwritten with the provider name by identify_name()
        notes=str(data.get("notes") or ""),
    )


def identify_name(filename: str, mtime: datetime | None, config: Config) -> GameGuess:
    """Identify a game from a filename string (file need not exist)."""
    variant = detect_variant(filename)
    provider = config.llm_provider
    prompt = _build_prompt(filename, mtime, config)
    log.debug("identify prompt:\n%s", prompt)
    try:
        text = llm.generate_json(config, SYSTEM_INSTRUCTION, prompt, RESPONSE_SCHEMA)
        guess = replace(_parse_response(text), variant=variant, source=provider)
    except llm.LLMError as exc:
        log.warning("identify: LLM call failed (%s) -> unidentified", exc)
        return GameGuess(
            identified=False, variant=variant, source=provider, notes=f"llm error: {exc}"
        )
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        log.warning("identify: unparseable LLM response (%s) -> unidentified", exc)
        return GameGuess(
            identified=False, variant=variant, source=provider, notes=f"bad response: {exc}"
        )

    log.info(
        "identify: identified=%s league='%s' teams='%s vs %s' event='%s' "
        "date=%s confidence=%.2f",
        guess.identified,
        guess.league,
        guess.home_team,
        guess.away_team,
        guess.event_name,
        guess.event_date,
        guess.confidence,
    )
    return guess


def identify(path: Path, config: Config) -> GameGuess:
    """Identify a game from a file on disk (pipeline entrypoint)."""
    mtime = None
    with contextlib.suppress(OSError):
        mtime = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    return identify_name(path.name, mtime, config)
