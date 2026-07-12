"""Watcher daemon: /watch -> pipeline -> /library, hands-off (design.md §3.1).

- watchdog observer for instant reaction on local filesystems
- periodic sweep as a safety net for mounts where inotify doesn't fire
  (network shares, some Docker volume drivers) and for files that landed
  while the daemon was down
- file-stability gate: recordings are written over hours; a file is only
  processed once its size has stopped changing for STABILITY_SECONDS
- SQLite ledger prevents double-processing across restarts
"""

from __future__ import annotations

import logging
import queue
import signal
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from sfps import pipeline
from sfps.config import Config
from sfps.ledger import FileIdentity, Ledger

log = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = 30.0  # seconds between /config/heartbeat touches


def wait_for_stable(
    path: Path,
    stability_seconds: float,
    stop: threading.Event,
    poll: float | None = None,
) -> bool:
    """Block until the file's size/mtime is unchanged for stability_seconds.

    Returns False if the file disappears or the daemon is shutting down.
    """
    if poll is None:
        poll = max(0.2, min(stability_seconds / 4, 5.0)) if stability_seconds > 0 else 0.05
    last: tuple[int, float] | None = None
    stable_since = time.monotonic()
    while not stop.is_set():
        try:
            stat = path.stat()
        except OSError:
            log.info("watcher: %s disappeared while waiting for stability", path.name)
            return False
        current = (stat.st_size, stat.st_mtime)
        if current != last:
            last = current
            stable_since = time.monotonic()
        elif time.monotonic() - stable_since >= stability_seconds:
            try:
                with path.open("rb"):
                    return True
            except OSError:
                stable_since = time.monotonic()  # still locked by the recorder
        stop.wait(poll)
    return False


class _Handler(FileSystemEventHandler):
    def __init__(self, daemon: Daemon) -> None:
        self._daemon = daemon

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._daemon.enqueue(Path(str(event.src_path)))

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._daemon.enqueue(Path(str(event.dest_path)))


class Daemon:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.ledger = Ledger(config.config_dir / "ledger.db")
        self._queue: queue.Queue[Path] = queue.Queue()
        self._pending: set[Path] = set()
        self._pending_lock = threading.Lock()
        self._stop = threading.Event()

    # -- intake ----------------------------------------------------------

    def enqueue(self, path: Path) -> None:
        if path.suffix.lower() not in self.config.media_extensions:
            return
        with self._pending_lock:
            if path in self._pending:
                return
            self._pending.add(path)
        log.info("watcher: queued %s", path.name)
        self._queue.put(path)

    def sweep(self) -> int:
        """Enqueue unprocessed media files already sitting in /watch."""
        found = 0
        if not self.config.watch_dir.is_dir():
            log.warning("watcher: watch dir %s does not exist", self.config.watch_dir)
            return 0
        for path in sorted(self.config.watch_dir.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in self.config.media_extensions:
                continue
            if self.ledger.is_processed(path):
                continue
            with self._pending_lock:
                already = path in self._pending
            if not already:
                self.enqueue(path)
                found += 1
        return found

    # -- processing ------------------------------------------------------

    def process_path(self, path: Path) -> str:
        """Stability-gate, dedupe, and run one file through the pipeline."""
        try:
            if not wait_for_stable(path, self.config.stability_seconds, self._stop):
                return "skipped"
            identity = FileIdentity.of(path)
            if self.ledger.has(identity):
                log.info("watcher: %s already processed, skipping", path.name)
                return "skipped"

            result = pipeline.process_file(path, self.config, dry_run=self.config.dry_run)
            if not self.config.dry_run:
                self.ledger.record(identity, result.status, result.target_dir, result.detail)
            return result.status
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the daemon
            log.exception("watcher: error processing %s", path.name)
            try:
                if not self.config.dry_run and path.exists():
                    self.ledger.record(FileIdentity.of(path), "error", detail=str(exc))
            except OSError:
                pass
            return "error"
        finally:
            with self._pending_lock:
                self._pending.discard(path)

    # -- lifecycle ---------------------------------------------------------

    def _beat(self) -> None:
        """Touch /config/heartbeat so `sfps health` (docker HEALTHCHECK) passes."""
        try:
            hb = self.config.config_dir / "heartbeat"
            hb.parent.mkdir(parents=True, exist_ok=True)
            hb.touch()
        except OSError as exc:
            log.warning("watcher: cannot write heartbeat (%s)", exc)

    def stop(self, *_args) -> None:
        log.info("watcher: shutdown requested")
        self._stop.set()

    def run(self, install_signals: bool = True) -> None:
        cfg = self.config
        log.info(
            "watcher: starting - watch=%s library=%s stability=%ds sweep=%ds extensions=%s%s",
            cfg.watch_dir,
            cfg.library_dir,
            cfg.stability_seconds,
            cfg.sweep_seconds,
            ",".join(cfg.media_extensions),
            " [DRY RUN]" if cfg.dry_run else "",
        )
        if install_signals:
            signal.signal(signal.SIGINT, self.stop)
            signal.signal(signal.SIGTERM, self.stop)

        observer = Observer()
        observer.schedule(_Handler(self), str(cfg.watch_dir), recursive=True)
        observer.daemon = True
        observer.start()

        swept = self.sweep()
        if swept:
            log.info("watcher: startup sweep queued %d file(s)", swept)
        last_sweep = time.monotonic()
        self._beat()
        last_beat = time.monotonic()

        try:
            while not self._stop.is_set():
                if time.monotonic() - last_beat >= HEARTBEAT_INTERVAL:
                    self._beat()
                    last_beat = time.monotonic()
                try:
                    path = self._queue.get(timeout=1.0)
                except queue.Empty:
                    if time.monotonic() - last_sweep >= cfg.sweep_seconds:
                        self.sweep()
                        last_sweep = time.monotonic()
                    continue
                self.process_path(path)
        finally:
            observer.stop()
            observer.join(timeout=5)
            log.info("watcher: stopped")
