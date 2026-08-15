"""Unit tests for shared client scheduled login behavior."""

import logging
import os
import queue
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread
from unittest.mock import Mock, patch

import pytest
from jsonrpc.exceptions import JSONRPCDispatchException, JSONRPCInternalError

import shared_client
from accounts import AccountLease, AccountRegion, JpEnCredential
from api_client import AuthTransition, AuthTransitionKind
from utils import deadline as deadline_module
from utils.deadline import (
    Deadline,
    DeadlineExceeded,
    reset_current_deadline,
    set_current_deadline,
)


@pytest.fixture
def reset_lifecycle(monkeypatch):
    monkeypatch.setattr(shared_client, "_active_lease_operation", None)
    runtime = shared_client._lifecycle
    with runtime.lock:
        old = (
            runtime.client,
            runtime.authenticated,
            deepcopy(runtime.user),
            runtime.state,
            runtime.error,
            runtime.retry_until_mono,
            runtime.failure_count,
            runtime.last_attempt_mono,
            runtime.last_attempt_at,
            runtime.next_retry_at,
            runtime.hidden_auth_failure_pending,
            runtime.active_auth_transaction_id,
            runtime.auth_generation,
        )
        runtime.client = None
        runtime.authenticated = False
        runtime.user = None
        runtime.state = shared_client.LifecycleState.UNINITIALIZED
        runtime.error = None
        runtime.retry_until_mono = 0.0
        runtime.failure_count = 0
        runtime.last_attempt_mono = None
        runtime.last_attempt_at = None
        runtime.next_retry_at = None
        runtime.hidden_auth_failure_pending = False
        runtime.active_auth_transaction_id = None
        runtime.auth_generation = 0
        shared_client._publish_snapshot_locked()
    yield
    with runtime.lock:
        (
            runtime.client,
            runtime.authenticated,
            runtime.user,
            runtime.state,
            runtime.error,
            runtime.retry_until_mono,
            runtime.failure_count,
            runtime.last_attempt_mono,
            runtime.last_attempt_at,
            runtime.next_retry_at,
            runtime.hidden_auth_failure_pending,
            runtime.active_auth_transaction_id,
            runtime.auth_generation,
        ) = old
        shared_client._publish_snapshot_locked()


@pytest.fixture
def logged_in_client(reset_lifecycle, monkeypatch):
    client = Mock()
    client.headers = {"x-session-token": "active-token", "x-app-version": "1.0"}
    client.account_info = {"userId": "current-user"}
    client.version_info = {"dataVersion": "current-data"}
    client.master_split_paths = ["current-path"]
    client.user_info = {"name": "current-user"}

    with shared_client._lifecycle.lock:
        shared_client._lifecycle.client = client
        shared_client._lifecycle.authenticated = True
        shared_client._lifecycle.user = {"name": "current-user"}
        shared_client._lifecycle.state = shared_client.LifecycleState.READY
        shared_client._publish_snapshot_locked()
    monkeypatch.setattr(
        shared_client, "get_account_info", lambda: {"userId": "replacement-user"}
    )
    return client


def test_failed_forced_login_restores_active_session(monkeypatch, logged_in_client):
    def fail_after_clearing_session():
        logged_in_client.headers.pop("x-session-token")
        logged_in_client.version_info = {"dataVersion": "replacement-data"}
        logged_in_client.master_split_paths = ["replacement-path"]
        logged_in_client.user_info = {"name": "replacement-user"}
        raise RuntimeError("daily refresh failed")

    logged_in_client.login.side_effect = fail_after_clearing_session
    day_change_job = Mock()
    monkeypatch.setattr(shared_client, "day_change_job", day_change_job)

    with (
        pytest.raises(RuntimeError, match="daily refresh failed"),
    ):
        shared_client.login_account(True)

    assert logged_in_client.headers["x-session-token"] == "active-token"
    assert logged_in_client.account_info == {"userId": "current-user"}
    assert logged_in_client.version_info == {"dataVersion": "current-data"}
    assert logged_in_client.master_split_paths == ["current-path"]
    assert logged_in_client.user_info == {"name": "current-user"}
    assert shared_client.user_info == {"name": "current-user"}
    day_change_job.pause.assert_called_once_with()
    day_change_job.resume.assert_called_once_with()


