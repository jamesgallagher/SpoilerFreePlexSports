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
