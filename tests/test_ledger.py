import time
from pathlib import Path

from sfps.ledger import FileIdentity, Ledger


def make_file(tmp_path: Path, name: str = "game.ts", content: bytes = b"abc") -> Path:
    f = tmp_path / name
    f.write_bytes(content)
    return f


def test_record_and_skip(tmp_path: Path):
    ledger = Ledger(tmp_path / "config" / "ledger.db")
    f = make_file(tmp_path)
    identity = FileIdentity.of(f)

    assert not ledger.has(identity)
    ledger.record(identity, "organized", target="/library/x", detail="ok")
    assert ledger.has(identity)
    assert ledger.is_processed(f)


def test_identity_survives_move(tmp_path: Path):
    """Recording works even after the organizer moved the file away."""
    ledger = Ledger(tmp_path / "ledger.db")
    f = make_file(tmp_path)
    identity = FileIdentity.of(f)
    f.unlink()  # organizer moved it
    ledger.record(identity, "organized")
    assert ledger.has(identity)


def test_changed_file_is_new_work(tmp_path: Path):
    ledger = Ledger(tmp_path / "ledger.db")
    f = make_file(tmp_path, content=b"first")
    ledger.record(FileIdentity.of(f), "unknown")

    time.sleep(0.01)
    f.write_bytes(b"different size content")
    assert not ledger.is_processed(f)  # size changed -> new fingerprint


def test_missing_file_is_not_processed(tmp_path: Path):
    ledger = Ledger(tmp_path / "ledger.db")
    assert not ledger.is_processed(tmp_path / "nope.ts")


def test_entries_filter_by_status(tmp_path: Path):
    ledger = Ledger(tmp_path / "ledger.db")
    a = make_file(tmp_path, "a.ts", b"aaa")
    b = make_file(tmp_path, "b.ts", b"bbbb")
    ledger.record(FileIdentity.of(a), "organized")
    ledger.record(FileIdentity.of(b), "unknown")

    assert len(ledger.entries()) == 2
    unknowns = ledger.entries(status="unknown")
    assert len(unknowns) == 1
    assert unknowns[0]["path"].endswith("b.ts")


def test_persistence_across_instances(tmp_path: Path):
    db = tmp_path / "ledger.db"
    f = make_file(tmp_path)
    Ledger(db).record(FileIdentity.of(f), "organized")
    assert Ledger(db).is_processed(f)  # fresh instance, same DB
