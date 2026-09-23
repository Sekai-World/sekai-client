import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import accounts.lease_journal as lease_journal_module
import gunicorn_conf
import shared_client
from accounts import AccountLease, AccountRegion, TwKrCredential
from accounts.provider import AccountProviderError, InvalidLeaseError


class DurableProvider:
    requires_durable_idempotency = True

    def __init__(self, lease):
        self.lease = lease
        self.acquire_keys = []
        self.acquire_ttls = []
        self.release_calls = []
        self.fail_once = False
        self.release_error = None
        self.renew_calls = []
        self.renew_error = None
        self.renew_expiry = datetime.now(UTC) + timedelta(hours=24)

    def acquire(self, region, consumer, *, ttl_seconds, idempotency_key):
        self.acquire_keys.append(idempotency_key)
        self.acquire_ttls.append(ttl_seconds)
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
    assert provider.acquire_ttls == [
        shared_client._ACCOUNT_LEASE_TTL_SECONDS,
        shared_client._ACCOUNT_LEASE_TTL_SECONDS,
    ]
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
    assert provider.renew_calls[0][1] == shared_client._ACCOUNT_LEASE_TTL_SECONDS
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


class FakeClock:
    def __init__(self, now):
        self.current_time = now

    def now(self):
        return self.current_time

    def advance(self, duration):
        self.current_time += duration


class FakeAccountService:
    """Idempotent service fake that makes expired leases available again."""

    requires_durable_idempotency = True

    def __init__(self, clock):
        self.clock = clock
        self.acquire_calls = []
        self.renew_calls = []
        self.leases_by_key = {}
        self.next_lease_number = 0
        self.credential = TwKrCredential(
            AccountRegion.TW,
            "open-id",
            "token",
            "device-id",
            "install-id",
            "user-agent",
            "device-model",
            "os-version",
        )

    def acquire(self, region, consumer, *, ttl_seconds, idempotency_key):
        self.acquire_calls.append((region, consumer, ttl_seconds, idempotency_key))
        existing = self.leases_by_key.get(idempotency_key)
        if existing is not None and not existing.is_expired(self.clock.now()):
            return existing

        self.next_lease_number += 1
        lease = AccountLease(
            f"service-lease-{self.next_lease_number}",
            consumer,
            self.clock.now() + timedelta(seconds=ttl_seconds),
            self.credential,
        )
        self.leases_by_key[idempotency_key] = lease
        return lease

    def renew(self, lease_id, *, extend_seconds, idempotency_key):
        self.renew_calls.append((lease_id, extend_seconds, idempotency_key))
        return self.clock.now() + timedelta(seconds=extend_seconds)

    def release(self, lease_id):
        del lease_id

    def report_invalid(self, lease_id, reason):
        del lease_id, reason


@pytest.fixture
def fake_clock(monkeypatch):
    clock = FakeClock(datetime(2030, 1, 1, tzinfo=UTC))

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            now = clock.now()
            return now if tz is not None else now.replace(tzinfo=None)

    monkeypatch.setattr(shared_client, "_utc_now", clock.now)
    monkeypatch.setattr(lease_journal_module, "datetime", FakeDateTime)
    return clock


