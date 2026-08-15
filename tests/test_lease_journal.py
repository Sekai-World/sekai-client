import json
import stat
from datetime import UTC, datetime, timedelta

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
