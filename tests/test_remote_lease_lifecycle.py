from datetime import UTC, datetime, timedelta

import pytest

import gunicorn_conf
import shared_client
from accounts import AccountLease, AccountRegion, TwKrCredential
from accounts.provider import AccountProviderError, InvalidLeaseError


class DurableProvider:
    requires_durable_idempotency = True

    def __init__(self, lease):
        self.lease = lease
        self.acquire_keys = []
        self.release_calls = []
        self.fail_once = False
        self.release_error = None

    def acquire(self, region, consumer, *, ttl_seconds, idempotency_key):
        self.acquire_keys.append(idempotency_key)
        if self.fail_once:
            self.fail_once = False
            raise AccountProviderError("interrupted", retryable=True)
        return self.lease

    def release(self, lease_id):
        self.release_calls.append(lease_id)
        if self.release_error:
            raise self.release_error


def _lease(lease_id="lease-1"):
    return AccountLease(
        lease_id,
        "shared-client-tw",
        datetime.now(UTC) + timedelta(minutes=5),
        TwKrCredential(AccountRegion.TW, "open-id", "token"),
    )


def test_outer_retry_reuses_durable_idempotency_key(tmp_path, monkeypatch):
    provider = DurableProvider(_lease())
    provider.fail_once = True
    monkeypatch.setenv("SEKAI_ACCOUNT_LEASE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(shared_client._lifecycle, "region", "tw")
    monkeypatch.setattr(shared_client, "_account_provider", provider)
    monkeypatch.setattr(shared_client, "_active_account_lease", None)
    monkeypatch.setattr(shared_client, "_active_lease_operation", None)

    with pytest.raises(AccountProviderError, match="interrupted"):
        shared_client.get_account_info()
    result = shared_client.get_account_info()

    assert result["userId"] == "open-id"
    assert provider.acquire_keys[0] == provider.acquire_keys[1]
    restored = shared_client._remote_lease_journal(provider).load(
        "tw", "shared-client-tw"
    )
    assert restored is not None and restored.lease_id == "lease-1"


def test_release_intent_is_cleared_only_after_release(tmp_path, monkeypatch):
    provider = DurableProvider(_lease())
    monkeypatch.setenv("SEKAI_ACCOUNT_LEASE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(shared_client._lifecycle, "region", "tw")
    monkeypatch.setattr(shared_client, "_account_provider", provider)
    monkeypatch.setattr(shared_client, "_active_account_lease", None)
    monkeypatch.setattr(shared_client, "_active_lease_operation", None)
    shared_client.get_account_info()

    shared_client.release_active_account_lease()

    assert provider.release_calls == ["lease-1"]
    assert (
        shared_client._remote_lease_journal(provider).load("tw", "shared-client-tw")
        is None
    )


def test_gunicorn_worker_exit_releases_active_lease(monkeypatch):
    released = []
    monkeypatch.setattr(
        shared_client, "release_active_account_lease", lambda: released.append(True)
    )

    gunicorn_conf.worker_exit(object(), object())

    assert released == [True]


def test_restart_completes_pending_release_before_new_acquire(tmp_path, monkeypatch):
    provider = DurableProvider(_lease("lease-new"))
    journal = shared_client.LeaseJournal(tmp_path)
    old = journal.load_or_create("tw", "shared-client-tw")
    old = journal.mark_acquired(
        old, "lease-old", datetime.now(UTC) + timedelta(minutes=5)
    )
    journal.mark_release_pending(old)
    monkeypatch.setenv("SEKAI_ACCOUNT_LEASE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(shared_client._lifecycle, "region", "tw")
    monkeypatch.setattr(shared_client, "_account_provider", provider)
    monkeypatch.setattr(shared_client, "_active_account_lease", None)
    monkeypatch.setattr(shared_client, "_active_lease_operation", None)

    shared_client.get_account_info()

    assert provider.release_calls == ["lease-old"]
    assert provider.acquire_keys[0] != old.idempotency_key


def test_restart_accepts_an_already_released_lease(tmp_path, monkeypatch):
    provider = DurableProvider(_lease("lease-new"))
    journal = shared_client.LeaseJournal(tmp_path)
    old = journal.load_or_create("tw", "shared-client-tw")
    old = journal.mark_acquired(
        old, "lease-old", datetime.now(UTC) + timedelta(minutes=5)
    )
    journal.mark_release_pending(old)
    provider.release_error = InvalidLeaseError()
    monkeypatch.setenv("SEKAI_ACCOUNT_LEASE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(shared_client._lifecycle, "region", "tw")
    monkeypatch.setattr(shared_client, "_account_provider", provider)
    monkeypatch.setattr(shared_client, "_active_account_lease", None)
    monkeypatch.setattr(shared_client, "_active_lease_operation", None)

    result = shared_client.get_account_info()

    assert result["userId"] == "open-id"
    assert provider.release_calls == ["lease-old"]
    assert provider.acquire_keys[0] != old.idempotency_key


def test_ambiguous_release_keeps_recovery_intent(tmp_path, monkeypatch):
    provider = DurableProvider(_lease())
    provider.release_error = RuntimeError("network failure")
    monkeypatch.setenv("SEKAI_ACCOUNT_LEASE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(shared_client._lifecycle, "region", "tw")
    monkeypatch.setattr(shared_client, "_account_provider", provider)
    monkeypatch.setattr(shared_client, "_active_account_lease", None)
    monkeypatch.setattr(shared_client, "_active_lease_operation", None)
    shared_client.get_account_info()

    shared_client.release_active_account_lease()

    restored = shared_client.LeaseJournal(tmp_path).load("tw", "shared-client-tw")
    assert restored is not None and restored.release_pending is True
