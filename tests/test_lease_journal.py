import json
import stat
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread

import pytest

from accounts.lease_journal import LeaseJournal


def test_pending_acquire_key_survives_restart_and_is_atomic(tmp_path):
    journal = LeaseJournal(tmp_path / "leases")
    first = journal.load_or_create("tw", "worker")
    second = LeaseJournal(tmp_path / "leases").load_or_create("tw", "worker")

    assert second.idempotency_key == first.idempotency_key
    journal_file = next((tmp_path / "leases").glob("lease-*.json"))
    assert stat.S_IMODE(journal_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(journal_file.parent.stat().st_mode) == 0o700
    assert not list(journal_file.parent.glob("*.tmp"))


def test_acquired_operation_and_release_intent_survive_restart(tmp_path):
    journal = LeaseJournal(tmp_path)
    pending = journal.load_or_create("tw", "worker")
    acquired = journal.mark_acquired(
        pending, "lease-1", datetime.now(UTC) + timedelta(minutes=5)
    )
    releasing = journal.mark_release_pending(acquired)

    restored = LeaseJournal(tmp_path).load("tw", "worker")
    assert restored == releasing
    journal.clear(releasing)
    assert journal.load("tw", "worker") is None


def test_expired_operation_gets_a_new_idempotency_key(tmp_path):
    journal = LeaseJournal(tmp_path)
    pending = journal.load_or_create("tw", "worker")
    journal.mark_acquired(pending, "lease-1", datetime.now(UTC) - timedelta(seconds=1))

    replacement = journal.load_or_create("tw", "worker")
    assert replacement.idempotency_key != pending.idempotency_key
    assert replacement.lease_id is None


def test_corrupt_journal_fails_closed(tmp_path):
    journal = LeaseJournal(tmp_path)
    operation = journal.load_or_create("tw", "worker")
    journal_file = next(tmp_path.glob("lease-*.json"))
    journal_file.write_text(json.dumps({"idempotency_key": operation.idempotency_key}))

    with pytest.raises(RuntimeError, match="journal is invalid"):
        journal.load_or_create("tw", "worker")


def test_shared_temporary_directory_is_rejected_without_chmod():
    directory = Path(tempfile.gettempdir())
    original_mode = stat.S_IMODE(directory.stat().st_mode)

    with pytest.raises(RuntimeError, match="shared temporary directory"):
        LeaseJournal(directory).load_or_create("tw", "worker")

    assert stat.S_IMODE(directory.stat().st_mode) == original_mode


def test_existing_non_private_directory_is_rejected(tmp_path):
    directory = tmp_path / "leases"
    directory.mkdir(mode=0o755)

    with pytest.raises(RuntimeError, match="must be private"):
        LeaseJournal(directory).load_or_create("tw", "worker")

    assert stat.S_IMODE(directory.stat().st_mode) == 0o755


def test_load_rejects_an_existing_non_private_directory(tmp_path):
    directory = tmp_path / "leases"
    directory.mkdir(mode=0o755)

    with pytest.raises(RuntimeError, match="must be private"):
        LeaseJournal(directory).load("tw", "worker")


def test_clear_cannot_delete_a_concurrent_replacement(tmp_path, monkeypatch):
    clearing = LeaseJournal(tmp_path)
    replacing = LeaseJournal(tmp_path)
    pending = clearing.load_or_create("tw", "worker")
    expired = clearing.mark_acquired(
        pending, "lease-old", datetime.now(UTC) - timedelta(seconds=1)
    )
    loaded = Event()
    continue_clear = Event()
    replacement_finished = Event()
    original_load = clearing._load

    def paused_load(target, region, consumer):
        operation = original_load(target, region, consumer)
        loaded.set()
        assert continue_clear.wait(timeout=2)
        return operation

    monkeypatch.setattr(clearing, "_load", paused_load)
    clear_thread = Thread(target=clearing.clear, args=(expired,))
    clear_thread.start()
    assert loaded.wait(timeout=2)

    def replace():
        replacing.load_or_create("tw", "worker")
        replacement_finished.set()

    replace_thread = Thread(target=replace)
    replace_thread.start()
    assert not replacement_finished.wait(timeout=0.1)
    continue_clear.set()
    clear_thread.join(timeout=2)
    replace_thread.join(timeout=2)

    replacement = replacing.load("tw", "worker")
    assert replacement is not None
    assert replacement.idempotency_key != expired.idempotency_key