def test_injected_provider_lease_is_released_when_login_fails(
    monkeypatch, logged_in_client
):
    lease = AccountLease(
        "lease-new",
        "shared-client-jp",
        datetime.now(UTC) + timedelta(minutes=5),
        JpEnCredential(AccountRegion.JP, "new-user", "credential", "signature"),
    )
    provider = Mock()
    provider.acquire.return_value = lease
    monkeypatch.setattr(shared_client, "_account_provider", provider)
    monkeypatch.setattr(shared_client, "_active_account_lease", None)
    monkeypatch.setattr(shared_client, "day_change_job", Mock())
    # Exercise the provider-backed compatibility entry point from login_account.
    monkeypatch.undo()
    monkeypatch.setattr(shared_client, "_account_provider", provider)
    monkeypatch.setattr(shared_client, "_active_account_lease", None)
    monkeypatch.setattr(shared_client, "day_change_job", Mock())
    logged_in_client.login.side_effect = RuntimeError("login failed")

    with pytest.raises(RuntimeError, match="login failed"):
        shared_client.login_account(True)

    provider.acquire.assert_called_once()
    provider.release.assert_called_once_with("lease-new")
    assert shared_client._active_account_lease is None


def test_release_failure_does_not_mask_login_failure(monkeypatch, logged_in_client):
    lease = AccountLease(
        "lease-new",
        "shared-client-jp",
        datetime.now(UTC) + timedelta(minutes=5),
        JpEnCredential(AccountRegion.JP, "new-user", "credential", "signature"),
    )
    provider = Mock()
    provider.release.side_effect = RuntimeError("release failed")
    monkeypatch.setattr(shared_client, "_account_provider", provider)
    monkeypatch.setattr(shared_client, "_active_account_lease", None)
    monkeypatch.setattr(shared_client, "day_change_job", Mock())

    def acquire_account():
        shared_client._active_account_lease = lease
        return {"userId": "new-user"}

    monkeypatch.setattr(shared_client, "get_account_info", acquire_account)
    logged_in_client.login.side_effect = RuntimeError("login failed")

    with pytest.raises(RuntimeError, match="login failed"):
        shared_client.login_account(True)

    provider.release.assert_called_once_with("lease-new")


def test_get_account_info_reuses_live_lease(monkeypatch, reset_lifecycle):
    lease = AccountLease(
        "lease-live",
        "shared-client-jp",
        datetime.now(UTC) + timedelta(minutes=5),
        JpEnCredential(AccountRegion.JP, "user", "credential", "signature"),
    )
    provider = Mock()
    monkeypatch.setattr(shared_client._lifecycle, "region", "jp")
    monkeypatch.setattr(shared_client, "_account_provider", provider)
    monkeypatch.setattr(shared_client, "_active_account_lease", lease)

    first = shared_client.get_account_info()
    second = shared_client.get_account_info()

    assert first == second
    assert first["userId"] == "user"
    provider.acquire.assert_not_called()


def test_get_account_info_reacquires_expired_lease(monkeypatch, reset_lifecycle):
    expired = AccountLease(
        "lease-expired",
        "shared-client-jp",
        datetime.now(UTC) - timedelta(seconds=1),
        JpEnCredential(AccountRegion.JP, "old", "old-credential", "old-signature"),
    )
    replacement = AccountLease(
        "lease-new",
        "shared-client-jp",
        datetime.now(UTC) + timedelta(hours=24),
        JpEnCredential(AccountRegion.JP, "new", "credential", "signature"),
    )
    provider = Mock()
    provider.acquire.return_value = replacement
    monkeypatch.setattr(shared_client._lifecycle, "region", "jp")
    monkeypatch.setattr(shared_client, "_account_provider", provider)
    monkeypatch.setattr(shared_client, "_active_account_lease", expired)

    account_info = shared_client.get_account_info()

    assert account_info["userId"] == "new"
    assert shared_client._active_account_lease is replacement
    provider.acquire.assert_called_once()