def _configure_service(service, monkeypatch, tmp_path):
    monkeypatch.setenv("SEKAI_ACCOUNT_LEASE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(shared_client._lifecycle, "region", "tw")
    monkeypatch.setattr(shared_client, "_account_provider", service)
    monkeypatch.setattr(shared_client, "_active_account_lease", None)
    monkeypatch.setattr(shared_client, "_active_lease_operation", None)


def test_renewal_ahead_is_stable_bounded_and_spread():
    lease_ids = [f"regional-lease-{number}" for number in range(20)]
    renewal_ahead = [
        shared_client._lease_renewal_ahead(lease_id) for lease_id in lease_ids
    ]

    assert all(
        shared_client._LEASE_RENEW_AHEAD_MIN
        <= delay
        <= shared_client._LEASE_RENEW_AHEAD_MAX
        for delay in renewal_ahead
    )
    assert renewal_ahead == [
        shared_client._lease_renewal_ahead(lease_id) for lease_id in lease_ids
    ]
    assert len(set(renewal_ahead)) > 1
    assert max(renewal_ahead) - min(renewal_ahead) >= timedelta(minutes=10)


def test_renewal_starts_at_stable_lease_specific_boundary(
    tmp_path, monkeypatch, fake_clock
):
    provider = FakeAccountService(fake_clock)
    expires_at = fake_clock.now() + timedelta(hours=2)
    lease = _lease("boundary-lease", expires_at=expires_at)
    _prepare_active_lease(provider, monkeypatch, tmp_path, lease)
    renewal_at = expires_at - shared_client._lease_renewal_ahead(lease.lease_id)

    fake_clock.advance(renewal_at - fake_clock.now() - timedelta(microseconds=1))
    shared_client.get_account_info()
    assert provider.renew_calls == []

    fake_clock.advance(timedelta(microseconds=1))
    shared_client.get_account_info()
    assert len(provider.renew_calls) == 1


def test_expired_active_lease_is_reacquired_without_renewal(
    tmp_path, monkeypatch, fake_clock
):
    provider = FakeAccountService(fake_clock)
    expires_at = fake_clock.now() + timedelta(hours=1)
    lease = _lease("expired-active-lease", expires_at=expires_at)
    _prepare_active_lease(provider, monkeypatch, tmp_path, lease)
    operation = shared_client._active_lease_operation
    assert operation is not None

    fake_clock.advance(expires_at - fake_clock.now())
    shared_client.get_account_info()

    assert provider.renew_calls == []
    assert len(provider.acquire_calls) == 1
    assert provider.acquire_calls[0][3] != operation.idempotency_key
    assert shared_client._active_account_lease is not None
    assert shared_client._active_account_lease.lease_id != lease.lease_id


def test_simulated_restart_replays_unexpired_journaled_lease_idempotently(
    tmp_path, monkeypatch, fake_clock
):
    provider = FakeAccountService(fake_clock)
    _configure_service(provider, monkeypatch, tmp_path)

    original_info = shared_client.get_account_info()
    original_lease = shared_client._active_account_lease
    original_operation = shared_client._active_lease_operation
    assert original_lease is not None and original_operation is not None

    # Simulate a fresh worker process: in-memory state is gone, journal and
    # account-service idempotency state survive.
    monkeypatch.setattr(shared_client, "_active_account_lease", None)
    monkeypatch.setattr(shared_client, "_active_lease_operation", None)
    replayed_info = shared_client.get_account_info()

    assert replayed_info == original_info
    assert shared_client._active_account_lease is original_lease
    assert len(provider.acquire_calls) == 2
    assert provider.acquire_calls[0][3] == provider.acquire_calls[1][3]
    assert provider.acquire_calls[0][3] == original_operation.idempotency_key


def test_simulated_restart_after_expiry_uses_fresh_acquire_and_recovers(
    tmp_path, monkeypatch, fake_clock
):
    provider = FakeAccountService(fake_clock)
    _configure_service(provider, monkeypatch, tmp_path)

    shared_client.get_account_info()
    original_lease = shared_client._active_account_lease
    original_operation = shared_client._active_lease_operation
    assert original_lease is not None and original_operation is not None

    # Simulate an unclean restart with no in-memory lease. At expiry the service
    # permits a new idempotency operation to claim the recovered account.
    monkeypatch.setattr(shared_client, "_active_account_lease", None)
    monkeypatch.setattr(shared_client, "_active_lease_operation", None)
    fake_clock.advance(original_lease.expires_at - fake_clock.now())
    recovered_info = shared_client.get_account_info()

    recovered_lease = shared_client._active_account_lease
    recovered_operation = shared_client._active_lease_operation
    assert recovered_info["userId"] == "open-id"
    assert recovered_lease is not None
    assert recovered_lease.lease_id != original_lease.lease_id
    assert recovered_operation is not None
    assert recovered_operation.idempotency_key != original_operation.idempotency_key
    assert [call[3] for call in provider.acquire_calls] == [
        original_operation.idempotency_key,
        recovered_operation.idempotency_key,
    ]
    assert provider.renew_calls == []


def test_lease_journal_replays_unexpired_operation_across_processes():
    with tempfile.TemporaryDirectory() as journal_dir:
        journal = shared_client.LeaseJournal(journal_dir)
        operation = journal.load_or_create("tw", "shared-client-tw")
        operation = journal.mark_acquired(
            operation,
            "subprocess-lease",
            datetime.now(UTC) + timedelta(hours=1),
        )
        script = """
import sys
from accounts.lease_journal import LeaseJournal

operation = LeaseJournal(sys.argv[1]).load_or_create("tw", "shared-client-tw")
print(operation.idempotency_key)
print(operation.lease_id)
"""
        result = subprocess.run(
            [sys.executable, "-c", script, journal_dir],
            check=True,
            capture_output=True,
            text=True,
            cwd=Path(__file__).resolve().parents[1],
        )

    assert operation.lease_id == "subprocess-lease"
    assert result.stdout.splitlines() == [operation.idempotency_key, operation.lease_id]
