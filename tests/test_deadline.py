from unittest.mock import Mock, patch

import pytest

import api_client
import shared_client
from config import Config
from utils import deadline as deadline_module
from utils.deadline import (
    Deadline,
    DeadlineExceeded,
    bounded_timeout,
    reset_current_deadline,
    set_current_deadline,
)
from utils.jsonrpc_client import INTERNAL_RPC_TIMEOUT_HEADER, JSONRPCClient


def test_deadline_uses_monotonic_remaining_budget(monkeypatch):
    now = 10.0
    monkeypatch.setattr(deadline_module, "monotonic", lambda: now)
    deadline = Deadline.after(3.0)

    now = 12.25
    assert deadline.remaining() == pytest.approx(0.75)

    now = 13.0
    with pytest.raises(DeadlineExceeded, match="deadline exceeded"):
        deadline.require_remaining()


def test_bounded_timeout_uses_smaller_remaining_budget(monkeypatch):
    now = 20.0
    monkeypatch.setattr(deadline_module, "monotonic", lambda: now)
    deadline = Deadline.after(2.0)
    token = set_current_deadline(deadline)
    try:
        now = 20.5
        assert bounded_timeout(10.0) == pytest.approx(1.5)
        assert bounded_timeout(1.0) == pytest.approx(1.0)
    finally:
        reset_current_deadline(token)


@patch("requests.post")
def test_jsonrpc_client_sends_timeout_budget_header(mock_post, monkeypatch):
    monkeypatch.setattr(Config, "get_internal_rpc_token", lambda: "test-token")
    response = Mock()
    response.json.return_value = {"jsonrpc": "2.0", "result": "ok", "id": 1}
    mock_post.return_value = response

    JSONRPCClient().request("test", timeout=0.25)

    assert mock_post.call_args.kwargs["headers"][INTERNAL_RPC_TIMEOUT_HEADER] == ("250")


def test_shared_client_caps_remote_budget_to_server_limit():
    with shared_client.app.test_request_context(
        "/", headers={INTERNAL_RPC_TIMEOUT_HEADER: "999999"}
    ):
        deadline = shared_client._new_request_deadline()

    assert deadline.remaining() <= Config.ANSWER_QUEUE_TIMEOUT
    assert deadline.remaining() > Config.ANSWER_QUEUE_TIMEOUT - 1


def test_shared_client_rejects_invalid_remote_budget():
    with shared_client.app.test_request_context(
        "/", headers={INTERNAL_RPC_TIMEOUT_HEADER: "invalid"}
    ):
        with pytest.raises(Exception) as raised:
            shared_client._new_request_deadline()
    assert raised.value.error.data == "Invalid request deadline"


@patch("api_client.requests.request")
def test_game_http_timeout_is_bounded_by_rpc_deadline(mock_request, monkeypatch):
    now = 30.0
    monkeypatch.setattr(deadline_module, "monotonic", lambda: now)
    response = Mock()
    response.headers = {}
    response.status_code = 200
    mock_request.return_value = response
    client = api_client.APIClient("jp")
    deadline = Deadline.after(0.5)
    token = set_current_deadline(deadline)
    try:
        now = 30.1
        client._send_api_request("/system", "get", None)
    finally:
        reset_current_deadline(token)

    assert mock_request.call_args.kwargs["timeout"] == pytest.approx(0.4)


def test_retry_delay_cannot_exceed_remaining_deadline(monkeypatch):
    now = 40.0
    monkeypatch.setattr(deadline_module, "monotonic", lambda: now)
    monkeypatch.setattr(api_client.random, "uniform", lambda low, high: 2.0)
    sleep_mock = Mock()
    monkeypatch.setattr(api_client, "sleep", sleep_mock)
    client = api_client.APIClient("jp")
    deadline = Deadline.after(1.0)
    token = set_current_deadline(deadline)
    try:
        with pytest.raises(DeadlineExceeded, match="deadline exceeded"):
            client._wait_before_retry(None, 2)
    finally:
        reset_current_deadline(token)

    sleep_mock.assert_not_called()