def test_graceful_shutdown_release_is_best_effort_and_idempotent(monkeypatch):
    lease = AccountLease(
        "lease-active",
        "shared-client-jp",
        datetime.now(UTC) + timedelta(minutes=5),
        JpEnCredential(AccountRegion.JP, "user", "credential", "signature"),
    )
    provider = Mock()
    monkeypatch.setattr(shared_client, "_account_provider", provider)
    monkeypatch.setattr(shared_client, "_active_account_lease", lease)

    shared_client.release_active_account_lease()
    shared_client.release_active_account_lease()

    provider.release.assert_called_once_with("lease-active")
    assert shared_client._active_account_lease is None


def test_graceful_shutdown_release_failure_does_not_escape(monkeypatch, caplog):
    lease = AccountLease(
        "lease-active",
        "shared-client-jp",
        datetime.now(UTC) + timedelta(minutes=5),
        JpEnCredential(AccountRegion.JP, "user", "credential", "signature"),
    )
    provider = Mock()
    provider.release.side_effect = RuntimeError("sensitive upstream response")
    monkeypatch.setattr(shared_client, "_account_provider", provider)
    monkeypatch.setattr(shared_client, "_active_account_lease", lease)

    shared_client.release_active_account_lease()

    assert "sensitive upstream response" not in caplog.text


def test_nested_hidden_auth_success_survives_outer_login_error(
    monkeypatch, reset_lifecycle
):
    client = Mock()
    client.headers = {"x-session-token": "old-token"}
    client.account_info = {"userId": "old-user"}
    client.version_info = {"dataVersion": "old-data"}
    client.master_split_paths = ["old-path"]
    client.user_info = {}

    with shared_client._lifecycle.lock:
        shared_client._lifecycle.client = client
        shared_client._publish_snapshot_locked()
    shared_client._attach_lifecycle_callback(client)
    monkeypatch.setattr(
        shared_client, "get_account_info", lambda: {"userId": "new-user"}
    )
    monkeypatch.setattr(shared_client, "day_change_job", Mock())
    monkeypatch.setattr(shared_client, "run_job", lambda job: job())

    def hidden_auth_then_outer_error():
        client.headers["x-session-token"] = "new-token"
        client.user_info = {"name": "new-user"}
        client.lifecycle_callback(AuthTransition(1, AuthTransitionKind.ATTEMPT))
        client.lifecycle_callback(AuthTransition(1, AuthTransitionKind.SUCCESS))
        raise RuntimeError("ordinary outer operation failed")

    client.login.side_effect = hidden_auth_then_outer_error

    with pytest.raises(RuntimeError, match="ordinary outer operation failed"):
        shared_client.login()

    with shared_client._lifecycle.lock:
        assert shared_client._lifecycle.state is shared_client.LifecycleState.READY
        assert shared_client._lifecycle.authenticated is True
        assert shared_client._lifecycle.user == {"name": "new-user"}
        assert shared_client._lifecycle.failure_count == 0
        assert shared_client._lifecycle.retry_until_mono == 0.0
    assert client.headers["x-session-token"] == "new-token"
    assert client.user_info == {"name": "new-user"}


def test_day_change_logs_queued_relogin_failure(monkeypatch, caplog, reset_lifecycle):
    with shared_client._lifecycle.lock:
        shared_client._lifecycle.authenticated = True
        shared_client._lifecycle.client = Mock()
        shared_client._lifecycle.state = shared_client.LifecycleState.READY
        shared_client._publish_snapshot_locked()
    error = JSONRPCDispatchException(
        code=JSONRPCInternalError.CODE,
        message=JSONRPCInternalError.MESSAGE,
        data="daily refresh failed",
    )

    with patch.object(shared_client, "_client_job", side_effect=error) as client_job:
        caplog.set_level(logging.ERROR, logger=shared_client.__name__)
        shared_client.day_change_func()

    client_job.assert_called_once()
    assert "Scheduled daily relogin failed: daily refresh failed" in caplog.text


