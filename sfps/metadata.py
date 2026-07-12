"""Spoiler-free descriptions and Kodi/Plex NFO sidecars for matched events.

Plex's built-in NFO agent (PMS 1.43.1+) reads an episode `.nfo` placed next to
the video — `<title>`, `<plot>`, `<aired>` — with no API or credentials. This
enriches the Plex card for events we could positively identify.

Everything here is built from `SafeEvent`, which structurally cannot hold a
score, so the description and NFO are spoiler-free by construction.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET

from sfps.models import SafeEvent

log = logging.getLogger(__name__)

_VARIANT_SENTENCE = {
    "highlights": "Highlights package.",
    "mini": "Condensed match.",
}


def _pretty_date(iso: str) -> str:
    try:
        d = date.fromisoformat(iso)
    except (ValueError, TypeError):
        return ""
    return f"{d.day} {d.strftime('%B %Y')}"  # e.g. "11 July 2026"


def _participants(event: SafeEvent) -> str:
    if event.home_team and event.away_team:
        return f"{event.home_team} vs {event.away_team}"
    return event.name


def build_summary(event: SafeEvent, variant: str = "full") -> str:
    """A short spoiler-free description, e.g.:

    "Rugby Nations Championship (Round 2): Australia Rugby vs France Rugby at
    Suncorp Stadium, Brisbane, QLD, Australia on 11 July 2026. Highlights package."

    Every clause is optional and omitted gracefully when the field is absent.
    """
    header = " ".join(x for x in (event.sport, event.league) if x)
    if event.round:
        header = f"{header} (Round {event.round})".strip()

    body = _participants(event)
    sentence = f"{header}: {body}".strip(": ").strip() if header else body

    location = ", ".join(x for x in (event.venue, event.city, event.country) if x)
    if location:
        sentence += f" at {location}"

    pretty = _pretty_date(event.event_date)
    if pretty:
        sentence += f" on {pretty}"

    sentence = sentence.strip()
    if sentence and not sentence.endswith("."):
        sentence += "."

    tail = _VARIANT_SENTENCE.get(variant)
    if tail:
        sentence = f"{sentence} {tail}".strip()
    return sentence


def display_title(event: SafeEvent) -> str:
    """Preferred card title: TheSportsDB's event name, else the matchup."""
    return event.name or _participants(event) or "Unknown Event"


def _write_xml(root: ET.Element, dest: Path) -> None:
    ET.indent(root)
    xml = ET.tostring(root, encoding="unicode")
    dest.write_text('<?xml version="1.0" encoding="UTF-8"?>\n' + xml + "\n", encoding="utf-8")


def write_episode_nfo(event: SafeEvent, dest: Path, variant: str = "full") -> Path:
    """Kodi `episodedetails` NFO next to the video (Plex NFO agent reads it)."""
    root = ET.Element("episodedetails")
    title = display_title(event)
    if variant == "highlights":
        title = f"{title} (Highlights)"
    elif variant == "mini":
        title = f"{title} (Mini)"
    ET.SubElement(root, "title").text = title
    ET.SubElement(root, "plot").text = build_summary(event, variant)
    if event.event_date:
        ET.SubElement(root, "aired").text = event.event_date
        ET.SubElement(root, "premiered").text = event.event_date
        ET.SubElement(root, "year").text = event.event_date[:4]
    if event.league:
        ET.SubElement(root, "studio").text = event.league
    if event.sport:
        ET.SubElement(root, "genre").text = event.sport
    _write_xml(root, dest)
    log.info("metadata: wrote episode NFO -> %s", dest.name)
    return dest


def write_show_nfo(league: str, sport: str, dest: Path) -> Path | None:
    """Minimal `tvshow.nfo` at the league (show) folder; created once."""
    if dest.exists():
        return None
    root = ET.Element("tvshow")
    ET.SubElement(root, "title").text = league
    if sport:
        ET.SubElement(root, "genre").text = sport
    ET.SubElement(root, "plot").text = f"{sport} — {league}".strip(" —") if sport else league
    _write_xml(root, dest)
    log.info("metadata: wrote show NFO -> %s", dest)
    return dest
