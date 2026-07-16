import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import Config
from utils.jsonrpc_client import JSONRPCClient

SERVICE_TYPES = ("shared_client", "check_update", "event_tracker")
SERVICE_NAME_CANDIDATES: dict[str, tuple[str, ...]] = {
    "shared_client": ("sharedApiClient-{region}", "sekai-shared-client-{region}"),
    "check_update": ("checkUpdate-{region}", "sekai-check-update-{region}"),
    "event_tracker": ("eventTracker-{region}", "sekai-event-tracker-{region}"),
}
ERROR_RE = re.compile(
    r"(ERROR|CRITICAL|Traceback|Exception|HTTPError|status[=:]\s*5\d\d|\b5\d\d\b)",
    re.IGNORECASE,
)


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


def service_ref(
    region: str,
    service_type: str,
    processes: dict[str, dict[str, Any]] | None = None,
) -> ServiceRef:
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
    except (subprocess.SubprocessError, json.JSONDecodeError) as err:
        raise RuntimeError(f"Failed to read pm2 process list: {err}") from err

    return {
        proc["name"]: proc
        for proc in processes
        if isinstance(proc, dict) and isinstance(proc.get("name"), str)
    }


def _tail_lines(file_path: str | None, limit: int) -> list[str]:
    if not file_path:
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
        "recentErrors": matches[-5:],
    }


def _shared_client_probe(region: str) -> dict[str, Any]:
    client = JSONRPCClient(f"http://localhost:{Config.get_region_port(region)}/")
    try:
        initialized = bool(client.request("is_init", []))
        logged_in = bool(client.request("is_login", [])) if initialized else False
        return {"ok": initialized, "initialized": initialized, "loggedIn": logged_in}
    except Exception as err:
        return {"ok": False, "error": str(err)}


def _process_summary(
    ref: ServiceRef, processes: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    proc = processes.get(ref.name)
    if not proc:
        return {
            "name": ref.name,
            "type": ref.service_type,
            "status": "missing",
            "ok": False,
            "logs": {"scannedLines": 0, "errorCount": 0, "recentErrors": []},
        }

    pm2_env = proc.get("pm2_env", {})
    status = pm2_env.get("status", "unknown")
    restart_count = pm2_env.get("restart_time", 0)
    uptime = pm2_env.get("pm_uptime")
    logs = _scan_logs(proc)
    ok = status == "online" and logs["errorCount"] == 0

    summary = {
        "name": ref.name,
        "type": ref.service_type,
        "status": status,
        "ok": ok,
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
        summary["ok"] = ok and probe["ok"]
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
    processes = _pm2_processes()
    ref = service_ref(region, service_type, processes)
    _run_pm2(["restart", ref.name])
    time.sleep(Config.SERVICE_STABLE_WAIT_SECONDS)
    proc = _pm2_processes().get(ref.name)
    if not proc:
        raise RuntimeError(f"PM2 service not found after restart: {ref.name}")
    return {"status": "success", "service": _process_summary(ref, {ref.name: proc})}


def restart_region(region: str) -> dict[str, Any]:
    restarted = []
    for service_type in SERVICE_TYPES:
        restarted.append(restart_service(region, service_type)["service"])
    return {"status": "success", "region": region, "services": restarted}
