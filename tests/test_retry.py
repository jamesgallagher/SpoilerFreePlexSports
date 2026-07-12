import json
from pathlib import Path

import pytest
from PIL import Image

from sfps import identifier, matcher, organizer, retry
from sfps.config import Config
from sfps.ledger import FileIdentity, Ledger
from sfps.models import GameGuess, SafeEvent

# Raw API payload including the score fields the firewall strips
RAW_EVENT = {
    "idEvent": "2052711",
    "strEvent": "Texas Super Kings vs Washington Freedom",
    "strSport": "Cricket",
    "strLeague": "Major League Cricket",
    "strSeason": "2026",
    "strHomeTeam": "Texas Super Kings",
    "strAwayTeam": "Washington Freedom",
    "dateEvent": "2026-07-11",
    "strVenue": "Grand Prairie Stadium",
    "strThumb": "https://img.example/event/thumb.jpg",
    "strPoster": "https://img.example/event/poster.jpg",
    "intHomeScore": "184",
    "intAwayScore": "180",
    "strStatus": "Match Finished",
}

EVENT = SafeEvent(
    event_id="2466440",
    name="Texas Super Kings vs Washington Freedom",
    sport="Cricket",
    league="Major League Cricket",
    season="2026",
    home_team="Texas Super Kings",
    away_team="Washington Freedom",
    event_date="2026-07-11",
    artwork={"thumb": "https://img.example/thumb.jpg"},
)

GUESS = GameGuess(
    identified=True,
    league="Major League Cricket",
    home_team="Texas Super Kings",
    away_team="Washington Freedom",
    event_date="2026-07-11",
    confidence=0.95,
    source="gemini",
)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config.from_env(
        env={
            "GEMINI_API_KEY": "x",
            "WATCH_DIR": str(tmp_path / "watch"),
            "LIBRARY_DIR": str(tmp_path / "library"),
            "CONFIG_DIR": str(tmp_path / "config"),
        }
    )


@pytest.fixture
def no_downloads(monkeypatch):
    monkeypatch.setattr(
        matcher, "download_artwork", lambda event, dest, config, client=None: {}
    )


def make_unknown(config: Config, name: str = "mystery game.mkv") -> Path:
    """Simulate the daemon's unknown flow: organize + ledger entry."""
    f = config.watch_dir / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"\x00" * 1024)
    identity = FileIdentity.of(f)
    result = organizer.organize(f, GameGuess(identified=False), None, config, dry_run=False)
    Ledger(config.config_dir / "ledger.db").record(
        identity, "unknown", result.target_dir, "no match"
    )
    return Path(result.target_dir)


class FakeClient:
    def __init__(self, raw=RAW_EVENT):
        self.raw = raw

    def lookup_event(self, event_id: str):
        return self.raw if event_id == self.raw["idEvent"] else None

    def close(self):
        pass


# --- retry_unknowns -----------------------------------------------------------


def test_retry_unknowns_reorganizes(config: Config, monkeypatch, no_downloads):
    unknown_dir = make_unknown(config)
    monkeypatch.setattr(identifier, "identify", lambda path, cfg: GUESS)
    monkeypatch.setattr(
        matcher, "match", lambda guess, cfg, hint_date=None, client=None: EVENT
    )

    stats = retry.retry_unknowns(config)

    assert stats == {"eligible": 1, "matched": 1}
    assert not unknown_dir.exists(), "unknown dir must be cleaned up"
    new_dir = (
        config.library_dir
        / "Major League Cricket"
        / "Season 2026"
        / "Texas Super Kings vs Washington Freedom 2026-07-11"
    )
    assert new_dir.is_dir()
    assert any(p.suffix == ".mkv" for p in new_dir.iterdir())

    ledger = Ledger(config.config_dir / "ledger.db")
    assert ledger.entries(status="unknown") == []
    assert len(ledger.entries(status="organized")) == 1


