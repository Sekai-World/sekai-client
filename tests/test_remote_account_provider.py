"""Integration tests against an in-process fake account-service HTTP server."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from unittest.mock import Mock

import pytest

import shared_client
from accounts import (
    AccountProviderError,
    AccountRegion,
    AccountUnavailableError,
    InvalidAccountReason,
    InvalidLeaseError,
    JpEnCredential,
    RemoteAccountProvider,
    TwKrCredential,
)
from game_auth import GameAuthenticationService


class FakeAccountService:
    def __init__(self) -> None:
        self.requests: list[Any] = []
        self.acquire_statuses: list[int] = []
        self.renew_statuses: list[int] = []
        self.renew_lease_id: str | None = None
        self.renew_expires_at = "2099-01-02T00:00:00+00:00"

    def handler(self):  # noqa: C901 - fake server keeps its contract in one handler
        state = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self._body()
                state.requests.append(("POST", self.path, dict(self.headers), body))
                if self.headers.get("Authorization") != "Bearer service-token":
                    self._respond(401, {"detail": "unauthorized"})
                    return
                if self.path == "/v1/leases":
                    status = (
                        state.acquire_statuses.pop(0) if state.acquire_statuses else 201
                    )
                    if status != 201:
                        self._respond(
                            status,
                            {"detail": "unavailable"},
                            {"Retry-After": "2"},
                        )
                        return
                    region = body["region"]
                    auth = (
                        {
                            "kind": "jp_en",
                            "user_id": f"{region}-user",
                            "credential": f"{region}-credential",
                            "signature": f"{region}-signature",
                        }
                        if region in ("jp", "en")
                        else {
                            "kind": "tw_kr",
                            "sdk_open_id": f"{region}-open",
                            "access_token": f"{region}-token",
                            "device_id": f"{region}-device",
                            "install_id": f"{region}-install",
                            "user_agent": f"{region}-agent",
                            "device_model": f"{region}-model",
                            "os_version": f"{region}-os",
                        }
                    )
                    self._respond(
                        201,
                        {
                            "lease_id": f"lease-{region}",
                            "account_id": "not-used-by-client",
                            "region": region,
                            "consumer": body["consumer"],
                            "expires_at": "2099-01-01T00:00:00+00:00",
                            "auth": auth,
                        },
                        {"Cache-Control": "no-store"},
                    )
                    return
                if self.path.endswith("/invalid"):
                    self._respond(204, None)
                    return
                self._respond(404, {"detail": "not found"})

            def do_DELETE(self):
                state.requests.append(("DELETE", self.path, dict(self.headers), None))
                if self.headers.get("Authorization") != "Bearer service-token":
                    self._respond(401, {"detail": "unauthorized"})
                    return
                self._respond(204, None)

            def do_PATCH(self):
                body = self._body()
                state.requests.append(("PATCH", self.path, dict(self.headers), body))
                if self.headers.get("Authorization") != "Bearer service-token":
                    self._respond(401, {"detail": "unauthorized"})
                    return
                status = (
                    state.renew_statuses.pop(0) if state.renew_statuses else 200
                )
                if status != 200:
                    self._respond(
                        status,
                        {"detail": "unavailable"},
                        {"Retry-After": "2"},
                    )
                    return
                self._respond(
                    200,
                    {
                        "lease_id": state.renew_lease_id
                        or self.path.rsplit("/", 1)[-1],
                        "expires_at": state.renew_expires_at,
                    },
                )

            def _body(self):
                length = int(self.headers.get("Content-Length", "0"))
                return json.loads(self.rfile.read(length)) if length else None

            def _respond(self, status, body, headers=None):
                payload = b"" if body is None else json.dumps(body).encode()
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                if payload:
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if payload:
                    self.wfile.write(payload)

            def log_message(self, format, *args):
                return

        return Handler


@contextmanager
def fake_service():
    state = FakeAccountService()
    server = ThreadingHTTPServer(("127.0.0.1", 0), state.handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("region", list(AccountRegion))
def test_four_region_lease_contract_and_client_owned_game_auth(region):
    with fake_service() as (service, url):
        provider = RemoteAccountProvider(url, "service-token")
        lease = provider.acquire(
            region,
            f"shared-client-{region.value}",
            ttl_seconds=300,
            idempotency_key="logical-login-1",
        )

    request = service.requests[0]
    assert request[2]["Authorization"] == "Bearer service-token"
    assert request[2]["Idempotency-Key"] == "logical-login-1"
    assert request[3] == {
        "region": region.value,
        "consumer": f"shared-client-{region.value}",
        "ttl_seconds": 300,
    }

    game_transport = Mock()
    game_transport.call_pjsk_api.return_value = {
        "sessionToken": "game-session",
        "appVersion": "1.0.0",
        "dataVersion": "1.0.0",
        "assetVersion": "1.0.0",
        "multiPlayVersion": "1.0.0",
    }
    GameAuthenticationService(game_transport).authenticate(lease.credential)
    if isinstance(lease.credential, JpEnCredential):
        game_transport.call_pjsk_api.assert_called_once_with(
            f"/user/{region.value}-user/auth?refreshUpdatedResources=False",
            "put",
            {"credential": f"{region.value}-credential"},
        )
    else:
        assert isinstance(lease.credential, TwKrCredential)
        game_transport.call_pjsk_api.assert_called_once_with(
            "/user/auth",
            "post",
            {
                "userID": 0,
                "accessToken": f"{region.value}-token",
                "deviceId": None,
                "authTriggerType": "normal",
            },
        )


def test_release_and_invalid_report_match_service_contract():
    with fake_service() as (service, url):
        provider = RemoteAccountProvider(url, "service-token")
        lease = provider.acquire(
            AccountRegion.KR, "worker", ttl_seconds=60, idempotency_key="one"
        )
        provider.report_invalid(
            lease.lease_id, InvalidAccountReason.AUTHENTICATION_FAILED
        )
        provider.release(lease.lease_id)

    assert service.requests[1][1] == "/v1/leases/lease-kr/invalid"
    assert service.requests[1][3] == {"reason": "authentication_failed"}
    assert service.requests[2][0:2] == ("DELETE", "/v1/leases/lease-kr")


def test_renew_matches_service_contract_and_returns_expiry():
    with fake_service() as (service, url):
        service.renew_lease_id = "lease/with/slashes"
        provider = RemoteAccountProvider(url, "service-token")
        expires_at = provider.renew(
            "lease/with/slashes",
            extend_seconds=3600,
            idempotency_key="renew-one",
        )

    request = service.requests[0]
    assert request[0:2] == ("PATCH", "/v1/leases/lease%2Fwith%2Fslashes")
    assert request[2]["Content-Type"] == "application/json"
    assert request[2]["Idempotency-Key"] == "renew-one"
    assert request[3] == {"extend_seconds": 3600}
    assert expires_at == datetime.fromisoformat(service.renew_expires_at)
    assert expires_at.tzinfo is UTC


def test_renew_rejects_mismatched_lease_id():
    with fake_service() as (service, url):
        service.renew_lease_id = "another-lease"
        provider = RemoteAccountProvider(url, "service-token")
        with pytest.raises(AccountProviderError) as caught:
            provider.renew(
                "lease-tw", extend_seconds=30, idempotency_key="renew-one"
            )

    assert caught.value.code == "invalid_service_response"


def test_renew_rejects_naive_expiry():
    with fake_service() as (service, url):
        service.renew_expires_at = "2099-01-02T00:00:00"
        provider = RemoteAccountProvider(url, "service-token")
        with pytest.raises(AccountProviderError) as caught:
            provider.renew(
                "lease-tw", extend_seconds=30, idempotency_key="renew-one"
            )

    assert caught.value.code == "invalid_service_response"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (404, "invalid_lease"),
        (403, "account_service_unauthorized"),
        (409, "idempotency_conflict"),
    ],
)
def test_renew_maps_service_errors(status, expected):
    with fake_service() as (service, url):
        service.renew_statuses = [status]
        provider = RemoteAccountProvider(url, "service-token")
        with pytest.raises((InvalidLeaseError, AccountProviderError)) as caught:
            provider.renew(
                "lease-tw", extend_seconds=30, idempotency_key="renew-one"
            )

    assert caught.value.code == expected


def test_renew_retries_unavailable_and_honors_retry_after():
    with fake_service() as (service, url):
        service.renew_statuses = [503, 503, 503]
        delays = []
        provider = RemoteAccountProvider(url, "service-token", sleep=delays.append)
        with pytest.raises(AccountUnavailableError) as caught:
            provider.renew(
                "lease-tw", extend_seconds=30, idempotency_key="renew-one"
            )

    assert caught.value.retry_after == 2.0
    assert delays == [2.0, 2.0]


@pytest.mark.parametrize("extend_seconds", [29, 86401])
def test_renew_rejects_out_of_range_extension_without_http_call(extend_seconds):
    with fake_service() as (service, url):
        provider = RemoteAccountProvider(url, "service-token")
        with pytest.raises(ValueError, match="between 30 and 86400"):
            provider.renew(
                "lease-tw", extend_seconds=extend_seconds, idempotency_key="renew-one"
            )

    assert service.requests == []


def test_acquire_retries_with_same_idempotency_key_then_succeeds():
    with fake_service() as (service, url):
        service.acquire_statuses = [503, 201]
        delays = []
        provider = RemoteAccountProvider(url, "service-token", sleep=delays.append)
        lease = provider.acquire(
            AccountRegion.TW, "worker", ttl_seconds=60, idempotency_key="stable"
        )

    assert lease.region is AccountRegion.TW
    assert [request[2]["Idempotency-Key"] for request in service.requests] == [
        "stable",
        "stable",
    ]
    assert delays == [2.0]


def test_unavailable_and_auth_failures_map_without_response_secrets():
    with fake_service() as (service, url):
        service.acquire_statuses = [503, 503, 503]
        provider = RemoteAccountProvider(url, "service-token", sleep=lambda _: None)
        with pytest.raises(AccountUnavailableError) as unavailable:
            provider.acquire(
                AccountRegion.EN, "worker", ttl_seconds=60, idempotency_key="one"
            )
        assert unavailable.value.retry_after == 2.0

        rejected = RemoteAccountProvider(url, "wrong-token")
        with pytest.raises(AccountProviderError) as unauthorized:
            rejected.acquire(
                AccountRegion.EN, "worker", ttl_seconds=60, idempotency_key="two"
            )
        assert unauthorized.value.code == "account_service_unauthorized"
        assert "wrong-token" not in str(unauthorized.value)


def test_non_loopback_plain_http_is_rejected():
    with pytest.raises(ValueError, match="HTTPS"):
        RemoteAccountProvider("http://accounts.example.com", "service-token")


def test_shared_client_selects_remote_provider_from_environment(monkeypatch):
    monkeypatch.setenv("SEKAI_ACCOUNT_PROVIDER", "remote")
    monkeypatch.setenv("SEKAI_ACCOUNT_SERVICE_URL", "https://accounts.example.com")
    monkeypatch.setenv("SEKAI_ACCOUNT_SERVICE_TOKEN", "configuration-secret")
    monkeypatch.setenv("SEKAI_ACCOUNT_SERVICE_TIMEOUT", "4")
    monkeypatch.setenv("SEKAI_ACCOUNT_SERVICE_MAX_ATTEMPTS", "2")

    provider = shared_client._build_account_provider()

    assert isinstance(provider, RemoteAccountProvider)
    assert "configuration-secret" not in repr(provider)


def test_tw_kr_payload_missing_device_id_is_rejected():
    payload = {
        "kind": "tw_kr",
        "sdk_open_id": "open",
        "access_token": "token",
    }
    with pytest.raises(ValueError):
        RemoteAccountProvider._credential(AccountRegion.TW, payload)


def test_tw_kr_payload_empty_device_id_is_rejected():
    payload = {
        "kind": "tw_kr",
        "sdk_open_id": "open",
        "access_token": "token",
        "device_id": "",
    }
    with pytest.raises(ValueError):
        RemoteAccountProvider._credential(AccountRegion.TW, payload)


@pytest.mark.parametrize(
    "missing_key",
    ["install_id", "user_agent", "device_model", "os_version"],
)
def test_tw_kr_payload_rejects_missing_fingerprint_field(missing_key):
    payload = {
        "kind": "tw_kr",
        "sdk_open_id": "open",
        "access_token": "token",
        "device_id": "device",
        "install_id": "install",
        "user_agent": "agent",
        "device_model": "model",
        "os_version": "os",
    }
    del payload[missing_key]
    with pytest.raises(ValueError):
        RemoteAccountProvider._credential(AccountRegion.TW, payload)


@pytest.mark.parametrize(
    "empty_key",
    ["install_id", "user_agent", "device_model", "os_version"],
)
def test_tw_kr_payload_rejects_empty_fingerprint_field(empty_key):
    payload = {
        "kind": "tw_kr",
        "sdk_open_id": "open",
        "access_token": "token",
        "device_id": "device",
        "install_id": "install",
        "user_agent": "agent",
        "device_model": "model",
        "os_version": "os",
    }
    payload[empty_key] = ""
    with pytest.raises(ValueError):
        RemoteAccountProvider._credential(AccountRegion.TW, payload)