def test_failed_split_path_refresh_restores_active_session(
    monkeypatch, logged_in_client
):
    def fail_after_clearing_session():
        logged_in_client.headers.pop("x-session-token")
        logged_in_client.master_split_paths = ["replacement-path"]
        raise RuntimeError("split path refresh failed")

    logged_in_client.refresh_master_split_paths.side_effect = (
        fail_after_clearing_session
    )
    monkeypatch.setattr(shared_client, "run_job", lambda job: job())

    with pytest.raises(RuntimeError, match="split path refresh failed"):
        shared_client.refresh_master_split_paths()

    assert logged_in_client.headers["x-session-token"] == "active-token"
    assert logged_in_client.master_split_paths == ["current-path"]


def test_run_job_raises_serializable_dispatch_error(monkeypatch):
    response_queue = queue.Queue()
    response_queue.put(RuntimeError("api failed"))
    monkeypatch.setattr(
        shared_client, "enqueue_job", lambda job: (response_queue, None)
    )

    with pytest.raises(JSONRPCDispatchException) as raised:
        shared_client.run_job(lambda: None)

    assert raised.value.error.data == "api failed"


def test_rpc_worker_error_is_returned_as_jsonrpc_error(monkeypatch, reset_lifecycle):
    client = Mock()
    with shared_client._lifecycle.lock:
        shared_client._lifecycle.client = client
        shared_client._publish_snapshot_locked()
    monkeypatch.setattr(shared_client, "start_scheduler", lambda: None)
    monkeypatch.setattr(shared_client.Config, "get_internal_rpc_token", lambda: "tok")
    monkeypatch.setattr(
        shared_client.Config, "allow_insecure_internal_rpc", lambda: False
    )

    response_queue = queue.Queue()
    response_queue.put(RuntimeError("request failed"))
    monkeypatch.setattr(
        shared_client, "enqueue_job", lambda job: (response_queue, None)
    )

    response = shared_client.app.test_client().post(
        "/",
        json={"jsonrpc": "2.0", "method": "fetch_system_data", "id": 1},
        headers={"x-internal-rpc-token": "tok"},
    )

    assert response.status_code == 200
    assert response.json["error"]["code"] == JSONRPCInternalError.CODE
    assert response.json["error"]["data"] == "request failed"


def test_already_logged_in_returns_cache_without_relogin(monkeypatch, logged_in_client):
    day_change_job = Mock()
    monkeypatch.setattr(shared_client, "day_change_job", day_change_job)

    result = shared_client.login_account()

    assert result == {"name": "current-user"}
    logged_in_client.login.assert_not_called()
    day_change_job.pause.assert_not_called()
    day_change_job.resume.assert_not_called()


def test_enqueue_job_rejects_with_error_when_queue_full(monkeypatch):
    full_queue: queue.Queue = queue.Queue(maxsize=1)
    full_queue.put(object())
    monkeypatch.setattr(shared_client, "job_queue", full_queue)

    response_queue, error = shared_client.enqueue_job(lambda: None)

    assert response_queue is None
    assert isinstance(error, JSONRPCInternalError)
    assert error.data == {
        "code": "queue_full",
        "retryable": True,
        "retry_after": 1,
    }


def test_account_info_rpc_returns_only_userid_and_region(monkeypatch, reset_lifecycle):
    client = Mock()
    client.account_info = {"userId": "u1", "credential": "C", "signature": "S"}
    with shared_client._lifecycle.lock:
        shared_client._lifecycle.client = client
        shared_client._lifecycle.authenticated = True
        shared_client._lifecycle.state = shared_client.LifecycleState.READY
        shared_client._publish_snapshot_locked()

    result = shared_client.account_info()

    assert result == {"userId": "u1", "region": "jp"}
    # Credential/signature must never cross the RPC boundary.
    assert "credential" not in result
    assert "signature" not in result