def test_retry_unknowns_leaves_still_unmatched(config: Config, monkeypatch, no_downloads):
    unknown_dir = make_unknown(config)
    monkeypatch.setattr(identifier, "identify", lambda path, cfg: GUESS)
    monkeypatch.setattr(
        matcher, "match", lambda guess, cfg, hint_date=None, client=None: None
    )

    stats = retry.retry_unknowns(config)
    assert stats == {"eligible": 1, "matched": 0}
    assert unknown_dir.exists()


def test_retry_unknowns_respects_window(config: Config, monkeypatch, no_downloads):
    make_unknown(config)
    monkeypatch.setattr(retry, "_within_window", lambda iso, days: False)
    stats = retry.retry_unknowns(config)
    assert stats == {"eligible": 0, "matched": 0}


# --- retry_artwork ------------------------------------------------------------


def make_organized(config: Config, art: dict) -> Path:
    game_dir = config.library_dir / "Major League Cricket" / "Season 2026" / "TSK vs WF"
    game_dir.mkdir(parents=True)
    (game_dir / "MLC - 2026-07-11 - TSK vs WF.mkv").write_bytes(b"\x00" * 64)
    from datetime import datetime

    sidecar = {
        "matched": True,
        "thesportsdb_event_id": "2052711",
        "variant": "full",
        "artwork": art,
        "processed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    (game_dir / "game.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return game_dir


def fake_download_urls(urls, dest_dir, config, client=None):
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = {}
    for kind in urls:
        p = dest_dir / f"{kind}.jpg"
        Image.new("RGB", (100, 60), (5, 5, 80)).save(p, "JPEG")
        out[kind] = p
    return out


def test_retry_artwork_upgrades(config: Config, monkeypatch):
    game_dir = make_organized(
        config, {"thumb": "generated", "poster": "none", "background": "none"}
    )
    monkeypatch.setattr(matcher, "download_urls", fake_download_urls)

    stats = retry.retry_artwork(config, client=FakeClient())

    assert stats == {"checked": 1, "updated": 1}
    assert (game_dir / "MLC - 2026-07-11 - TSK vs WF.jpg").is_file()
    assert (game_dir / "poster.jpg").is_file()
    data = json.loads((game_dir / "game.json").read_text(encoding="utf-8"))
    assert data["artwork"]["thumb"] == "downloaded"
    assert data["artwork"]["poster"] == "downloaded"
    assert "artwork_updated_at" in data


def test_retry_artwork_skips_complete(config: Config):
    make_organized(
        config,
        {"thumb": "downloaded", "poster": "downloaded", "background": "downloaded"},
    )
    stats = retry.retry_artwork(config, client=FakeClient())
    assert stats == {"checked": 0, "updated": 0}


def test_retry_artwork_noop_in_generate_mode(tmp_path: Path):
    cfg = Config.from_env(
        env={"GEMINI_API_KEY": "x", "ARTWORK_MODE": "generate", "LIBRARY_DIR": str(tmp_path)}
    )
    assert retry.retry_artwork(cfg, client=FakeClient()) == {"checked": 0, "updated": 0}


# --- force_match ---------------------------------------------------------------


def test_force_match_from_unknown_dir(config: Config, monkeypatch, no_downloads):
    unknown_dir = make_unknown(config, "who knows.mkv")

    result = retry.force_match(unknown_dir, "2052711", config, client=FakeClient())

    assert result is not None and result.status == "organized"
    assert not unknown_dir.exists()
    assert "Major League Cricket" in result.target_dir
    data = json.loads((Path(result.target_dir) / "game.json").read_text(encoding="utf-8"))
    assert data["identifier"]["source"] == "review"
    assert data["thesportsdb_event_id"] == "2052711"


def test_force_match_unknown_event_id(config: Config, no_downloads):
    unknown_dir = make_unknown(config, "still unknown.mkv")
    assert retry.force_match(unknown_dir, "999999", config, client=FakeClient()) is None
    assert unknown_dir.exists()
