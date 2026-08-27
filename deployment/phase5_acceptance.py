"""Phase 5 production acceptance tool (read-only, redacted).

This CLI performs controlled, non-mutating validation of the deployed
shared-client topology. It never starts, stops, reloads, or otherwise mutates
PM2 processes or services. It only reads ``pm2 jlist`` (a read-only command) and,
optionally, performs read-only HTTP ``GET`` requests against the public health
endpoints.

All emitted output is aggregate and redacted. The tool never prints URLs,
response bodies, credentials, process IDs, filesystem paths, exact timestamps,
or detailed operational counters. Only aggregate status values and small
aggregate counts are emitted.

Offline/static validation may leave the public-health section as ``not_run`` and
still pass, but only when ``--allow-not-run`` is supplied. Without that flag a
``not_run`` health result (i.e. no configured public health URL) causes a
non-zero exit code, because full production acceptance requires the health
checks to actually run.

Run ``--help`` for options.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

FORMAL_REGIONS = ("jp", "en", "tw", "kr")
EXPECTED_PROCESS_NAMES = {f"sharedApiClient-{r}" for r in FORMAL_REGIONS}

DEFAULT_PM2_BINARY = "pm2"
DEFAULT_HEALTH_TIMEOUT = 5.0
HEALTH_LIVE_PATH = "/health/live"
HEALTH_READY_PATH = "/health/ready"

_WORKERS_RE = re.compile(r"--workers\s+(\d+)")
_BIND_RE = re.compile(r"--bind\s+(\S+)")
_CONFIG_RE = re.compile(r"--config\s+(\S+)")
_PM2_TIMEOUT = 30.0


def _is_loopback_bind(value: str) -> bool:
    """Return True only for a bind target of host ``127.0.0.1``.

    An optional, valid port (1-65535) is permitted. Strings such as
    ``127.0.0.10`` (a different address), ``127.0.0.1:abc`` (non-numeric port),
    ``127.0.0.1:`` (empty port), ``127.0.0.1:0``/``127.0.0.1:70000`` (out of
    range), or any non-loopback host are rejected.
    """
    if value == "127.0.0.1":
        return True
    if not value.startswith("127.0.0.1:"):
        return False
    port = value.split(":", 1)[1]
    if not port.isdigit():
        return False
    port_num = int(port)
    return 1 <= port_num <= 65535


class AcceptanceError(Exception):
    """Raised for unrecoverable input or configuration errors."""


class PM2ParseError(AcceptanceError):
    """Raised when ``pm2 jlist`` output cannot be parsed."""


def parse_pm2_jlist(raw: str) -> list[dict]:
    """Parse ``pm2 jlist`` JSON text into a list of process entries.

    Raises PM2ParseError on malformed or unexpected output. Never echoes the
    raw payload.
    """
    if not raw or not raw.strip():
        raise PM2ParseError("empty pm2 jlist output")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PM2ParseError("invalid pm2 jlist json") from exc
    if not isinstance(data, list):
        raise PM2ParseError("pm2 jlist output is not a list")
    entries: list[dict] = []
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            raise PM2ParseError(f"pm2 jlist entry {idx} is not an object")
        entries.append(item)
    return entries


def _process_name(entry: dict) -> str:
    name = entry.get("name")
    if isinstance(name, str) and name:
        return name
    pm2_env = entry.get("pm2_env")
    if isinstance(pm2_env, dict):
        env_name = pm2_env.get("name")
        if isinstance(env_name, str) and env_name:
            return env_name
    return ""


def select_shared_client_processes(entries: list[dict]) -> dict[str, dict]:
    """Return ``{process_name: entry}`` for formal shared-client processes."""
    out: dict[str, dict] = {}
    for entry in entries:
        name = _process_name(entry)
        if name in EXPECTED_PROCESS_NAMES:
            out[name] = entry
    return out


def validate_shared_client_process(name: str, entry: dict) -> dict[str, str]:
    """Validate one shared-client process. Returns per-check status strings.

    Checks: online status, exactly one Gunicorn worker, loopback bind, and the
    ``--config gunicorn_conf.py`` flag. The process name is never required to
    match the expected set again here; that is enforced by selection.
    """
    pm2_env = entry.get("pm2_env")
    if not isinstance(pm2_env, dict):
        pm2_env = {}
    status = str(pm2_env.get("status", "")).lower()
    script = str(pm2_env.get("script") or entry.get("script") or "")

    result = {"online": "fail", "workers": "fail", "bind": "fail", "config": "fail"}
    result["online"] = "pass" if status == "online" else "fail"

    workers = _WORKERS_RE.search(script)
    if workers is not None and workers.group(1) == "1":
        result["workers"] = "pass"

    bind = _BIND_RE.search(script)
    if bind is not None and _is_loopback_bind(bind.group(1)):
        result["bind"] = "pass"

    config = _CONFIG_RE.search(script)
    if config is not None and config.group(1) == "gunicorn_conf.py":
        result["config"] = "pass"

    return result


def validate_processes(entries: list[dict]) -> dict:
    """Aggregate PM2 and Gunicorn validation across formal shared clients."""
    selected = select_shared_client_processes(entries)
    per_region = {
        name: validate_shared_client_process(name, entry)
        for name, entry in selected.items()
    }
    checked = len(per_region)
    online_ok = sum(1 for r in per_region.values() if r["online"] == "pass")
    workers_ok = sum(1 for r in per_region.values() if r["workers"] == "pass")
    bind_ok = sum(1 for r in per_region.values() if r["bind"] == "pass")
    config_ok = sum(1 for r in per_region.values() if r["config"] == "pass")

    all_present = checked == len(EXPECTED_PROCESS_NAMES)
    pm2_status = "pass" if (all_present and online_ok == checked) else "fail"
    gunicorn_ok = (
        all_present
        and workers_ok == checked
        and bind_ok == checked
        and config_ok == checked
    )
    gunicorn_status = "pass" if gunicorn_ok else "fail"
    return {
        "pm2": {
            "status": pm2_status,
            "expected": len(EXPECTED_PROCESS_NAMES),
            "present": checked,
            "online": online_ok,
        },
        "gunicorn": {
            "status": gunicorn_status,
            "checked": checked,
            "workers_ok": workers_ok,
            "bind_ok": bind_ok,
            "config_ok": config_ok,
        },
        "missing_count": len(EXPECTED_PROCESS_NAMES) - checked,
    }


def http_get(url: str, timeout: float) -> tuple[int, str]:
    """Read-only HTTP GET using only the standard library.

    Returns ``(status_code, body_text)``. Raises on network or protocol errors.
    Only ``http``/``https`` URLs are accepted by callers; this helper never
    performs mutating requests.
    """
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": "phase5-acceptance"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.getcode(), resp.read().decode("utf-8", "replace")


def validate_health(
    base_url: str | None,
    timeout: float = DEFAULT_HEALTH_TIMEOUT,
    get: Callable[[str, float], tuple[int, str]] = http_get,
) -> dict:
    """Validate the public health endpoints without leaking details.

    Returns aggregate status only. The base URL and any response body are never
    placed in the returned structure. When ``base_url`` is empty/unset the
    result is ``not_run`` (callers decide whether that is acceptable).
    """
    if not base_url:
        return {
            "status": "not_run",
            "live": "not_run",
            "ready": "not_run",
        }

    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in ("http", "https"):
        return {"status": "fail", "live": "fail", "ready": "fail"}
    if not parsed.netloc:
        return {"status": "fail", "live": "fail", "ready": "fail"}
    if parsed.username is not None or parsed.password is not None:
        return {"status": "fail", "live": "fail", "ready": "fail"}
    if parsed.query or parsed.fragment:
        return {"status": "fail", "live": "fail", "ready": "fail"}

    base = base_url.rstrip("/")
    live_url = base + HEALTH_LIVE_PATH
    ready_url = base + HEALTH_READY_PATH

    result = {"live": "fail", "ready": "fail"}
    try:
        live_code, _ = get(live_url, timeout)
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError):
        result["live"] = "fail"
    else:
        result["live"] = "pass" if live_code == 200 else "fail"

    try:
        ready_code, _ = get(ready_url, timeout)
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError):
        result["ready"] = "fail"
    else:
        result["ready"] = "pass" if ready_code == 200 else "fail"

    health_ok = result["live"] == "pass" and result["ready"] == "pass"
    status = "pass" if health_ok else "fail"
    return {"status": status, "live": result["live"], "ready": result["ready"]}


def summarize(sections: dict, allow_not_run: bool) -> tuple[str, int]:
    """Compute overall status and process exit code from section results."""
    states = [
        sections["pm2"]["status"],
        sections["gunicorn"]["status"],
        sections["health"]["status"],
    ]
    if any(state == "fail" for state in states):
        return "fail", 1
    if sections["health"]["status"] == "not_run":
        if allow_not_run:
            return "pass", 0
        return "fail", 1
    return "pass", 0


def _fail_process_sections() -> dict:
    return {
        "pm2": {
            "status": "fail",
            "expected": len(EXPECTED_PROCESS_NAMES),
            "present": 0,
            "online": 0,
        },
        "gunicorn": {
            "status": "fail",
            "checked": 0,
            "workers_ok": 0,
            "bind_ok": 0,
            "config_ok": 0,
        },
        "missing_count": len(EXPECTED_PROCESS_NAMES),
    }


def run_acceptance(
    pm2_binary: str = DEFAULT_PM2_BINARY,
    health_base_url: str | None = None,
    health_timeout: float = DEFAULT_HEALTH_TIMEOUT,
    allow_not_run: bool = False,
    get: Callable[[str, float], tuple[int, str]] = http_get,
) -> tuple[int, dict]:
    """Run the full read-only acceptance check.

    Returns ``(exit_code, report)``. ``report`` contains only aggregate,
    redacted fields.
    """
    report: dict = {"acceptance": "phase5", "sections": {}}

    try:
        completed = subprocess.run(
            [pm2_binary, "jlist"],
            capture_output=True,
            text=True,
            timeout=_PM2_TIMEOUT,
        )
        entries = parse_pm2_jlist(completed.stdout)
        proc = validate_processes(entries)
    except (subprocess.SubprocessError, OSError, PM2ParseError):
        proc = _fail_process_sections()

    report["sections"]["pm2"] = proc["pm2"]
    report["sections"]["gunicorn"] = proc["gunicorn"]
    report["sections"]["missing_count"] = proc["missing_count"]

    health = validate_health(health_base_url, health_timeout, get=get)
    report["sections"]["health"] = {
        "status": health["status"],
        "live": health["live"],
        "ready": health["ready"],
    }

    overall, exit_code = summarize(report["sections"], allow_not_run)
    report["status"] = overall
    report["exit_code"] = exit_code
    return exit_code, report


def format_report(report: dict, fmt: str = "text") -> str:
    """Render the report as redacted text or JSON."""
    if fmt == "json":
        return json.dumps(report, sort_keys=True)

    sections = report["sections"]
    pm2 = sections["pm2"]
    gunicorn = sections["gunicorn"]
    health = sections["health"]
    lines = [
        f"phase5 acceptance: {report['status']}",
        "  pm2: {} (expected {}, present {}, online {})".format(
            pm2["status"], pm2["expected"], pm2["present"], pm2["online"]
        ),
        "  gunicorn: {} (checked {}, workers {}, bind {}, config {})".format(
            gunicorn["status"],
            gunicorn["checked"],
            gunicorn["workers_ok"],
            gunicorn["bind_ok"],
            gunicorn["config_ok"],
        ),
        "  health: {} (live {}, ready {})".format(
            health["status"], health["live"], health["ready"]
        ),
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 5 read-only production acceptance: aggregate, redacted "
            "PM2/Gunicorn and public-health validation."
        )
    )
    parser.add_argument(
        "--pm2-binary",
        default=DEFAULT_PM2_BINARY,
        help="PM2 binary/command to invoke for the read-only `jlist` call.",
    )
    parser.add_argument(
        "--health-base-url",
        default=os.environ.get("PHASE5_PUBLIC_HEALTH_BASE_URL", ""),
        help=(
            "Public health base URL (e.g. https://<host>). When empty the "
            "health section is `not_run`. Configure via this flag or the "
            "PHASE5_PUBLIC_HEALTH_BASE_URL environment variable."
        ),
    )
    parser.add_argument(
        "--health-timeout",
        type=float,
        default=DEFAULT_HEALTH_TIMEOUT,
        help="Per-request timeout (seconds) for health probes.",
    )
    parser.add_argument(
        "--allow-not-run",
        action="store_true",
        help=(
            "Permit a `not_run` health result (offline/static validation only). "
            "Without this flag, a missing health URL fails acceptance."
        ),
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format.",
    )
    args = parser.parse_args(argv)

    exit_code, report = run_acceptance(
        pm2_binary=args.pm2_binary,
        health_base_url=args.health_base_url,
        health_timeout=args.health_timeout,
        allow_not_run=args.allow_not_run,
    )
    print(format_report(report, args.format))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
