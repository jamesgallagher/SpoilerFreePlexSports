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
