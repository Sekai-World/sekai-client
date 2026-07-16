"""Unit tests for internal JSON-RPC authentication.

Covers both ends of the loopback RPC channel:

- Server side (shared_client before_request): fail-closed token check,
  loopback dev bypass, and that the scheduler is never started by an
  unauthenticated request.
- Client side (utils.jsonrpc_client.JSONRPCClient): the internal token
  header is added automatically, missing token fails closed, and HTTP
  errors are raised before JSON parsing.
"""

import pytest
from werkzeug.test import Client

import shared_client
from utils.jsonrpc_client import INTERNAL_RPC_TOKEN_HEADER, JSONRPCClient


def _make_client() -> Client:
    return shared_client.app.test_client()


def _is_init_payload() -> dict:
    return {"jsonrpc": "2.0", "method": "is_init", "id": 1}


class TestServerRpcAuth:
    def test_missing_token_returns_500(self, monkeypatch):
        monkeypatch.setattr(shared_client.Config, "get_internal_rpc_token", lambda: "")
        monkeypatch.setattr(
            shared_client.Config, "allow_insecure_internal_rpc", lambda: False
        )
        # ensure client comes from loopback so only the token gate matters
        monkeypatch.setattr(shared_client, "_is_loopback", lambda addr: True)
        resp = _make_client().post("/", json=_is_init_payload())
        assert resp.status_code == 500

    def test_wrong_token_returns_401(self, monkeypatch):
        monkeypatch.setattr(
            shared_client.Config, "get_internal_rpc_token", lambda: "right"
        )
        monkeypatch.setattr(
            shared_client.Config, "allow_insecure_internal_rpc", lambda: False
        )
        monkeypatch.setattr(shared_client, "_is_loopback", lambda addr: True)
        resp = _make_client().post(
            "/",
            json=_is_init_payload(),
            headers={INTERNAL_RPC_TOKEN_HEADER: "wrong"},
        )
        assert resp.status_code == 401

    def test_correct_token_accepted(self, monkeypatch):
        monkeypatch.setattr(
            shared_client.Config, "get_internal_rpc_token", lambda: "right"
        )
        monkeypatch.setattr(
            shared_client.Config, "allow_insecure_internal_rpc", lambda: False
        )
        monkeypatch.setattr(shared_client, "_is_loopback", lambda addr: True)
        resp = _make_client().post(
            "/",
            json=_is_init_payload(),
            headers={INTERNAL_RPC_TOKEN_HEADER: "right"},
        )
        # 200 (ok) or a JSON-RPC error for an uninitialized client, but NOT 401/500.
        assert resp.status_code not in (401, 500)

    def test_loopback_dev_bypass_allowed(self, monkeypatch):
        monkeypatch.setattr(shared_client.Config, "get_internal_rpc_token", lambda: "")
        monkeypatch.setattr(
            shared_client.Config, "allow_insecure_internal_rpc", lambda: True
        )
        monkeypatch.setattr(shared_client, "_is_loopback", lambda addr: True)
        resp = _make_client().post("/", json=_is_init_payload())
        assert resp.status_code not in (401, 500)

    def test_non_loopback_rejected_even_with_bypass(self, monkeypatch):
        # Even with a correct token and insecure bypass on, a non-loopback
        # caller must be rejected.
        monkeypatch.setattr(
            shared_client.Config, "get_internal_rpc_token", lambda: "right"
        )
        monkeypatch.setattr(
            shared_client.Config, "allow_insecure_internal_rpc", lambda: True
        )
        monkeypatch.setattr(shared_client, "_is_loopback", lambda addr: False)
        resp = _make_client().post(
            "/",
            json=_is_init_payload(),
            headers={INTERNAL_RPC_TOKEN_HEADER: "right"},
        )
        assert resp.status_code == 401

    def test_configured_token_disables_loopback_dev_bypass(self, monkeypatch):
        monkeypatch.setattr(
            shared_client.Config, "get_internal_rpc_token", lambda: "right"
        )
        monkeypatch.setattr(
            shared_client.Config, "allow_insecure_internal_rpc", lambda: True
        )
        monkeypatch.setattr(shared_client, "_is_loopback", lambda addr: True)

        missing = _make_client().post("/", json=_is_init_payload())
        wrong = _make_client().post(
            "/",
            json=_is_init_payload(),
            headers={INTERNAL_RPC_TOKEN_HEADER: "wrong"},
        )

        assert missing.status_code == 401
        assert wrong.status_code == 401

    def test_missing_token_without_bypass_is_500(self, monkeypatch):
        monkeypatch.setattr(shared_client.Config, "get_internal_rpc_token", lambda: "")
        monkeypatch.setattr(
            shared_client.Config, "allow_insecure_internal_rpc", lambda: False
        )
        monkeypatch.setattr(shared_client, "_is_loopback", lambda addr: False)
        resp = _make_client().post("/", json=_is_init_payload())
        assert resp.status_code == 500

    def test_unauthenticated_request_does_not_start_scheduler(self, monkeypatch):
        monkeypatch.setattr(shared_client.Config, "get_internal_rpc_token", lambda: "")
        monkeypatch.setattr(
            shared_client.Config, "allow_insecure_internal_rpc", lambda: False
        )
        monkeypatch.setattr(shared_client, "_is_loopback", lambda addr: True)
        started = []
        monkeypatch.setattr(
            shared_client, "start_scheduler", lambda: started.append(True)
        )
        _make_client().post("/", json=_is_init_payload())
        assert started == []


class TestClientRpcAuth:
    def test_request_adds_token_header(self, monkeypatch):
        monkeypatch.setattr(
            shared_client.Config, "get_internal_rpc_token", lambda: "tok"
        )
        captured = {}

        def fake_post(url, **kwargs):
            captured["headers"] = kwargs.get("headers", {})
            resp = MockResponse()
            return resp

        monkeypatch.setattr("requests.post", fake_post)

        client = JSONRPCClient()
        client.request("is_init", [])

        assert captured["headers"].get(INTERNAL_RPC_TOKEN_HEADER) == "tok"

    def test_request_raises_on_http_error(self, monkeypatch):
        monkeypatch.setattr(
            shared_client.Config, "get_internal_rpc_token", lambda: "tok"
        )

        class BadResp:
            status_code = 401

            def raise_for_status(self):
                raise RuntimeError("401 auth")

            def json(self):
                raise ValueError("no json")

        monkeypatch.setattr("requests.post", lambda *a, **k: BadResp())

        client = JSONRPCClient()
        with pytest.raises(RuntimeError):
            client.request("is_init", [])

    def test_missing_token_raises_runtime_error(self, monkeypatch):
        monkeypatch.setattr(shared_client.Config, "get_internal_rpc_token", lambda: "")
        monkeypatch.setattr(
            shared_client.Config, "allow_insecure_internal_rpc", lambda: False
        )
        client = JSONRPCClient()
        with pytest.raises(RuntimeError):
            client.request("is_init", [])

    def test_refuses_to_send_token_to_non_loopback_url(self, monkeypatch):
        monkeypatch.setattr(
            shared_client.Config, "get_internal_rpc_token", lambda: "tok"
        )
        monkeypatch.setattr(
            shared_client.Config, "allow_insecure_internal_rpc", lambda: False
        )
        calls = []
        monkeypatch.setattr("requests.post", lambda *a, **k: calls.append((a, k)))

        client = JSONRPCClient("http://example.com:39390/")
        with pytest.raises(RuntimeError, match="non-loopback"):
            client.request("is_init", [])

        assert calls == []


class MockResponse:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "jsonrpc": "2.0",
            "result": True,
            "id": 1,
        }
