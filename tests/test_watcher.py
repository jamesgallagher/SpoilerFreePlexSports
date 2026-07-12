import threading
import time
from pathlib import Path

import pytest

from sfps import pipeline
from sfps.config import Config
from sfps.models import OrganizeResult
from sfps.watcher import Daemon, wait_for_stable


@pytest.fixture
def config(tmp_path: Path) -> Config:
    (tmp_path / "watch").mkdir()
    return Config.from_env(
        env={
            "GEMINI_API_KEY": "x",
            "WATCH_DIR": str(tmp_path / "watch"),
            "LIBRARY_DIR": str(tmp_path / "library"),
            "CONFIG_DIR": str(tmp_path / "config"),
            "STABILITY_SECONDS": "0",
            "SWEEP_SECONDS": "1",
        }
    )


def drop_file(config: Config, name: str = "EPL Arsenal vs Chelsea 2026-07-12.ts") -> Path:
    f = config.watch_dir / name
    f.write_bytes(b"\x00" * 2048)
    return f


# --- stability gate -----------------------------------------------------------


def test_stable_file_passes(tmp_path: Path):
    f = tmp_path / "done.ts"
    f.write_bytes(b"x" * 100)
    assert wait_for_stable(f, 0.2, threading.Event(), poll=0.05)


def test_growing_file_waits_until_stable(tmp_path: Path):
    f = tmp_path / "recording.ts"
    f.write_bytes(b"x")

    def grow():
        for _ in range(3):
            time.sleep(0.1)
            with f.open("ab") as fh:
                fh.write(b"more")

    t = threading.Thread(target=grow)
    start = time.monotonic()
    t.start()
    assert wait_for_stable(f, 0.3, threading.Event(), poll=0.05)
    elapsed = time.monotonic() - start
    t.join()
    assert elapsed >= 0.5  # had to outlast the growth (0.3s) plus stability window


def test_vanished_file_returns_false(tmp_path: Path):
    f = tmp_path / "gone.ts"
    f.write_bytes(b"x")

    stop = threading.Event()

    def vanish():
        time.sleep(0.1)
        f.unlink()

    t = threading.Thread(target=vanish)
    t.start()
    assert not wait_for_stable(f, 5.0, stop, poll=0.05)
    t.join()


def test_shutdown_interrupts_wait(tmp_path: Path):
    f = tmp_path / "slow.ts"
    f.write_bytes(b"x")
    stop = threading.Event()
    threading.Timer(0.1, stop.set).start()
    assert not wait_for_stable(f, 60.0, stop, poll=0.05)


# --- daemon processing --------------------------------------------------------


def test_process_path_records_and_dedupes(config: Config, monkeypatch):
    calls = []

    def fake_process(path, cfg, dry_run=False):
        calls.append(path)
        return OrganizeResult(status="organized", target_dir="/lib/x", detail="ok")

    monkeypatch.setattr(pipeline, "process_file", fake_process)
    daemon = Daemon(config)
    f = drop_file(config)

    assert daemon.process_path(f) == "organized"
    assert len(calls) == 1
    assert daemon.ledger.is_processed(f)
    # identical file again -> skipped without invoking the pipeline
    assert daemon.process_path(f) == "skipped"
    assert len(calls) == 1


def test_process_path_records_errors(config: Config, monkeypatch):
    def boom(path, cfg, dry_run=False):
        raise RuntimeError("pipeline exploded")

    monkeypatch.setattr(pipeline, "process_file", boom)
    daemon = Daemon(config)
    f = drop_file(config)

    assert daemon.process_path(f) == "error"
    errors = daemon.ledger.entries(status="error")
    assert len(errors) == 1
    assert "exploded" in errors[0]["detail"]


def test_dry_run_does_not_touch_ledger(config: Config, monkeypatch):
    monkeypatch.setattr(
        pipeline,
        "process_file",
        lambda path, cfg, dry_run=False: OrganizeResult(status="planned"),
    )
    cfg = Config.from_env(
        env={
            "GEMINI_API_KEY": "x",
            "WATCH_DIR": str(config.watch_dir),
            "CONFIG_DIR": str(config.config_dir),
            "STABILITY_SECONDS": "0",
            "DRY_RUN": "true",
        }
    )
    daemon = Daemon(cfg)
    f = drop_file(cfg)
    assert daemon.process_path(f) == "planned"
    assert not daemon.ledger.is_processed(f)


def test_enqueue_filters_extensions(config: Config):
    daemon = Daemon(config)
    daemon.enqueue(config.watch_dir / "notes.txt")
    daemon.enqueue(config.watch_dir / "game.mkv")
    assert daemon._queue.qsize() == 1


def test_sweep_finds_preexisting_files(config: Config):
    daemon = Daemon(config)
    drop_file(config, "old recording.mkv")
    (config.watch_dir / "sub").mkdir()
    drop_file(config, "sub/nested.ts")
    drop_file(config, "ignore.txt")
    assert daemon.sweep() == 2


def test_sweep_skips_processed(config: Config, monkeypatch):
    monkeypatch.setattr(
        pipeline,
        "process_file",
        lambda path, cfg, dry_run=False: OrganizeResult(status="organized"),
    )
    daemon = Daemon(config)
    f = drop_file(config)
    daemon.process_path(f)
    assert daemon.sweep() == 0


# --- end-to-end daemon (real watchdog observer, offline gemini stub) -----------


def test_daemon_end_to_end(config: Config):
    """Drop a file while the daemon runs -> organized with zero manual steps.

    The conftest Gemini stub returns unidentified, so the file lands in
    Unknown Events with a placeholder thumb - the full unknown flow, real
    filesystem, real watchdog observer.
    """
    daemon = Daemon(config)
    thread = threading.Thread(target=daemon.run, kwargs={"install_signals": False})
    thread.start()
    try:
        time.sleep(0.5)  # let the observer start
        f = drop_file(config, "Mystery Sport Final 2026.mkv")

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if daemon.ledger.entries(status="unknown"):
                break
            time.sleep(0.2)
        entries = daemon.ledger.entries(status="unknown")
        assert entries, "daemon did not process the dropped file in time"
        assert not f.exists(), "file should have been moved out of /watch"
        assert (config.config_dir / "heartbeat").is_file(), "daemon must write a heartbeat"
        target = Path(entries[0]["target"])
        assert (target / "Mystery Sport Final 2026.mkv").is_file()
        assert (target / "Mystery Sport Final 2026.jpg").is_file()
        assert (target / "game.json").is_file()
    finally:
        daemon.stop()
        thread.join(timeout=10)
    assert not thread.is_alive()
