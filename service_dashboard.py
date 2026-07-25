import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import Config
from utils.jsonrpc_client import JSONRPCClient
from utils.redaction import redact_structure, redact_text

SERVICE_TYPES = ("shared_client", "check_update", "event_tracker")
READINESS_PROBE_TIMEOUT = 5.0
SERVICE_NAME_CANDIDATES: dict[str, tuple[str, ...]] = {
    "shared_client": ("sharedApiClient-{region}", "sekai-shared-client-{region}"),
    "check_update": ("checkUpdate-{region}", "sekai-check-update-{region}"),
    "event_tracker": ("eventTracker-{region}", "sekai-event-tracker-{region}"),
}
ERROR_RE = re.compile(
    r"(ERROR|CRITICAL|Traceback|Exception|HTTPError|status[=:]\s*5\d\d|\b5\d\d\b)",
    re.IGNORECASE,
)

# Canonical, single normalized service state. Every service summary exposes
# exactly one of these in ``state``; the legacy ``ok`` flag is *derived* from
# ``state`` (``ok == (state == "healthy")``) so the two can never contradict.
SERVICE_STATES = (
    "healthy",
    "degraded",
    "probe_failed",
    "offline",
    "missing",
    "restarting",
)

# Priority order (highest precedence first) used by ``_derive_state``.
# The first matching condition wins.
_STATE_PRIORITY = (
    "missing",  # no pm2 process entry at all
    "restarting",  # pm2 itself reports the process is restarting right now
    "offline",  # pm2 status is anything other than "online"
    "probe_failed",  # online, but the shared_client health probe did not pass
    "degraded",  # online, but recent log errors were detected
    "healthy",  # online, probe ok, no log errors
)

# Structured outcome of a restart operation. ``success`` means the pm2 restart
# command ran and the service came back online; ``restart_failed`` means the
# pm2 restart command itself failed (or the process vanished); ``refresh_failed``
# means pm2 restart *succeeded* but the post-restart status could not be
# confirmed (status refresh error, or the process is offline/missing again).
RESTART_STATUS = ("success", "restart_failed", "refresh_failed")


@dataclass(frozen=True)
class ServiceRef:
    region: str
    service_type: str
    name: str


def service_name(region: str, service_type: str) -> str:
    if region not in Config.REGIONS:
        raise ValueError(f"Unsupported region: {region}")
    if service_type not in SERVICE_NAME_CANDIDATES:
        raise ValueError(f"Unsupported service type: {service_type}")
    templates = {
        "shared_client": (Config.SERVICE_SHARED_CLIENT_TEMPLATE,)
        + SERVICE_NAME_CANDIDATES["shared_client"],
        "check_update": (Config.SERVICE_CHECK_UPDATE_TEMPLATE,)
        + SERVICE_NAME_CANDIDATES["check_update"],
        "event_tracker": (Config.SERVICE_EVENT_TRACKER_TEMPLATE,)
        + SERVICE_NAME_CANDIDATES["event_tracker"],
    }
    return templates[service_type][0].format(region=region)


def _validate_service_target(region: str, service_type: str) -> None:
    """Validate dashboard targets before consulting PM2."""
    if region not in Config.REGIONS:
        raise ValueError(f"Unsupported region: {region}")
    if service_type not in SERVICE_TYPES:
        raise ValueError(f"Unsupported service type: {service_type}")


def service_ref(
    region: str,
    service_type: str,
    processes: dict[str, dict[str, Any]] | None = None,
) -> ServiceRef:
    _validate_service_target(region, service_type)
    candidates = [
        Config.SERVICE_SHARED_CLIENT_TEMPLATE
        if service_type == "shared_client"
        else Config.SERVICE_CHECK_UPDATE_TEMPLATE
        if service_type == "check_update"
        else Config.SERVICE_EVENT_TRACKER_TEMPLATE
    ]
    candidates.extend(SERVICE_NAME_CANDIDATES[service_type])
    seen: set[str] = set()
    for template in candidates:
        name = template.format(region=region)
        if name in seen:
            continue
        seen.add(name)
        if processes is None or name in processes:
            return ServiceRef(region, service_type, name)
    return ServiceRef(region, service_type, candidates[0].format(region=region))


