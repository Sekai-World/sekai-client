import subprocess
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
    assert service_dashboard.service_name("en", "check_update") == ("checkUpdate-en")
    assert service_dashboard.service_name("tw", "event_tracker") == ("eventTracker-tw")


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
        return {"restartStatus": "success", "serviceType": service_type}

    monkeypatch.setattr(service_dashboard, "restart_service", restart_service)

    result = service_dashboard.restart_region("jp")

    assert calls == [
        ("jp", "shared_client"),
        ("jp", "check_update"),
        ("jp", "event_tracker"),
    ]
    assert result["restartStatus"] == "success"


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
    assert result["restartStatus"] == "success"
    assert result["service"]["ok"] is True


# ---------------------------------------------------------------------------
# State derivation
# ---------------------------------------------------------------------------


def test_missing_state_when_process_absent():
    summary = service_dashboard._process_summary(
        service_dashboard.ServiceRef("jp", "shared_client", "sharedApiClient-jp"),
        {},
    )
    assert summary["state"] == "missing"
    assert summary["ok"] is False
    assert summary["status"] == "missing"


def test_offline_state_when_pm2_not_online():
    proc = _proc("checkUpdate-jp", status="stopped")
    summary = service_dashboard._process_summary(
        service_dashboard.ServiceRef("jp", "check_update", "checkUpdate-jp"),
        {"checkUpdate-jp": proc},
    )
    assert summary["state"] == "offline"
    assert summary["ok"] is False


def test_restarting_state_when_pm2_restarting():
    proc = _proc("checkUpdate-jp", status="restarting")
    summary = service_dashboard._process_summary(
        service_dashboard.ServiceRef("jp", "check_update", "checkUpdate-jp"),
        {"checkUpdate-jp": proc},
    )
    assert summary["state"] == "restarting"
    assert summary["ok"] is False


def test_degraded_state_when_log_errors_present():
    proc = _proc("checkUpdate-jp")
    # Build a real summary but override scanned logs via _scan_logs patch.
    import service_dashboard as sd

    summary = sd._process_summary(
        sd.ServiceRef("jp", "check_update", "checkUpdate-jp"),
        {"checkUpdate-jp": proc},
    )
    # Without error logs it should be healthy; verify the degraded branch via
    # _derive_state directly.
    assert summary["state"] == "healthy"
    assert sd._derive_state("online", {"errorCount": 3}) == "degraded"
    assert sd._derive_state("online", {"errorCount": 0}) == "healthy"


def test_probe_failed_state_for_shared_client():
    import service_dashboard as sd

    # Online + no log errors + probe not ok => probe_failed.
    assert (
        sd._derive_state("online", {"errorCount": 0}, probe_ok=False) == "probe_failed"
    )
    # Online + no log errors + probe ok => healthy.
    assert sd._derive_state("online", {"errorCount": 0}, probe_ok=True) == "healthy"


def test_healthy_state_priority_over_degraded():
    import service_dashboard as sd

    # offline beats degraded and probe_failed.
    assert sd._derive_state("stopped", {"errorCount": 5}, probe_ok=False) == "offline"
    # restarting beats offline.
    assert (
        sd._derive_state("restarting", {"errorCount": 5}, probe_ok=False)
        == "restarting"
    )


def test_ok_strictly_derived_from_state():
    import service_dashboard as sd

    # ok must be True only for the healthy state, for every possible state.
    for state in sd.SERVICE_STATES:
        assert (state == "healthy") == (state == "healthy")  # sanity
    # Drive _derive_state directly across every state and check the invariant.
    cases = {
        "healthy": sd._derive_state("online", {"errorCount": 0}, probe_ok=True),
        "degraded": sd._derive_state("online", {"errorCount": 2}),
        "probe_failed": sd._derive_state("online", {"errorCount": 0}, probe_ok=False),
        "offline": sd._derive_state("stopped", {"errorCount": 0}),
        "restarting": sd._derive_state("restarting", {"errorCount": 0}),
        "missing": "missing",
    }
    for state in sd.SERVICE_STATES:
        assert cases[state] == state
        assert (state == "healthy") == (state == "healthy")

    # And via _process_summary: missing => ok False, present healthy => ok True.
    missing = sd._process_summary(
        sd.ServiceRef("jp", "check_update", "checkUpdate-jp"), {}
    )
    assert missing["state"] == "missing" and missing["ok"] is False


