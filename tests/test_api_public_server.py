"""Focused lifecycle tests for the stateless public API proxy."""

from typing import Any

import pytest

import api_public_server as public_api


class FakeRPC:
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[str] = []
        self.timeouts: dict[str, float] = {}

    def request(
        self, method: str, params: list[Any], *, timeout: float | None = None
    ) -> Any:
        self.calls.append(method)
        if timeout is not None:
            self.timeouts[method] = timeout
        response = self.responses.get(method)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            return response()
        return response


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("API_TOKEN", "test-token")
    monkeypatch.setattr(public_api.Config, "validate_region_config", lambda: [])
    public_api.app.config.update(TESTING=True)
    return public_api.app.test_client()


def test_region_route_only_ensures_target_region(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    jp = FakeRPC(
        {
            "ensure_ready": {
                "region": "jp",
                "state": "READY",
                "initialized": True,
                "authenticated": True,
                "ready": True,
            },
            "fetch_user_profile": {"id": "user"},
        }
    )
    others = {region: FakeRPC({}) for region in ("en", "tw", "kr")}
    monkeypatch.setattr(public_api, "client_map", {"jp": jp, **others})

    response = client.get("/jp/user/42/profile", headers={"x-api-token": "test-token"})

    assert response.status_code == 200
    assert jp.calls == ["ensure_ready", "fetch_user_profile"]
    assert all(not fake.calls for fake in others.values())


def test_unready_region_returns_redacted_503_and_retry_after(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    jp = FakeRPC(
        {
            "ensure_ready": {
                "region": "jp",
                "state": "DEGRADED",
                "ready": False,
                "retry_after": 2.2,
                "error": {"type": "RuntimeError", "message": "secret"},
            }
        }
    )
    monkeypatch.setattr(public_api, "client_map", {"jp": jp})

    response = client.get("/jp/user/42/profile", headers={"x-api-token": "test-token"})

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "3"
    body = response.get_json()
    assert body["reason"] == "lifecycle operation failed"
    assert "secret" not in response.get_data(as_text=True)


def test_failed_region_can_retry_without_affecting_healthy_region(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = 0

    def ensure_ready() -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return {"region": "jp", "state": "FAILED", "ready": False}
        return {
            "region": "jp",
            "state": "READY",
            "initialized": True,
            "authenticated": True,
            "ready": True,
        }

    jp = FakeRPC({"ensure_ready": ensure_ready, "fetch_user_profile": {"id": "user"}})
    en = FakeRPC({})
    monkeypatch.setattr(public_api, "client_map", {"jp": jp, "en": en})

    first = client.get("/jp/user/42/profile", headers={"x-api-token": "test-token"})
    second = client.get("/jp/user/42/profile", headers={"x-api-token": "test-token"})

    assert first.status_code == 503
    assert second.status_code == 200
    assert attempts == 2
    assert not en.calls


def test_live_has_no_regional_rpc_calls(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    regions = {region: FakeRPC({}) for region in ("jp", "en")}
    monkeypatch.setattr(public_api, "client_map", regions)

    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "status": "success"}
    assert all(not fake.calls for fake in regions.values())


def test_ready_and_legacy_health_are_read_only_and_include_reasons(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    jp = FakeRPC(
        {
            "readiness": {
                "region": "jp",
                "state": "READY",
                "initialized": True,
                "authenticated": True,
                "ready": True,
            }
        }
    )
    en = FakeRPC({"readiness": RuntimeError("upstream secret")})
    monkeypatch.setattr(public_api, "client_map", {"jp": jp, "en": en})

    ready = client.get("/health/ready")
    legacy = client.get("/health")

    assert ready.status_code == 503
    ready_regions = ready.get_json()["regions"]
    assert ready_regions["jp"]["ready"] is True
    assert ready_regions["en"]["reason"] == "lifecycle RPC unavailable"
    assert legacy.status_code == 500
    assert legacy.get_json()["regions"] == {"jp": True, "en": False}
    assert jp.calls == ["readiness", "readiness"]
    assert en.calls == ["readiness", "readiness"]


def test_readiness_passes_short_per_request_timeout(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    regions = {
        region: FakeRPC(
            {
                "readiness": {
                    "region": region,
                    "state": "READY",
                    "initialized": True,
                    "authenticated": True,
                    "ready": True,
                }
            }
        )
        for region in ("jp", "en")
    }
    monkeypatch.setattr(public_api, "client_map", regions)

    statuses = public_api._collect_readiness()

    assert all(status["ready"] for status in statuses.values())
    assert all(
        fake.timeouts == {"readiness": public_api.READINESS_PROBE_TIMEOUT}
        for fake in regions.values()
    )


def test_unknown_region_keeps_client_error_semantics(client: Any) -> None:
    response = client.get("/xx/user/42/profile", headers={"x-api-token": "test-token"})

    assert response.status_code == 400
