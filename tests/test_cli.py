from pathlib import Path

from sfps import __version__
from sfps.cli import main


def test_version(capsys):
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == __version__


def test_config_command_reports_problems(capsys, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert main(["config"]) == 1
    out = capsys.readouterr().out
    assert "GEMINI_API_KEY" in out


def test_config_command_redacts_secrets(capsys, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "super-secret-value")
    assert main(["config"]) == 0
    out = capsys.readouterr().out
    assert "super-secret-value" not in out
    assert "***set***" in out


def test_process_dry_run(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("LIBRARY_DIR", str(tmp_path / "library"))
    f = tmp_path / "NFL Week 5 2026-07-12.mkv"
    f.write_bytes(b"\x00" * 64)
    assert main(["process", str(f), "--dry-run"]) == 0


def test_process_live_refuses_bad_config(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    f = tmp_path / "game.ts"
    f.write_bytes(b"\x00")
    assert main(["process", str(f)]) == 1


def test_process_missing_file(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    assert main(["process", "C:/does/not/exist.ts", "--dry-run"]) == 2


def test_identify_prints_json_guess(capsys, monkeypatch):
    import json as jsonlib

    from sfps import gemini

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        gemini,
        "generate_json",
        lambda config, system_instruction, prompt, response_schema: jsonlib.dumps(
            {
                "identified": True,
                "league": "Formula 1",
                "event_name": "Miami Grand Prix Sprint Qualifying",
                "event_date": "2026-05-02",
                "confidence": 0.9,
            }
        ),
    )
    f1_name = "Formula_1_Highlights___Miami_Grand_Prix__Sprint_Qualifying_20260502_224400.ts"
    rc = main(["identify", f1_name])
    assert rc == 0
    data = jsonlib.loads(capsys.readouterr().out)
    assert data["event_name"] == "Miami Grand Prix Sprint Qualifying"
    assert data["identified"] is True


def test_identify_unidentified_exits_3(monkeypatch, capsys):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    # conftest offline stub returns identified=False
    assert main(["identify", "mystery recording.ts"]) == 3


def test_identify_requires_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert main(["identify", "anything.ts"]) == 1


def test_health_no_heartbeat(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    assert main(["health"]) == 1


def test_health_fresh_heartbeat(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    (tmp_path / "heartbeat").touch()
    assert main(["health"]) == 0


def test_retry_command(monkeypatch, capsys, tmp_path: Path):
    from sfps import retry

    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(
        retry,
        "retry_unknowns",
        lambda cfg: {"eligible": 2, "matched": 1, "artwork_upgraded": 1},
    )
    monkeypatch.setattr(
        retry, "retry_artwork", lambda cfg, client=None: {"checked": 3, "updated": 2}
    )
    assert main(["retry"]) == 0
    out = capsys.readouterr().out
    assert "2 eligible, 1 matched" in out
    assert "1 artwork upgraded" in out
    assert "3 checked, 2 updated" in out


def test_review_lists_unknowns(monkeypatch, capsys, tmp_path: Path):
    from sfps.ledger import FileIdentity, Ledger

    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    f = tmp_path / "weird game.ts"
    f.write_bytes(b"x")
    Ledger(tmp_path / "ledger.db").record(
        FileIdentity.of(f), "unknown", target="/library/Unknown Events/weird game"
    )
    assert main(["review"]) == 0
    out = capsys.readouterr().out
    assert "weird game.ts" in out
    assert "--set-event" in out


def test_review_set_event_requires_path(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    assert main(["review", "--set-event", "123"]) == 2


def test_health_stale_heartbeat(tmp_path: Path, monkeypatch):
    import os
    import time

    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    hb = tmp_path / "heartbeat"
    hb.touch()
    stale = time.time() - 7200  # 2h old, limit is max(300,120)*2 = 600s
    os.utime(hb, (stale, stale))
    assert main(["health"]) == 1
