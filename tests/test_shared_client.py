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

    response_queue = queue.Queue()
    response_queue.put(RuntimeError("request failed"))
    monkeypatch.setattr(
        shared_client, "enqueue_job", lambda job: (response_queue, None)
    )

    response = shared_client.app.test_client().post(
        "/",
        json={"jsonrpc": "2.0", "method": "fetch_system_data", "id": 1},
    )

    assert response.status_code == 200
    assert response.json["error"]["code"] == JSONRPCInternalError.CODE
    assert response.json["error"]["data"] == "request failed"
