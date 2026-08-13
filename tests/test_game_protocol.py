"""Tests for the lifecycle-independent encrypted protocol transport."""

import logging
from unittest.mock import Mock

import pytest
import requests

import game_protocol
from game_protocol import GameProtocolTransport


def make_transport(region: str = "jp") -> GameProtocolTransport:
    return GameProtocolTransport(region, {}, logging.getLogger("test.protocol"))


def test_send_uses_region_endpoint_and_persists_session_token(monkeypatch):
    response = Mock(
        status_code=200,
        headers={"x-session-token": "new-session"},
    )
    request = Mock(return_value=response)
    monkeypatch.setattr(game_protocol.requests, "request", request)
    transport = make_transport()

    assert transport.send("/system", "GET", None, "request-1") is response

    assert transport.headers["x-request-id"] == "request-1"
    assert transport.headers["x-session-token"] == "new-session"
    assert request.call_args.kwargs["allow_redirects"] is False
    assert request.call_args.kwargs["url"].endswith("/system")


def test_send_refuses_to_persist_redirect_session_token(monkeypatch):
    response = Mock(
        status_code=302,
        headers={"x-session-token": "redirect-session"},
    )
    monkeypatch.setattr(game_protocol.requests, "request", Mock(return_value=response))
    transport = make_transport()
    transport.headers["x-session-token"] = "existing-session"

    transport.send("/system", "get", None)

    assert transport.headers["x-session-token"] == "existing-session"


def test_cookie_errors_are_bounded_and_secret_safe(monkeypatch):
    monkeypatch.setattr(
        game_protocol.requests,
        "post",
        Mock(side_effect=requests.ConnectionError("upstream-secret")),
    )

    with pytest.raises(RuntimeError) as caught:
        make_transport().init_cookie()

    assert str(caught.value) == "Cookie initialization request failed"
    assert "upstream-secret" not in str(caught.value)


def test_encrypt_and_decrypt_are_transport_responsibilities(monkeypatch):
    monkeypatch.setattr(game_protocol, "encrypt_msgpack", lambda body: b"encrypted")
    monkeypatch.setattr(game_protocol, "decrypt_msgpack", lambda body: {"ok": True})
    response = Mock(
        content=b"encrypted-response",
        headers={"content-type": "application/octet-stream"},
    )

    assert (
        GameProtocolTransport.encrypt_request_body("post", {"value": 1}) == b"encrypted"
    )
    assert GameProtocolTransport.decrypt_response(response) == {"ok": True}


def test_transport_has_no_lifecycle_or_application_dependencies():
    transport = make_transport()

    assert not hasattr(transport, "login")
    assert not hasattr(transport, "account_info")
    assert not hasattr(transport, "lifecycle_callback")
