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
        self.renew_calls = []
        self.renew_error = None
        self.renew_expiry = datetime.now(UTC) + timedelta(hours=24)

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

    def report_invalid(self, lease_id, reason):
        del lease_id, reason

    def renew(self, lease_id, *, extend_seconds, idempotency_key):
        self.renew_calls.append((lease_id, extend_seconds, idempotency_key))
        if self.renew_error:
            raise self.renew_error
        return self.renew_expiry


@pytest.fixture(autouse=True)
def reset_lease_renewal_retry_until(monkeypatch):
    monkeypatch.setattr(shared_client, "_lease_renewal_retry_until", None)


def _lease(lease_id="lease-1", *, expires_at=None):
    return AccountLease(
        lease_id,
        "shared-client-tw",
        expires_at or datetime.now(UTC) + timedelta(minutes=5),
        TwKrCredential(
            AccountRegion.TW,
            "open-id",
            "token",
            "device-id",
            "install-id",
            "user-agent",
            "device-model",
            "os-version",
        ),
    )


def _prepare_active_lease(provider, monkeypatch, tmp_path, lease):
    monkeypatch.setenv("SEKAI_ACCOUNT_LEASE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(shared_client._lifecycle, "region", "tw")
    monkeypatch.setattr(shared_client, "_account_provider", provider)
    monkeypatch.setattr(shared_client, "_active_account_lease", lease)
    operation = shared_client.LeaseJournal(tmp_path).load_or_create(
        "tw", "shared-client-tw"
    )
    operation = shared_client.LeaseJournal(tmp_path).mark_acquired(
        operation, lease.lease_id, lease.expires_at
    )
    monkeypatch.setattr(shared_client, "_active_lease_operation", operation)


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


def test_active_lease_renews_without_reacquiring_and_keeps_credential(
    tmp_path, monkeypatch
):
    lease = _lease(expires_at=datetime.now(UTC) + timedelta(minutes=30))
    provider = DurableProvider(lease)
    _prepare_active_lease(provider, monkeypatch, tmp_path, lease)

    result = shared_client.get_account_info()

    assert result["userId"] == "open-id"
    assert lease.credential is shared_client._active_account_lease.credential
    assert len(provider.renew_calls) == 1
    assert provider.release_calls == []
    assert provider.acquire_keys == []


def test_retryable_renewal_failure_keeps_cached_lease_without_reacquiring(
    tmp_path, monkeypatch
):
    lease = _lease(expires_at=datetime.now(UTC) + timedelta(minutes=30))
    provider = DurableProvider(lease)
    provider.renew_error = AccountProviderError("temporary", retryable=True)
    _prepare_active_lease(provider, monkeypatch, tmp_path, lease)
    operation = shared_client._active_lease_operation

    result = shared_client.get_account_info()

    assert result["userId"] == "open-id"
    assert shared_client._active_account_lease is lease
    assert shared_client._active_lease_operation is operation
    assert provider.acquire_keys == []
    restored = shared_client.LeaseJournal(tmp_path).load("tw", "shared-client-tw")
    assert restored == operation


def test_retryable_renewal_failure_honors_retry_after(tmp_path, monkeypatch):
    lease = _lease(expires_at=datetime.now(UTC) + timedelta(minutes=30))
    provider = DurableProvider(lease)
    provider.renew_error = AccountProviderError(
        "temporary", retryable=True, retry_after=30
    )
    _prepare_active_lease(provider, monkeypatch, tmp_path, lease)

    first = shared_client.get_account_info()
    second = shared_client.get_account_info()

    assert first == second
    assert len(provider.renew_calls) == 1
    assert shared_client._lease_renewal_retry_until is not None

    provider.renew_error = None
    monkeypatch.setattr(
        shared_client,
        "_lease_renewal_retry_until",
        datetime.now(UTC) - timedelta(seconds=1),
    )
    shared_client.get_account_info()

    assert len(provider.renew_calls) == 2


def test_retryable_renewal_failure_without_retry_after_uses_default_floor(
    tmp_path, monkeypatch
):
    lease = _lease(expires_at=datetime.now(UTC) + timedelta(minutes=30))
    provider = DurableProvider(lease)
    provider.renew_error = AccountProviderError("temporary", retryable=True)
    _prepare_active_lease(provider, monkeypatch, tmp_path, lease)

    shared_client.get_account_info()
    shared_client.get_account_info()

    assert len(provider.renew_calls) == 1
    assert shared_client._lease_renewal_retry_until is not None
    assert shared_client._lease_renewal_retry_until >= datetime.now(UTC) + timedelta(
        seconds=59
    )


def test_successful_renewal_clears_retry_deadline(tmp_path, monkeypatch):
    lease = _lease(expires_at=datetime.now(UTC) + timedelta(minutes=30))
    provider = DurableProvider(lease)
    _prepare_active_lease(provider, monkeypatch, tmp_path, lease)
    monkeypatch.setattr(
        shared_client,
        "_lease_renewal_retry_until",
        datetime.now(UTC) + timedelta(minutes=5),
    )
    shared_client.get_account_info()
    assert provider.renew_calls == []

    monkeypatch.setattr(
        shared_client,
        "_lease_renewal_retry_until",
        datetime.now(UTC) - timedelta(seconds=1),
    )

    shared_client.get_account_info()

    assert shared_client._lease_renewal_retry_until is None


def test_diverged_renewal_journal_reacquires(tmp_path, monkeypatch):
    lease = _lease(expires_at=datetime.now(UTC) + timedelta(minutes=30))
    replacement = _lease("lease-new")
    provider = DurableProvider(replacement)
    _prepare_active_lease(provider, monkeypatch, tmp_path, lease)
    monkeypatch.setattr(
        shared_client.LeaseJournal,
        "mark_renewed",
        lambda self, operation, expires_at: None,
    )

    result = shared_client.get_account_info()

    assert result["userId"] == "open-id"
    assert shared_client._active_account_lease is replacement
    assert len(provider.acquire_keys) == 1
    restored = shared_client.LeaseJournal(tmp_path).load("tw", "shared-client-tw")
    assert restored is not None and restored.lease_id == replacement.lease_id


def test_renewal_persists_expiry_and_reuses_key_from_journal(tmp_path, monkeypatch):
    lease = _lease(expires_at=datetime.now(UTC) + timedelta(minutes=30))
    provider = DurableProvider(lease)
    _prepare_active_lease(provider, monkeypatch, tmp_path, lease)
    original_operation = shared_client._active_lease_operation

    shared_client.get_account_info()
    first_key = provider.renew_calls[0][2]
    restored = shared_client.LeaseJournal(tmp_path).load("tw", "shared-client-tw")
    assert restored is not None
    assert restored.expires_at == provider.renew_expiry

    assert original_operation is not None
    shared_client.LeaseJournal(tmp_path).mark_renewed(
        original_operation, lease.expires_at
    )
    monkeypatch.setattr(shared_client, "_active_account_lease", lease)
    monkeypatch.setattr(shared_client, "_active_lease_operation", original_operation)
    shared_client.get_account_info()

    assert provider.renew_calls[1][2] == first_key


def test_renewal_404_reacquires(tmp_path, monkeypatch):
    lease = _lease(expires_at=datetime.now(UTC) + timedelta(minutes=30))
    replacement = _lease("lease-new")
    provider = DurableProvider(replacement)
    provider.renew_error = InvalidLeaseError()
    _prepare_active_lease(provider, monkeypatch, tmp_path, lease)

    result = shared_client.get_account_info()

    assert result["userId"] == "open-id"
    assert len(provider.renew_calls) == 1
    assert len(provider.acquire_keys) == 1
    assert shared_client._active_account_lease is replacement
    restored = shared_client.LeaseJournal(tmp_path).load("tw", "shared-client-tw")
    assert restored is not None
    assert restored.lease_id == replacement.lease_id
    assert restored.expires_at == replacement.expires_at


def test_lease_outside_renew_window_does_not_call_provider(tmp_path, monkeypatch):
    lease = _lease(expires_at=datetime.now(UTC) + timedelta(hours=2))
    provider = DurableProvider(lease)
    _prepare_active_lease(provider, monkeypatch, tmp_path, lease)

    shared_client.get_account_info()

    assert provider.renew_calls == []
    assert provider.acquire_keys == []


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