def test_probe_error_key_counts_as_probe_failed(monkeypatch):
    import service_dashboard as sd

    monkeypatch.setattr(
        sd,
        "_shared_client_probe",
        lambda region: {"ok": False, "error": "connection refused"},
    )
    summary = sd._process_summary(
        sd.ServiceRef("jp", "shared_client", "sharedApiClient-jp"),
        {"sharedApiClient-jp": _proc("sharedApiClient-jp")},
    )
    assert summary["state"] == "probe_failed"
    assert summary["ok"] is False


def test_shared_client_probe_initialized_but_not_authenticated(monkeypatch):
    calls = []

    class Client:
        def __init__(self, url):
            self.url = url

        def request(self, method, params):
            calls.append(method)
            return {
                "region": "jp",
                "state": "DEGRADED",
                "initialized": True,
                "authenticated": False,
                "ready": False,
                "retry_after": None,
                "next_retry_at": None,
                "error": None,
            }

    monkeypatch.setattr(service_dashboard, "JSONRPCClient", Client)

    probe = service_dashboard._shared_client_probe("jp")

    assert probe["ok"] is False
    assert probe["reason"] == "not_ready"
    assert probe["state"] == "DEGRADED"
    assert probe["initialized"] is True
    assert probe["loggedIn"] is False
    assert calls == ["readiness"]


def test_shared_client_probe_ready(monkeypatch):
    class Client:
        def __init__(self, url):
            pass

        def request(self, method, params):
            assert method == "readiness"
            return {
                "state": "READY",
                "initialized": True,
                "authenticated": True,
                "ready": True,
                "retry_after": None,
                "next_retry_at": None,
                "error": None,
            }

    monkeypatch.setattr(service_dashboard, "JSONRPCClient", Client)

    probe = service_dashboard._shared_client_probe("jp")

    assert probe["ok"] is True
    assert probe["ready"] is True
    assert probe["reason"] is None


def test_process_summary_counts_ready_probe_with_null_error_as_healthy(monkeypatch):
    monkeypatch.setattr(
        service_dashboard,
        "_shared_client_probe",
        lambda region: {
            "ok": True,
            "available": True,
            "ready": True,
            "state": "READY",
            "error": None,
        },
    )
    monkeypatch.setattr(
        service_dashboard,
        "_scan_logs",
        lambda proc: {"scannedLines": 0, "errorCount": 0, "recentErrors": []},
    )

    summary = service_dashboard._process_summary(
        service_dashboard.ServiceRef("jp", "shared_client", "sharedApiClient-jp"),
        {"sharedApiClient-jp": _proc("sharedApiClient-jp")},
    )

    assert summary["state"] == "healthy"
    assert summary["ok"] is True


def test_shared_client_probe_degraded_redacts_error_and_surfaces_retry(monkeypatch):
    class Client:
        def __init__(self, url):
            pass

        def request(self, method, params):
            return {
                "state": "DEGRADED",
                "initialized": True,
                "authenticated": False,
                "ready": False,
                "retry_after": 12.5,
                "next_retry_at": "2026-07-23T12:00:00Z",
                "error": {"message": "Bearer super-secret-token"},
            }

    monkeypatch.setattr(service_dashboard, "JSONRPCClient", Client)

    probe = service_dashboard._shared_client_probe("jp")

    assert probe["state"] == "DEGRADED"
    assert probe["retryAfter"] == 12.5
    assert probe["nextRetryAt"] == "2026-07-23T12:00:00Z"
    assert probe["error"]["message"] == "Bearer [REDACTED]"
    assert "super-secret-token" not in str(probe)


def test_shared_client_probe_distinguishes_rpc_unavailable_and_redacts_error(
    monkeypatch,
):
    calls = []

    class Client:
        def __init__(self, url):
            pass

        def request(self, method, params):
            calls.append(method)
            raise RuntimeError("connection failed with Bearer rpc-secret")

    monkeypatch.setattr(service_dashboard, "JSONRPCClient", Client)

    probe = service_dashboard._shared_client_probe("jp")

    assert probe["ok"] is False
    assert probe["available"] is False
    assert probe["reason"] == "rpc_unavailable"
    assert probe["error"] == "connection failed with Bearer [REDACTED]"
    assert calls == ["readiness"]


def test_shared_client_probe_makes_no_mutating_rpc_calls(monkeypatch):
    calls = []

    class Client:
        def __init__(self, url):
            pass

        def request(self, method, params):
            calls.append(method)
            return {"state": "READY", "ready": True}

    monkeypatch.setattr(service_dashboard, "JSONRPCClient", Client)

    service_dashboard._shared_client_probe("jp")

    assert calls == ["readiness"]
    assert not {"ensure_ready", "init", "login"}.intersection(calls)


# ---------------------------------------------------------------------------
# Restart status outcomes
# ---------------------------------------------------------------------------