def test_fetch_master_split_rejects_unallowlisted_path(monkeypatch, reset_lifecycle):
    client = Mock()
    client.master_split_paths = ["suite/master/valid"]
    with shared_client._lifecycle.lock:
        shared_client._lifecycle.client = client
        shared_client._lifecycle.authenticated = True
        shared_client._lifecycle.state = shared_client.LifecycleState.READY

    with pytest.raises(JSONRPCDispatchException):
        shared_client.fetch_master_split("suite/master/evil")


def test_ordinary_api_failure_preserves_ready_and_retry_gate(reset_lifecycle):
    client = Mock()
    client.master_split_paths = ["suite/master/valid"]
    with shared_client._lifecycle.lock:
        shared_client._lifecycle.client = client
        shared_client._lifecycle.authenticated = True
        shared_client._lifecycle.state = shared_client.LifecycleState.READY
        shared_client._publish_snapshot_locked()
    before = shared_client.lifecycle_status()

    with pytest.raises(JSONRPCDispatchException):
        shared_client.fetch_master_split("suite/master/evil")

    after = shared_client.lifecycle_status()
    assert after["state"] == shared_client.LifecycleState.READY
    assert after["ready"] is True
    assert after["next_retry_at"] == before["next_retry_at"]


def test_client_job_does_not_block_lifecycle_reads(reset_lifecycle, monkeypatch):
    started = Event()
    release = Event()
    client = Mock()
    with shared_client._lifecycle.lock:
        shared_client._lifecycle.client = client
        shared_client._publish_snapshot_locked()

    monkeypatch.setattr(shared_client, "run_job", lambda job: job())

    def blocking_job():
        started.set()
        assert release.wait(timeout=2)
        return "done"

    worker = Thread(target=shared_client._client_job, args=(blocking_job,))
    worker.start()
    assert started.wait(timeout=2)

    assert shared_client.readiness()["initialized"] is True
    assert shared_client.lifecycle_status()["initialized"] is True

    release.set()
    worker.join(timeout=2)
    assert not worker.is_alive()


def test_status_timestamps_are_utc_strings_and_retry_uses_monotonic_gate(
    monkeypatch, reset_lifecycle
):
    wall_clock = datetime(2026, 7, 23, 12, 34, 56, 789000).astimezone()
    monkeypatch.setattr(shared_client, "_utc_now", lambda: wall_clock)
    monkeypatch.setattr(shared_client, "monotonic", lambda: 100.0)
    with shared_client._lifecycle.lock:
        shared_client._lifecycle.mark_attempt()
        shared_client._lifecycle.record_failure(
            RuntimeError("auth failed"), shared_client.LifecycleState.DEGRADED
        )

    status = shared_client.lifecycle_status()
    parsed_last = datetime.fromisoformat(
        status["last_attempt_at"].replace("Z", "+00:00")
    )
    parsed_next = datetime.fromisoformat(status["next_retry_at"].replace("Z", "+00:00"))
    assert parsed_last == wall_clock
    assert parsed_next > parsed_last
    assert isinstance(status["last_attempt_at"], str)
    assert isinstance(status["next_retry_at"], str)
    assert "100.0" not in str(status)


def test_authentication_failure_degrades_and_sets_retry_gate(
    monkeypatch, logged_in_client
):
    logged_in_client.login.side_effect = RuntimeError("authentication failed")
    monkeypatch.setattr(shared_client, "day_change_job", Mock())

    with pytest.raises(RuntimeError, match="authentication failed"):
        shared_client.login_account(True)

    status = shared_client.lifecycle_status()
    assert status["state"] == shared_client.LifecycleState.DEGRADED
    assert status["next_retry_at"] is not None


def test_fetch_master_split_allowlisted_calls_client(monkeypatch, reset_lifecycle):
    client = Mock()
    client.master_split_paths = ["suite/master/valid"]
    client.call_pjsk_api.return_value = {"k": "v"}
    with shared_client._lifecycle.lock:
        shared_client._lifecycle.client = client
        shared_client._lifecycle.authenticated = True
        shared_client._lifecycle.state = shared_client.LifecycleState.READY

    result = shared_client.fetch_master_split("suite/master/valid")

    assert result == {"k": "v"}
    client.call_pjsk_api.assert_called_once_with("/suite/master/valid")


