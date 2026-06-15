from unittest.mock import Mock

import service_dashboard


def _proc(name: str, status: str = "online") -> dict:
    return {
        "name": name,
        "pid": 123,
        "monit": {"cpu": 1, "memory": 1024 * 1024},
        "pm2_env": {
            "status": status,
            "restart_time": 2,
            "pm_uptime": 1000,
        },
    }


def test_service_name_uses_default_templates():
    assert service_dashboard.service_name("jp", "shared_client") == (
        "sharedApiClient-jp"
    )
    assert service_dashboard.service_name("en", "check_update") == (
        "checkUpdate-en"
    )
    assert service_dashboard.service_name("tw", "event_tracker") == (
        "eventTracker-tw"
    )


def test_dashboard_status_marks_missing_services_unhealthy(monkeypatch):
    monkeypatch.setattr(
        service_dashboard,
        "_pm2_processes",
        lambda: {"sharedApiClient-jp": _proc("sharedApiClient-jp")},
    )
    monkeypatch.setattr(
        service_dashboard,
        "_scan_logs",
        lambda proc: {"scannedLines": 0, "errorCount": 0, "recentErrors": []},
    )
    monkeypatch.setattr(
        service_dashboard,
        "_shared_client_probe",
        lambda region: {"ok": True, "initialized": True, "loggedIn": True},
    )

    status = service_dashboard.dashboard_status()

    assert status["regions"]["jp"]["services"]["shared_client"]["ok"] is True
    assert status["regions"]["jp"]["services"]["check_update"]["status"] == "missing"
    assert status["regions"]["jp"]["ok"] is False


def test_restart_region_uses_required_order(monkeypatch):
    calls = []

    def restart_service(region, service_type):
        calls.append((region, service_type))
        return {"service": {"type": service_type}}

    monkeypatch.setattr(service_dashboard, "restart_service", restart_service)

    result = service_dashboard.restart_region("jp")

    assert calls == [
        ("jp", "shared_client"),
        ("jp", "check_update"),
        ("jp", "event_tracker"),
    ]
    assert result["status"] == "success"


def test_restart_service_runs_pm2_and_returns_summary(monkeypatch):
    run_pm2 = Mock()
    monkeypatch.setattr(service_dashboard, "_run_pm2", run_pm2)
    monkeypatch.setattr(service_dashboard.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        service_dashboard,
        "_pm2_processes",
        lambda: {"checkUpdate-jp": _proc("checkUpdate-jp")},
    )
    monkeypatch.setattr(
        service_dashboard,
        "_scan_logs",
        lambda proc: {"scannedLines": 0, "errorCount": 0, "recentErrors": []},
    )

    result = service_dashboard.restart_service("jp", "check_update")

    run_pm2.assert_called_once_with(["restart", "checkUpdate-jp"])
    assert result["status"] == "success"
    assert result["service"]["ok"] is True