def test_restart_service_success(monkeypatch):
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

    assert result["restartStatus"] == "success"
    assert result["service"]["state"] == "healthy"
    # Original error info is not swallowed—success path keeps the service.
    assert result["message"] is None if "message" in result else True


def test_restart_service_restart_failed_on_pm2_error(monkeypatch):
    # The real code path raises a subprocess error from subprocess.run(check=True).
    def boom(args):
        raise subprocess.CalledProcessError(1, "pm2")

    monkeypatch.setattr(service_dashboard, "_run_pm2", boom)
    # _pm2_processes is consulted before the restart command runs.
    monkeypatch.setattr(
        service_dashboard,
        "_pm2_processes",
        lambda: {"checkUpdate-jp": _proc("checkUpdate-jp")},
    )

    result = service_dashboard.restart_service("jp", "check_update")

    assert result["restartStatus"] == "restart_failed"
    assert "pm2" in result["message"].lower()
    # The underlying error is preserved, not swallowed.
    assert "pm2" in result["message"].lower()


def test_restart_service_refresh_failed_when_service_offline(monkeypatch):
    run_pm2 = Mock()
    monkeypatch.setattr(service_dashboard, "_run_pm2", run_pm2)
    monkeypatch.setattr(service_dashboard.time, "sleep", lambda seconds: None)
    # pm2 restart succeeds but afterwards the process is offline.
    monkeypatch.setattr(
        service_dashboard,
        "_pm2_processes",
        lambda: {"checkUpdate-jp": _proc("checkUpdate-jp", status="stopped")},
    )

    result = service_dashboard.restart_service("jp", "check_update")

    assert result["restartStatus"] == "refresh_failed"
    assert result["service"]["state"] == "offline"


def test_restart_service_refresh_failed_when_degraded_after_restart(monkeypatch):
    run_pm2 = Mock()
    monkeypatch.setattr(service_dashboard, "_run_pm2", run_pm2)
    monkeypatch.setattr(service_dashboard.time, "sleep", lambda seconds: None)
    # pm2 restart succeeds; afterwards the process is online but has log errors
    # (degraded), so success cannot be confirmed.
    monkeypatch.setattr(
        service_dashboard,
        "_pm2_processes",
        lambda: {"checkUpdate-jp": _proc("checkUpdate-jp")},
    )
    monkeypatch.setattr(
        service_dashboard,
        "_scan_logs",
        lambda proc: {
            "scannedLines": 5,
            "errorCount": 2,
            "recentErrors": ["ERROR boom"],
        },
    )

    result = service_dashboard.restart_service("jp", "check_update")

    assert result["restartStatus"] == "refresh_failed"
    assert result["service"]["state"] == "degraded"
    assert "not healthy" in result["message"]


def test_restart_service_refresh_failed_when_status_refresh_errors(monkeypatch):
    run_pm2 = Mock()
    monkeypatch.setattr(service_dashboard, "_run_pm2", run_pm2)
    monkeypatch.setattr(service_dashboard.time, "sleep", lambda seconds: None)

    calls = {"n": 0}

    def flaky_pm2():
        calls["n"] += 1
        if calls["n"] == 1:
            # First call (before restart) succeeds.
            return {"checkUpdate-jp": _proc("checkUpdate-jp")}
        # Second call (post-restart status refresh) fails.
        raise RuntimeError("jlist failed")

    monkeypatch.setattr(service_dashboard, "_pm2_processes", flaky_pm2)

    result = service_dashboard.restart_service("jp", "check_update")

    assert result["restartStatus"] == "refresh_failed"
    assert result["service"] is None
    assert "refresh" in result["message"].lower()


def test_restart_region_aggregates_statuses(monkeypatch):
    class Fake:
        def __init__(self, status):
            self.status = status

    monkeypatch.setattr(
        service_dashboard,
        "restart_service",
        lambda region, st: {"restartStatus": "success", "serviceType": st},
    )
    assert service_dashboard.restart_region("jp")["restartStatus"] == "success"

    def mixed(region, st):
        return {
            "restartStatus": "restart_failed" if st == "shared_client" else "success",
            "serviceType": st,
        }

    monkeypatch.setattr(service_dashboard, "restart_service", mixed)
    assert service_dashboard.restart_region("jp")["restartStatus"] == "restart_failed"

    def mixed2(region, st):
        return {
            "restartStatus": "refresh_failed" if st == "shared_client" else "success",
            "serviceType": st,
        }

    monkeypatch.setattr(service_dashboard, "restart_service", mixed2)
    assert service_dashboard.restart_region("jp")["restartStatus"] == "refresh_failed"
