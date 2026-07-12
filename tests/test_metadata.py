from pathlib import Path
from xml.etree import ElementTree as ET

from sfps import metadata
from sfps.models import SafeEvent

RUGBY = SafeEvent(
    event_id="2449593",
    name="Australia Rugby vs France Rugby",
    sport="Rugby",
    league="Nations Championship",
    season="2026",
    round="2",
    home_team="Australia Rugby",
    away_team="France Rugby",
    event_date="2026-07-11",
    venue="Suncorp Stadium",
    city="Brisbane, QLD",
    country="Australia",
)


def test_build_summary_full():
    s = metadata.build_summary(RUGBY, "full")
    assert s == (
        "Rugby Nations Championship (Round 2): Australia Rugby vs France Rugby "
        "at Suncorp Stadium, Brisbane, QLD, Australia on 11 July 2026."
    )


def test_build_summary_highlights_tail():
    assert metadata.build_summary(RUGBY, "highlights").endswith("Highlights package.")
    assert metadata.build_summary(RUGBY, "mini").endswith("Condensed match.")


def test_build_summary_omits_missing_fields():
    lean = SafeEvent(
        event_id="1",
        name="A vs B",
        sport="Rugby",
        home_team="A",
        away_team="B",
        event_date="2026-07-11",
    )
    s = metadata.build_summary(lean, "full")
    assert "at " not in s  # no venue/city/country
    assert "Round" not in s
    assert s == "Rugby: A vs B on 11 July 2026."


def test_build_summary_non_team_event():
    f1 = SafeEvent(
        event_id="1",
        name="Miami Grand Prix Sprint Qualifying",
        sport="Motorsport",
        league="Formula 1",
        event_date="2026-05-01",
        venue="Miami International Autodrome",
    )
    s = metadata.build_summary(f1, "full")
    assert "Miami Grand Prix Sprint Qualifying" in s
    assert "Formula 1" in s
    assert "at Miami International Autodrome" in s


def test_build_summary_handles_no_date():
    e = SafeEvent(event_id="1", name="A vs B", sport="Rugby", home_team="A", away_team="B")
    s = metadata.build_summary(e)
    assert "on " not in s
    assert s.endswith(".")


def test_display_title_prefers_event_name():
    assert metadata.display_title(RUGBY) == "Australia Rugby vs France Rugby"
    no_name = SafeEvent(event_id="1", home_team="A", away_team="B")
    assert metadata.display_title(no_name) == "A vs B"


def test_summary_contains_no_result_language():
    """SafeEvent structurally cannot hold scores; this guards the wording too.
    (Bare score digits like '26' are not checked — they collide with years.)"""
    s = metadata.build_summary(RUGBY, "full").lower()
    for banned in ("26-42", "26 - 42", "won", "beat", "defeat", "score", "result", "final score"):
        assert banned not in s


def test_write_episode_nfo_is_valid_and_complete(tmp_path: Path):
    dest = tmp_path / "game.nfo"
    metadata.write_episode_nfo(RUGBY, dest, "highlights")

    root = ET.fromstring(dest.read_text(encoding="utf-8"))
    assert root.tag == "episodedetails"
    assert root.findtext("title") == "Australia Rugby vs France Rugby (Highlights)"
    assert "Nations Championship" in root.findtext("plot")
    assert root.findtext("aired") == "2026-07-11"
    assert root.findtext("year") == "2026"
    assert root.findtext("studio") == "Nations Championship"
    assert root.findtext("genre") == "Rugby"


def test_write_episode_nfo_declares_utf8(tmp_path: Path):
    dest = tmp_path / "game.nfo"
    metadata.write_episode_nfo(RUGBY, dest, "full")
    assert dest.read_text(encoding="utf-8").startswith('<?xml version="1.0" encoding="UTF-8"?>')


def test_write_show_nfo_once(tmp_path: Path):
    dest = tmp_path / "tvshow.nfo"
    assert metadata.write_show_nfo("Nations Championship", "Rugby", dest) == dest
    root = ET.fromstring(dest.read_text(encoding="utf-8"))
    assert root.tag == "tvshow"
    assert root.findtext("title") == "Nations Championship"
    # second call must not overwrite
    assert metadata.write_show_nfo("Nations Championship", "Rugby", dest) is None