def test_generic_call_pjsk_api_disabled_by_default(monkeypatch):
    monkeypatch.setattr(shared_client.Config, "enable_unsafe_pjsk_rpc", lambda: False)

    with pytest.raises(RuntimeError, match="disabled"):
        shared_client.call_pjsk_api("/suite/master")


def test_generic_call_pjsk_api_enabled_with_flag(monkeypatch, reset_lifecycle):
    monkeypatch.setattr(shared_client.Config, "enable_unsafe_pjsk_rpc", lambda: True)
    client = Mock()
    client.call_pjsk_api.return_value = {"ok": True}
    with shared_client._lifecycle.lock:
        shared_client._lifecycle.client = client
        shared_client._publish_snapshot_locked()

    result = shared_client.call_pjsk_api("/suite/master")
    assert result == {"ok": True}


def test_mismatched_region_is_rejected(reset_lifecycle):
    with pytest.raises(ValueError, match="Region mismatch"):
        shared_client.init("en" if shared_client._configured_region == "jp" else "jp")


def test_duplicate_init_is_a_noop_and_preserves_ready(monkeypatch, reset_lifecycle):
    client = Mock()
    with shared_client._lifecycle.lock:
        shared_client._lifecycle.client = client
        shared_client._lifecycle.authenticated = True
        shared_client._lifecycle.user = {"name": "ready"}
        shared_client._lifecycle.state = shared_client.LifecycleState.READY
        shared_client._publish_snapshot_locked()

    with patch.object(shared_client, "APIClient") as api_client:
        assert shared_client._initialize_client() is True
        api_client.assert_not_called()

    assert shared_client.lifecycle_status()["ready"] is True
    assert shared_client.lifecycle_status()["authenticated"] is True


def test_snapshot_publication_is_one_way_after_commit(reset_lifecycle):
    client = Mock()
    with shared_client._lifecycle.lock:
        shared_client._lifecycle.client = client
        shared_client._lifecycle.authenticated = True
        shared_client._lifecycle.user = {"name": "published"}
        shared_client._lifecycle.state = shared_client.LifecycleState.READY
        shared_client._publish_snapshot_locked()

    # Mutating the compatibility snapshot cannot change authoritative state.
    shared_client.user_logged_in = False
    shared_client.user_info = {"name": "stale"}
    assert shared_client.is_login() is True
    assert shared_client.login_user_info() == {"name": "published"}


def test_ensure_ready_backoff_and_recovery_are_serialized(monkeypatch, reset_lifecycle):
    client = Mock()
    client.headers = {"x-session-token": "old"}
    client.account_info = {"userId": "u"}
    client.version_info = {}
    client.master_split_paths = []
    client.user_info = {}
    client.login.side_effect = [RuntimeError("first failure"), {"name": "recovered"}]
    monkeypatch.setattr(shared_client, "get_account_info", lambda: {"userId": "u"})
    monkeypatch.setattr(shared_client, "day_change_job", Mock())
    with shared_client._lifecycle.lock:
        shared_client._lifecycle.client = client
        shared_client._lifecycle.state = shared_client.LifecycleState.DEGRADED
        shared_client._lifecycle.retry_until_mono = 0.0

    results = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(shared_client.ensure_ready) for _ in range(2)]
        results = [future.result() for future in futures]
    assert client.login.call_count == 1
    assert any(result["attempted"] for result in results)

    with shared_client._lifecycle.lock:
        shared_client._lifecycle.retry_until_mono = 0.0
    recovered = shared_client.ensure_ready()
    assert recovered["ready"] is True
    assert client.login.call_count == 2


