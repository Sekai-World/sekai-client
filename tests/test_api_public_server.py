"""Focused lifecycle tests for the stateless public API proxy."""

from typing import Any

import pytest

import api_public_server as public_api
from utils.jsonrpc_client import JSONRPCError, JSONRPCTimeout, JSONRPCUnavailable


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


READY = {
    "region": "jp",
    "state": "READY",
    "initialized": True,
    "authenticated": True,
    "ready": True,
}


def test_ready_region_skips_queued_ensure_ready(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    jp = FakeRPC({"readiness": READY, "fetch_user_profile": {"id": "user"}})
    others = {region: FakeRPC({}) for region in ("en", "tw", "kr")}
    monkeypatch.setattr(public_api, "client_map", {"jp": jp, **others})

    response = client.get("/jp/user/42/profile", headers={"x-api-token": "test-token"})

    assert response.status_code == 200
    assert jp.calls == ["readiness", "fetch_user_profile"]
    assert jp.timeouts == {
        "readiness": public_api.READINESS_PROBE_TIMEOUT,
        "fetch_user_profile": public_api.Config.PUBLIC_REQUEST_TIMEOUT,
    }
    assert all(not fake.calls for fake in others.values())


def test_unready_region_falls_back_to_ensure_ready(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    jp = FakeRPC(
        {
            "readiness": {"region": "jp", "state": "DEGRADED", "ready": False},
            "ensure_ready": READY,
            "fetch_user_profile": {"id": "user"},
        }
    )
    monkeypatch.setattr(public_api, "client_map", {"jp": jp})

    response = client.get("/jp/user/42/profile", headers={"x-api-token": "test-token"})

    assert response.status_code == 200
    assert jp.calls == ["readiness", "ensure_ready", "fetch_user_profile"]


def test_failed_readiness_probe_falls_back_to_ensure_ready(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    jp = FakeRPC(
        {
            "readiness": JSONRPCUnavailable("down"),
            "ensure_ready": READY,
            "fetch_user_profile": {"id": "user"},
        }
    )
    monkeypatch.setattr(public_api, "client_map", {"jp": jp})

    response = client.get("/jp/user/42/profile", headers={"x-api-token": "test-token"})

    assert response.status_code == 200
    assert jp.calls == ["readiness", "ensure_ready", "fetch_user_profile"]


@pytest.mark.parametrize(
    ("error", "status", "code", "retry_after"),
    [
        (
            JSONRPCError(
                "JSON-RPC error (code=-32603)",
                code=-32603,
                kind="queue_full",
                retry_after=1,
            ),
            503,
            "region_busy",
            "1",
        ),
        (
            JSONRPCError("JSON-RPC error", kind="queue_full", retry_after=2.2),
            503,
            "region_busy",
            "3",
        ),
        (JSONRPCError("JSON-RPC error", kind="queue_full"), 503, "region_busy", "1"),
        (
            JSONRPCError("JSON-RPC error", kind="deadline_exceeded"),
            504,
            "upstream_timeout",
            None,
        ),
        (
            JSONRPCError("JSON-RPC error (code=-32603)", code=-32603),
            502,
            "upstream_error",
            None,
        ),
        (
            JSONRPCUnavailable("JSON-RPC request failed"),
            503,
            "region_unavailable",
            None,
        ),
        (
            JSONRPCTimeout("JSON-RPC request timed out"),
            504,
            "upstream_timeout",
            None,
        ),
        (RuntimeError("secret-detail"), 500, "internal_error", None),
    ],
)
def test_regional_failures_are_json_with_actionable_status(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    status: int,
    code: str,
    retry_after: str | None,
) -> None:
    jp = FakeRPC({"readiness": READY, "fetch_user_profile": error})
    monkeypatch.setattr(public_api, "client_map", {"jp": jp})

    response = client.get("/jp/user/42/profile", headers={"x-api-token": "test-token"})

    assert response.status_code == status
    assert response.is_json
    body = response.get_json()
    assert body["status"] == "error"
    assert body["code"] == code
    assert body["region"] == "jp"
    assert response.headers.get("Retry-After") == retry_after
    assert "secret-detail" not in response.get_data(as_text=True)


def test_http_errors_keep_their_own_status(client: Any) -> None:
    response = client.get("/jp/user/42/profile")

    assert response.status_code == 401


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


@pytest.mark.parametrize("region", ["jp", "en"])
def test_jp_en_target_user_event_ranking_is_proxied(
    client: Any, monkeypatch: pytest.MonkeyPatch, region: str
) -> None:
    regional = FakeRPC(
        {
            "readiness": {**READY, "region": region},
            "fetch_user_event_ranking": {"rankings": []},
        }
    )
    monkeypatch.setattr(public_api, "client_map", {region: regional})

    response = client.get(
        f"/{region}/user/42/event/7/ranking", headers={"x-api-token": "test-token"}
    )

    assert response.status_code == 200
    assert response.get_json() == {"status": "success", "data": {"rankings": []}}
    assert regional.calls == ["readiness", "fetch_user_event_ranking"]
    assert (
        regional.timeouts["fetch_user_event_ranking"]
        == public_api.Config.PUBLIC_REQUEST_TIMEOUT
    )


@pytest.mark.parametrize("region", ["cn", "tw", "kr"])
def test_nuverse_target_user_event_ranking_is_rejected_without_regional_rpc(
    client: Any, monkeypatch: pytest.MonkeyPatch, region: str
) -> None:
    regions = {name: FakeRPC({}) for name in ("jp", "en", "tw", "kr")}
    monkeypatch.setattr(public_api, "client_map", regions)

    response = client.get(
        f"/{region}/user/42/event/7/ranking", headers={"x-api-token": "test-token"}
    )

    assert response.status_code == 400
    assert response.get_json() == {
        "status": "error",
        "message": f"target user event ranking is not supported for region {region}",
    }
    assert all(not fake.calls for fake in regions.values())


def test_unknown_region_keeps_client_error_semantics(client: Any) -> None:
    response = client.get("/xx/user/42/profile", headers={"x-api-token": "test-token"})

    assert response.status_code == 400