def _run_pm2(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [Config.PM2_BIN, *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _pm2_processes() -> dict[str, dict[str, Any]]:
    try:
        result = _run_pm2(["jlist"])
        processes = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as err:
        raise RuntimeError(f"Failed to read pm2 process list: {err}") from err

    return {
        proc["name"]: proc
        for proc in processes
        if isinstance(proc, dict) and isinstance(proc.get("name"), str)
    }


def _tail_lines(file_path: str | None, limit: int) -> list[str]:
    if not file_path or limit <= 0:
        return []
    path = Path(file_path)
    if not path.exists() or not path.is_file():
        return []

    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        block_size = 8192
        data = b""
        while size > 0 and data.count(b"\n") <= limit:
            read_size = min(block_size, size)
            size -= read_size
            handle.seek(size)
            data = handle.read(read_size) + data

    return data.decode("utf-8", errors="replace").splitlines()[-limit:]


def _scan_logs(proc: dict[str, Any]) -> dict[str, Any]:
    pm2_env = proc.get("pm2_env", {})
    lines = _tail_lines(
        pm2_env.get("pm_out_log_path"), Config.SERVICE_LOG_TAIL_LINES
    ) + _tail_lines(pm2_env.get("pm_err_log_path"), Config.SERVICE_LOG_TAIL_LINES)
    matches = [line for line in lines if ERROR_RE.search(line)]
    return {
        "scannedLines": len(lines),
        "errorCount": len(matches),
        # Scanned log lines may contain tokens/paths; redact before surfacing.
        "recentErrors": [redact_text(line) for line in matches[-5:]],
    }


def _shared_client_probe(region: str) -> dict[str, Any]:
    client = JSONRPCClient(f"http://localhost:{Config.get_region_port(region)}/")
    try:
        # ``readiness`` is a read-only lifecycle snapshot.  In particular, do
        # not use is_init/is_login here: an initialized client is deliberately
        # not considered healthy until authentication has completed.
        lifecycle = client.request("readiness", [], timeout=READINESS_PROBE_TIMEOUT)
        if not isinstance(lifecycle, dict):
            raise RuntimeError("invalid readiness response")
        lifecycle = _redact_lifecycle(lifecycle)
        ready = bool(lifecycle.get("ready"))
        return {
            "ok": ready,
            "available": True,
            "ready": ready,
            "reason": None if ready else "not_ready",
            "lifecycle": lifecycle,
            # Keep the most useful lifecycle fields at the probe level too,
            # while retaining the complete contract response above.
            "state": lifecycle.get("state"),
            "initialized": bool(lifecycle.get("initialized")),
            "authenticated": bool(lifecycle.get("authenticated")),
            "loggedIn": bool(lifecycle.get("authenticated")),
            "retryAfter": lifecycle.get("retry_after"),
            "nextRetryAt": lifecycle.get("next_retry_at"),
            "error": lifecycle.get("error"),
        }
    except Exception as err:
        return {
            "ok": False,
            "available": False,
            "ready": False,
            "reason": "rpc_unavailable",
            "state": "UNAVAILABLE",
            "error": redact_text(str(err)),
        }


def _redact_lifecycle(value: Any) -> Any:
    """Redact lifecycle text without changing the RPC response structure."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        # Handle sensitive keys first, then redact free text in ordinary fields
        # such as lifecycle error messages.
        redacted = redact_structure(value)
        return {key: _redact_lifecycle(item) for key, item in redacted.items()}
    if isinstance(value, list):
        return [_redact_lifecycle(item) for item in value]
    return value


def _derive_state(
    status: str,
    logs: dict[str, Any],
    probe_ok: bool | None = None,
) -> str:
    """Compute the single normalized service ``state``.

    Priority (highest first): missing > restarting > offline > probe_failed >
    degraded > healthy. ``missing`` is handled by the caller (no process entry);
    here ``status`` is the pm2 status, ``logs`` the scanned logs, and
    ``probe_ok`` is ``None`` for non-shared_client services or a bool when a
    health probe ran.
    """
    if status == "restarting":
        return "restarting"
    if status != "online":
        return "offline"
    if probe_ok is not None and not probe_ok:
        return "probe_failed"
    if logs.get("errorCount", 0) > 0:
        return "degraded"
    return "healthy"


def _process_summary(
    ref: ServiceRef, processes: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    proc = processes.get(ref.name)
    if not proc:
        state = "missing"
        return {
            "name": ref.name,
            "type": ref.service_type,
            "status": "missing",
            "state": state,
            "ok": False,
            "logs": {"scannedLines": 0, "errorCount": 0, "recentErrors": []},
        }

    pm2_env = proc.get("pm2_env", {})
    status = pm2_env.get("status", "unknown")
    restart_count = pm2_env.get("restart_time", 0)
    uptime = pm2_env.get("pm_uptime")
    logs = _scan_logs(proc)

    probe_ok: bool | None = None
    summary: dict[str, Any] = {
        "name": ref.name,
        "type": ref.service_type,
        "status": status,
        "pid": proc.get("pid"),
        "cpu": proc.get("monit", {}).get("cpu"),
        "memory": proc.get("monit", {}).get("memory"),
        "restartCount": restart_count,
        "uptime": uptime,
        "logs": logs,
    }
    if ref.service_type == "shared_client":
        probe = _shared_client_probe(ref.region)
        summary["probe"] = probe
        # Readiness snapshots always include ``error``; ``None`` means there
        # was no lifecycle error.  Only a meaningful error should invalidate a
        # probe that otherwise reports itself as ready.
        probe_ok = bool(probe.get("ok")) and not probe.get("error")

    state = _derive_state(status, logs, probe_ok)
    summary["state"] = state
    # ``ok`` is strictly derived from ``state`` so the two can never contradict.
    summary["ok"] = state == "healthy"
    return summary


def dashboard_status() -> dict[str, Any]:
    processes = _pm2_processes()
    regions = {}
    for region in Config.REGIONS:
        services = {
            service_type: _process_summary(
                service_ref(region, service_type, processes), processes
            )
            for service_type in SERVICE_TYPES
        }
        regions[region] = {
            "ok": all(service["ok"] for service in services.values()),
            "services": services,
        }

    return {"regions": regions, "updatedAt": int(time.time())}


def restart_service(region: str, service_type: str) -> dict[str, Any]:
    _validate_service_target(region, service_type)
    processes = _pm2_processes()
    ref = service_ref(region, service_type, processes)
    try:
        _run_pm2(["restart", ref.name])
    except (OSError, subprocess.SubprocessError) as err:
        # The pm2 restart command itself failed: surface the real error and do
        # not fabricate a transient state.
        prior = {ref.name: proc} if (proc := processes.get(ref.name)) else {}
        return {
            "restartStatus": "restart_failed",
            "message": f"PM2 restart command failed for {ref.name}: {err}",
            "region": region,
            "serviceType": service_type,
            "service": _process_summary(ref, prior),
        }

    time.sleep(Config.SERVICE_STABLE_WAIT_SECONDS)
    try:
        proc = _pm2_processes().get(ref.name)
    except RuntimeError as err:
        # pm2 was reachable for the restart but the status refresh failed.
        return {
            "restartStatus": "refresh_failed",
            "message": f"Service restarted but status refresh failed: {err}",
            "region": region,
            "serviceType": service_type,
            "service": None,
        }

    if not proc:
        return {
            "restartStatus": "refresh_failed",
            "message": f"PM2 service not found after restart: {ref.name}",
            "region": region,
            "serviceType": service_type,
            "service": None,
        }

    service = _process_summary(ref, {ref.name: proc})
    if service["state"] != "healthy":
        # pm2 reported the restart succeeded, but the process is not cleanly
        # healthy afterwards (offline, missing, degraded, probe_failed). This is
        # distinct from a pm2 command failure: we distinguish "ran but could not
        # confirm healthy" from "pm2 error".
        return {
            "restartStatus": "refresh_failed",
            "message": (
                f"Service restarted but is not healthy (state={service['state']})."
            ),
            "region": region,
            "serviceType": service_type,
            "service": service,
        }

    return {
        "restartStatus": "success",
        "region": region,
        "serviceType": service_type,
        "service": service,
    }


def restart_region(region: str) -> dict[str, Any]:
    restarted = []
    for service_type in SERVICE_TYPES:
        restarted.append(restart_service(region, service_type))
    restart_statuses = {item["restartStatus"] for item in restarted}
    if "restart_failed" in restart_statuses:
        status = "restart_failed"
    elif "refresh_failed" in restart_statuses:
        status = "refresh_failed"
    else:
        status = "success"
    return {
        "restartStatus": status,
        "region": region,
        "services": restarted,
    }
