from pathlib import Path

import pytest
from PIL import Image

from sfps import identifier, matcher
from sfps.config import Config
from sfps.models import GameGuess, SafeEvent
from sfps.pipeline import process_file


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config.from_env(
        env={
            "GROQ_API_KEY": "test-key",
            "LIBRARY_DIR": str(tmp_path / "library"),
            "WATCH_DIR": str(tmp_path / "watch"),
        }
    )


@pytest.fixture
def fake_recording(tmp_path: Path) -> Path:
    f = tmp_path / "watch" / "EPL Arsenal vs Chelsea 2026-07-12.ts"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"\x00" * 1024)
    return f


def test_dry_run_ends_in_unknown_plan(config: Config, fake_recording: Path):
    """Phase 1 stubs never identify, so every file plans the Unknown Event path."""
    result = process_file(fake_recording, config, dry_run=True)
    assert result.status == "planned"
    assert "Unknown Events" in result.target_dir
    assert result.sidecar.endswith("game.json")


def test_dry_run_makes_no_filesystem_changes(config: Config, fake_recording: Path):
    library = config.library_dir
    process_file(fake_recording, config, dry_run=True)
    assert fake_recording.exists(), "source file must be untouched"
    assert not library.exists(), "nothing may be written in dry-run"


def test_teamless_event_uses_league_fallback(config: Config, fake_recording: Path, monkeypatch):
    """When a teamless event can't be matched to a specific event, the pipeline
    consults the league-art fallback and organizes it under the competition
    rather than dropping it into Unknown Events."""
    guess = GameGuess(
        identified=True,
        sport="Cycling",
        league="Tour de France",
        event_name="Tour de France Stage 8",
        event_date="2026-07-12",
        confidence=0.95,
        source="groq",
    )
    league_event = SafeEvent(
        event_id="",
        name="Tour de France Stage 8",
        sport="Cycling",
        league="UCI World Tour",
        event_date="2026-07-12",
        artwork={"thumb": "https://x/fanart.jpg"},
    )

    monkeypatch.setattr(identifier, "identify", lambda path, cfg: guess)
    monkeypatch.setattr(matcher, "match", lambda *a, **k: None)
    monkeypatch.setattr(matcher, "league_fallback", lambda *a, **k: league_event)

    def fake_download(event, dest_dir, cfg, client=None):
        dest_dir.mkdir(parents=True, exist_ok=True)
        saved = {}
        for kind in event.artwork:
            p = dest_dir / f"{kind}.jpg"
            Image.new("RGB", (320, 180), (10, 90, 40)).save(p, "JPEG")
            saved[kind] = p
        return saved

    monkeypatch.setattr(matcher, "download_artwork", fake_download)

    result = process_file(fake_recording, config, dry_run=False)
    assert result.status == "organized"
    assert "UCI World Tour" in result.target_dir
    assert "Unknown Events" not in result.target_dir


def test_missing_file_raises(config: Config, tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        process_file(tmp_path / "nope.ts", config, dry_run=True)


def test_unsupported_extension_raises(config: Config, tmp_path: Path):
    f = tmp_path / "notes.txt"
    f.write_text("hi")
    with pytest.raises(ValueError, match="unsupported extension"):
        process_file(f, config, dry_run=True)
