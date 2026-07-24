"""Unit tests for shared client scheduled login behavior."""

import logging
import queue
from unittest.mock import Mock, patch

import pytest
from jsonrpc.exceptions import JSONRPCDispatchException, JSONRPCInternalError

import shared_client


@pytest.fixture
def logged_in_client(monkeypatch):
    client = Mock()
    client.headers = {"x-session-token": "active-token", "x-app-version": "1.0"}
    client.account_info = {"userId": "current-user"}
    client.version_info = {"dataVersion": "current-data"}
    client.master_split_paths = ["current-path"]
    client.user_info = {"name": "current-user"}

    monkeypatch.setattr(shared_client, "api_client", client)
    monkeypatch.setattr(shared_client, "user_logged_in", True)
    monkeypatch.setattr(shared_client, "user_info", {"name": "current-user"})
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


def test_day_change_logs_queued_relogin_failure(monkeypatch, caplog):
    monkeypatch.setattr(shared_client, "user_logged_in", True)
    error = JSONRPCDispatchException(
        code=JSONRPCInternalError.CODE,
        message=JSONRPCInternalError.MESSAGE,
        data="daily refresh failed",
    )

    with patch.object(shared_client, "run_job", side_effect=error) as run_job:
        caplog.set_level(logging.ERROR, logger=shared_client.__name__)
        shared_client.day_change_func()

    run_job.assert_called_once()
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


def test_rpc_worker_error_is_returned_as_jsonrpc_error(monkeypatch):
    client = Mock()
    monkeypatch.setattr(shared_client, "api_client", client)
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
    assert "Job queue is full" in error.data


def test_write_account_yaml_atomic_mode_0600_and_cleanup_on_failure(
    monkeypatch, tmp_path
):
    target = tmp_path / "sharedAccount.jp.yaml"

    # Happy path: temp file replaced onto target with 0600 mode.
    shared_client._write_account_yaml_atomic(
        str(target), {"userId": "1", "credential": "c", "signature": "s"}
    )
    assert target.exists()
    assert oct(target.stat().st_mode & 0o777) == "0o600"
    content = target.read_text()
    assert "1" in content  # never asserts the secret values leak in tests
    assert "credential" in content

    # Failure path: exception during dump leaves no temp file behind.
    leftover = list(tmp_path.glob(".sharedAccount.*.tmp"))
    assert leftover == []

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(shared_client, "yaml", type("Y", (), {"safe_dump": boom})())
    with pytest.raises(OSError):
        shared_client._write_account_yaml_atomic(
            str(tmp_path / "sharedAccount.en.yaml"), {"userId": "2"}
        )
    # Temp file was cleaned up on failure.
    assert list(tmp_path.glob(".sharedAccount.*.tmp")) == []


def test_account_info_rpc_returns_only_userid_and_region(monkeypatch):
    client = Mock()
    client.account_info = {"userId": "u1", "credential": "C", "signature": "S"}
    monkeypatch.setattr(shared_client, "api_client", client)
    monkeypatch.setattr(shared_client, "user_logged_in", True)
    monkeypatch.setattr(shared_client, "client_region", "jp")

    result = shared_client.account_info()

    assert result == {"userId": "u1", "region": "jp"}
    # Credential/signature must never cross the RPC boundary.
    assert "credential" not in result
    assert "signature" not in result


def test_fetch_master_split_rejects_unallowlisted_path(monkeypatch):
    client = Mock()
    client.master_split_paths = ["suite/master/valid"]
    monkeypatch.setattr(shared_client, "api_client", client)
    monkeypatch.setattr(shared_client, "user_logged_in", True)

    with pytest.raises(RuntimeError, match="not in the allowlist"):
        shared_client.fetch_master_split("suite/master/evil")


def test_fetch_master_split_allowlisted_calls_client(monkeypatch):
    client = Mock()
    client.master_split_paths = ["suite/master/valid"]
    client.call_pjsk_api.return_value = {"k": "v"}
    monkeypatch.setattr(shared_client, "api_client", client)
    monkeypatch.setattr(shared_client, "user_logged_in", True)

    result = shared_client.fetch_master_split("suite/master/valid")

    assert result == {"k": "v"}
    client.call_pjsk_api.assert_called_once_with("/suite/master/valid")


def test_generic_call_pjsk_api_disabled_by_default(monkeypatch):
    monkeypatch.setattr(shared_client.Config, "enable_unsafe_pjsk_rpc", lambda: False)

    with pytest.raises(RuntimeError, match="disabled"):
        shared_client.call_pjsk_api("/suite/master")


def test_generic_call_pjsk_api_enabled_with_flag(monkeypatch):
    monkeypatch.setattr(shared_client.Config, "enable_unsafe_pjsk_rpc", lambda: True)
    client = Mock()
    client.call_pjsk_api.return_value = {"ok": True}
    monkeypatch.setattr(shared_client, "api_client", client)

    result = shared_client.call_pjsk_api("/suite/master")
    assert result == {"ok": True}
