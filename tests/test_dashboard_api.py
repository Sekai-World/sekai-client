"""
Focused tests for the dashboard REST endpoints in api_public_server.

Covers:
- The new structured ``restartStatus`` on /restart endpoints and its mapping to
  the legacy top-level ``status`` field (API compatibility).
- That the status endpoint still returns the region/service shape unchanged.
"""

import pytest

import api_public_server
import config
from api_public_server import app


@pytest.fixture
def client(monkeypatch):
    # Provide a token so require_apikey passes without a real env var.
    monkeypatch.setattr(
        config.Config, "get_api_token", classmethod(lambda cls: "test-token")
    )
    app.config["TESTING"] = True
    return app.test_client()


def _auth():
    return {"x-api-token": "test-token"}


def test_status_endpoint_returns_region_shape(client, monkeypatch):
    # api_public_server imports dashboard_status by name, so patch it there.
    summary = {
        "name": "checkUpdate-jp",
        "type": "check_update",
        "status": "online",
        "state": "healthy",
        "ok": True,
        "logs": {"scannedLines": 0, "errorCount": 0, "recentErrors": []},
    }
    monkeypatch.setattr(
        api_public_server,
        "dashboard_status",
        lambda: {
            "regions": {"jp": {"ok": True, "services": {"check_update": summary}}},
            "updatedAt": 123,
        },
    )

    resp = client.get("/dashboard/api/status", headers=_auth())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["regions"]["jp"]["services"]["check_update"]["state"] == "healthy"
    assert body["updatedAt"] == 123


def test_restart_service_success_maps_to_status_success(client, monkeypatch):
    monkeypatch.setattr(
        api_public_server,
        "restart_service",
        lambda region, st: {
            "restartStatus": "success",
            "region": region,
            "serviceType": st,
            "service": {"state": "healthy", "ok": True},
        },
    )

    resp = client.post(
        "/dashboard/api/regions/jp/services/check_update/restart", headers=_auth()
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["restartStatus"] == "success"
    # Legacy field preserved for older clients.
    assert body["status"] == "success"


def test_restart_service_restart_failed_maps_to_status_error_400(client, monkeypatch):
    monkeypatch.setattr(
        api_public_server,
        "restart_service",
        lambda region, st: {
            "restartStatus": "restart_failed",
            "message": "PM2 restart command failed: pm2 not found",
            "region": region,
            "serviceType": st,
            "service": None,
        },
    )

    resp = client.post(
        "/dashboard/api/regions/jp/services/check_update/restart", headers=_auth()
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["restartStatus"] == "restart_failed"
    assert body["status"] == "error"
    assert "pm2 not found" in body["message"]


def test_restart_service_refresh_failed_maps_to_status_partial_200(client, monkeypatch):
    monkeypatch.setattr(
        api_public_server,
        "restart_service",
        lambda region, st: {
            "restartStatus": "refresh_failed",
            "message": "Service restarted but status refresh failed: jlist failed",
            "region": region,
            "serviceType": st,
            "service": None,
        },
    )

    resp = client.post(
        "/dashboard/api/regions/jp/services/check_update/restart", headers=_auth()
    )
    # Distinct from restart_failed: the restart ran, so it is HTTP 200 with a
    # non-success ``status`` rather than a hard 400 error.
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["restartStatus"] == "refresh_failed"
    assert body["status"] == "partial"


def test_restart_region_aggregates_nested_restart_status(client, monkeypatch):
    monkeypatch.setattr(
        api_public_server,
        "restart_region",
        lambda region: {
            "restartStatus": "refresh_failed",
            "region": region,
            "services": [
                {"restartStatus": "success", "serviceType": "shared_client"},
                {"restartStatus": "refresh_failed", "serviceType": "check_update"},
                {"restartStatus": "success", "serviceType": "event_tracker"},
            ],
        },
    )

    resp = client.post("/dashboard/api/regions/jp/restart", headers=_auth())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["restartStatus"] == "refresh_failed"
    assert body["status"] == "partial"
    assert len(body["services"]) == 3


def test_unauthenticated_restart_returns_401(client):
    resp = client.post("/dashboard/api/regions/jp/services/check_update/restart")
    assert resp.status_code == 401