def test_hidden_auth_callback_success_and_failure(reset_lifecycle):
    client = Mock()
    client.user_info = {"name": "hidden-success"}
    with shared_client._lifecycle.lock:
        shared_client._lifecycle.client = client
        shared_client._lifecycle.state = shared_client.LifecycleState.REAUTHENTICATING
    shared_client._api_lifecycle_transition(
        AuthTransition(1, AuthTransitionKind.ATTEMPT)
    )
    shared_client._api_lifecycle_transition(
        AuthTransition(1, AuthTransitionKind.SUCCESS)
    )
    assert shared_client.lifecycle_status()["ready"] is True
    assert shared_client.user_info == {"name": "hidden-success"}

    shared_client._api_lifecycle_transition(
        AuthTransition(2, AuthTransitionKind.ATTEMPT)
    )
    shared_client._api_lifecycle_transition(
        AuthTransition(2, AuthTransitionKind.FAILURE, RuntimeError("token=secret"))
    )
    status = shared_client.lifecycle_status()
    assert status["state"] == shared_client.LifecycleState.DEGRADED
    assert "secret" not in str(status["error"])


def test_hidden_auth_callback_records_fresh_attempt_on_success_and_failure(
    monkeypatch, reset_lifecycle
):
    monotonic_values = iter([10.0, 10.0, 20.0, 20.0, 20.0])
    wall_values = iter(
        [
            datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC),
            datetime(2026, 7, 23, 12, 0, 1, tzinfo=UTC),
            datetime(2026, 7, 23, 12, 0, 2, tzinfo=UTC),
        ]
    )
    monkeypatch.setattr(shared_client, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(shared_client, "_utc_now", lambda: next(wall_values))

    shared_client._api_lifecycle_transition(
        AuthTransition(1, AuthTransitionKind.ATTEMPT)
    )
    first = shared_client.lifecycle_status()["last_attempt_at"]
    shared_client._api_lifecycle_transition(
        AuthTransition(2, AuthTransitionKind.ATTEMPT)
    )
    shared_client._api_lifecycle_transition(
        AuthTransition(
            2, AuthTransitionKind.FAILURE, RuntimeError("hidden authentication failed")
        )
    )
    second = shared_client.lifecycle_status()["last_attempt_at"]
    assert first != second
    assert shared_client._lifecycle.last_attempt_mono == 20.0


def test_slow_failure_deadline_uses_failure_instant(monkeypatch, reset_lifecycle):
    monotonic_values = iter([100.0, 150.0])
    wall_values = iter(
        [
            datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC),
            datetime(2026, 7, 23, 12, 0, 50, tzinfo=UTC),
        ]
    )
    monkeypatch.setattr(shared_client, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(shared_client, "_utc_now", lambda: next(wall_values))
    with shared_client._lifecycle.lock:
        shared_client._lifecycle.mark_attempt()
        shared_client._lifecycle.record_failure(
            RuntimeError("slow authentication failed"),
            shared_client.LifecycleState.DEGRADED,
        )
    monkeypatch.setattr(shared_client, "monotonic", lambda: 150.0)
    status = shared_client.lifecycle_status()
    deadline = datetime.fromisoformat(status["next_retry_at"].replace("Z", "+00:00"))
    failure_time = datetime(2026, 7, 23, 12, 0, 50, tzinfo=UTC)
    assert deadline == failure_time + timedelta(
        seconds=shared_client.Config.LIFECYCLE_RETRY_BASE_SECONDS
    )
    assert status["retry_after"] == pytest.approx(
        shared_client.Config.LIFECYCLE_RETRY_BASE_SECONDS
    )


def test_scheduled_relogin_uses_lifecycle_barrier(monkeypatch, reset_lifecycle):
    with shared_client._lifecycle.lock:
        shared_client._lifecycle.client = Mock()
        shared_client._lifecycle.authenticated = True
        shared_client._lifecycle.state = shared_client.LifecycleState.READY
        shared_client._publish_snapshot_locked()
    with patch.object(shared_client, "_client_job") as client_job:
        shared_client.day_change_func()
    client_job.assert_called_once()


def test_expired_job_restores_client_and_lifecycle_state(monkeypatch, logged_in_client):
    now = 100.0
    monkeypatch.setattr(deadline_module, "monotonic", lambda: now)
    monkeypatch.setattr(shared_client, "run_job", lambda job: job())
    deadline = Deadline.after(1.0)
    token = set_current_deadline(deadline)

    def late_result():
        nonlocal now
        logged_in_client.headers["x-session-token"] = "late-token"
        logged_in_client.user_info = {"name": "late-user"}
        with shared_client._lifecycle.lock:
            shared_client._lifecycle.user = {"name": "late-user"}
            shared_client._lifecycle.auth_generation += 1
            shared_client._lifecycle.state = shared_client.LifecycleState.DEGRADED
            shared_client._publish_snapshot_locked()
        now = 102.0
        return {"name": "late-user"}

    try:
        with pytest.raises(DeadlineExceeded, match="deadline exceeded"):
            shared_client._client_job(late_result)
    finally:
        reset_current_deadline(token)

    assert logged_in_client.headers["x-session-token"] == "active-token"
    assert logged_in_client.user_info == {"name": "current-user"}
    with shared_client._lifecycle.lock:
        assert shared_client._lifecycle.user == {"name": "current-user"}
        assert shared_client._lifecycle.auth_generation == 0
        assert shared_client._lifecycle.state is shared_client.LifecycleState.READY
    assert shared_client.user_info == {"name": "current-user"}


def test_expired_initialization_cannot_replace_committed_client(
    monkeypatch, logged_in_client
):
    now = 200.0
    monkeypatch.setattr(deadline_module, "monotonic", lambda: now)
    monkeypatch.setattr(shared_client, "run_job", lambda job: job())
    deadline = Deadline.after(1.0)
    token = set_current_deadline(deadline)
    late_client = Mock()

    def late_initialization():
        nonlocal now
        with shared_client._lifecycle.lock:
            shared_client._lifecycle.client = late_client
            shared_client._lifecycle.authenticated = False
            shared_client._lifecycle.user = None
            shared_client._lifecycle.state = shared_client.LifecycleState.INITIALIZING
            shared_client._publish_snapshot_locked()
        now = 202.0
        return True

    try:
        with pytest.raises(DeadlineExceeded, match="deadline exceeded"):
            shared_client._client_job(
                late_initialization, shared_client._ClientOperation.INITIALIZATION
            )
    finally:
        reset_current_deadline(token)

    with shared_client._lifecycle.lock:
        assert shared_client._lifecycle.client is logged_in_client
        assert shared_client._lifecycle.authenticated is True
        assert shared_client._lifecycle.state is shared_client.LifecycleState.READY
    assert shared_client.api_client is logged_in_client


def test_expired_hidden_auth_callback_cannot_publish(monkeypatch, logged_in_client):
    now = 300.0
    monkeypatch.setattr(deadline_module, "monotonic", lambda: now)
    deadline = Deadline.after(1.0)
    token = set_current_deadline(deadline)
    now = 302.0
    logged_in_client.user_info = {"name": "late-user"}

    try:
        shared_client._api_lifecycle_transition(
            AuthTransition(1, AuthTransitionKind.ATTEMPT)
        )
        shared_client._api_lifecycle_transition(
            AuthTransition(1, AuthTransitionKind.SUCCESS)
        )
    finally:
        reset_current_deadline(token)

    with shared_client._lifecycle.lock:
        assert shared_client._lifecycle.active_auth_transaction_id is None
        assert shared_client._lifecycle.auth_generation == 0
        assert shared_client._lifecycle.user == {"name": "current-user"}
        assert shared_client._lifecycle.state is shared_client.LifecycleState.READY


def test_readiness_exposes_secret_free_queue_metrics(reset_lifecycle):
    status = shared_client.readiness()

    assert status["queue"]["capacity"] == 1
    assert isinstance(status["queue"]["depth"], int)
    assert "accepted_total" in status["queue"]
    assert "execution_seconds_total" in status["queue"]


@pytest.mark.parametrize("configured", ["", "not-a-region"])
def test_invalid_region_fails_startup_in_isolated_process(configured):
    env = os.environ.copy()
    env.pop("SEKAI_REGION", None)
    if configured:
        env["SEKAI_REGION"] = configured
    result = subprocess.run(
        [sys.executable, "-c", "import shared_client"],
        cwd=str(Path(__file__).parents[1]),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "SEKAI_REGION" in (result.stderr + result.stdout)
