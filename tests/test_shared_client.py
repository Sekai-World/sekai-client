"""Unit tests for shared client scheduled login behavior."""

import logging
from unittest.mock import Mock, patch

import pytest
from jsonrpc.exceptions import JSONRPCInternalError

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
    error = JSONRPCInternalError(data="daily refresh failed")

    with patch.object(shared_client, "run_job", return_value=error) as run_job:
        caplog.set_level(logging.ERROR, logger=shared_client.__name__)
        shared_client.day_change_func()

    run_job.assert_called_once()
    assert "Scheduled daily relogin failed: daily refresh failed" in caplog.text
