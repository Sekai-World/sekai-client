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
import http.client
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

FORMAL_REGIONS = ("jp", "en", "tw", "kr")
EXPECTED_PROCESS_NAMES = {f"sharedApiClient-{r}" for r in FORMAL_REGIONS}

DEFAULT_PM2_BINARY = "pm2"
DEFAULT_HEALTH_TIMEOUT = 5.0
# A shared client that has been up for less than this many seconds counts as
# recently (re)started, so a crash loop or an unplanned restart is visible.
DEFAULT_MIN_UPTIME = 600.0
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
    args_value = pm2_env.get("args") or entry.get("args") or ""
    if isinstance(args_value, (list, tuple)):
        args = " ".join(str(arg) for arg in args_value)
    else:
        args = str(args_value)
    command = f"{script} {args}" if args else script

    result = {"online": "fail", "workers": "fail", "bind": "fail", "config": "fail"}
    result["online"] = "pass" if status == "online" else "fail"

    workers = _WORKERS_RE.search(command)
    if workers is not None and workers.group(1) == "1":
        result["workers"] = "pass"

    bind = _BIND_RE.search(command)
    if bind is not None and _is_loopback_bind(bind.group(1)):
        result["bind"] = "pass"

    config = _CONFIG_RE.search(command)
    if config is not None and config.group(1) == "gunicorn_conf.py":
        result["config"] = "pass"

    return result


def _is_stable(entry: dict, now_ms: float, min_uptime: float) -> bool:
    """True when PM2 reports the process online for at least ``min_uptime``.

    Uses only PM2's start time, so no state is kept between runs. A missing or
    non-numeric start time, or one in the future, is not stable.
    """
    pm2_env = entry.get("pm2_env")
    if not isinstance(pm2_env, dict):
        return False
    started = pm2_env.get("pm_uptime")
    if isinstance(started, bool) or not isinstance(started, (int, float)):
        return False
    if str(pm2_env.get("status", "")).lower() != "online":
        return False
    return (now_ms - started) / 1000.0 >= min_uptime


def _region_results(
    selected: dict[str, dict],
    per_region: dict[str, dict[str, str]],
    duplicates: set[str],
    now_ms: float,
    min_uptime: float,
) -> dict[str, dict[str, str]]:
    """Per-region ``pm2``/``gunicorn``/``stable`` states for the formal regions.

    A missing or duplicated process fails every check for its region.
    """
    regions = {}
    for region in FORMAL_REGIONS:
        name = f"sharedApiClient-{region}"
        checks = per_region.get(name)
        if checks is None or name in duplicates:
            regions[region] = {"pm2": "fail", "gunicorn": "fail", "stable": "fail"}
            continue
        gunicorn_ok = all(
            checks[key] == "pass" for key in ("workers", "bind", "config")
        )
        regions[region] = {
            "pm2": checks["online"],
            "gunicorn": "pass" if gunicorn_ok else "fail",
            "stable": "pass"
            if _is_stable(selected[name], now_ms, min_uptime)
            else "fail",
        }
    return regions


def validate_processes(
    entries: list[dict],
    now_ms: float | None = None,
    min_uptime: float = DEFAULT_MIN_UPTIME,
) -> dict:
    """Aggregate PM2 and Gunicorn validation across formal shared clients.

    Duplicate expected process names are detected and cause validation to fail:
    ``select_shared_client_processes`` keys on the process name, so duplicates
    would otherwise overwrite each other and silently pass the expected-process
    checks.
    """
    expected_present = [
        _process_name(entry)
        for entry in entries
        if _process_name(entry) in EXPECTED_PROCESS_NAMES
    ]
    seen: set[str] = set()
    duplicates: set[str] = set()
    for name in expected_present:
        if name in seen:
            duplicates.add(name)
        seen.add(name)

    if now_ms is None:
        now_ms = time.time() * 1000.0
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

    has_duplicates = bool(duplicates)
    all_present = checked == len(EXPECTED_PROCESS_NAMES)
    pm2_status = (
        "pass"
        if (all_present and online_ok == checked and not has_duplicates)
        else "fail"
    )
    gunicorn_ok = (
        all_present
        and workers_ok == checked
        and bind_ok == checked
        and config_ok == checked
        and not has_duplicates
    )
    gunicorn_status = "pass" if gunicorn_ok else "fail"
    regions = _region_results(selected, per_region, duplicates, now_ms, min_uptime)
    return {
        "regions": regions,
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
        "duplicate_count": len(duplicates),
    }


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: health endpoints answer directly, and a redirect target
    would bypass the base URL validation."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# An empty ProxyHandler ignores environment/system proxies so the probe goes
# straight to the validated URL and the URL never travels to a proxy.
_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), _NoRedirectHandler
)


_NOT_READY_STATUS = 503
_MAX_BODY_BYTES = 64 * 1024


def _read_capped(response) -> str:
    return response.read(_MAX_BODY_BYTES).decode("utf-8", "replace")


def http_get(url: str, timeout: float) -> tuple[int, str]:
    """Read-only HTTP GET using only the standard library.

    Returns ``(status_code, body_text)``. Raises on network or protocol errors,
    including any redirect response. Only ``http``/``https`` URLs are accepted
    by callers; this helper never performs mutating requests.
    """
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": "phase5-acceptance"},
    )
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.getcode(), _read_capped(resp)
    except urllib.error.HTTPError as error:
        # Only 503 carries information used here: the readiness endpoint
        # answers 503 with the per-region state. Every other error status
        # (including a refused redirect) still raises and counts as a failure.
        if error.code != _NOT_READY_STATUS:
            raise
        with error:
            return error.code, _read_capped(error)


_LOCAL_HOST_NAMES = frozenset({"localhost", "ip6-localhost", "ip6-loopback"})
_LOCAL_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".localdomain",
    ".internal",
    ".home.arpa",
)
# Anything but printable, non-space ASCII (plus ``%`` and ``\\``) in a host is
# rejected: the resolver applies IDNA/nameprep to non-ASCII text, which can turn
# a lookalike into a loopback or private literal after validation.
_INVALID_HOST_CHARS_RE = re.compile(r"[^\x21-\x7e]|[%\\]")
# IPv6 ranges that embed or tunnel IPv4 addresses (NAT64, 6to4, Teredo); a
# literal in these ranges can point at a non-public IPv4 destination.
_EMBEDDED_IPV4_NETWORKS = tuple(
    ipaddress.ip_network(net)
    for net in (
        "::/96",  # IPv4-compatible (deprecated)
        "::ffff:0:0:0/96",  # SIIT IPv4-translated
        "64:ff9b::/96",
        "64:ff9b:1::/48",
        "2002::/16",
        "2001::/32",
        "fec0::/10",  # deprecated site-local
    )
)


# A last label made only of digits or a hex prefix is never a real TLD; some
# resolvers (e.g. Darwin) still read such strings as IPv4 (``127.0.0.08``).
_NUMERIC_LAST_LABEL_RE = re.compile(r"0x[0-9a-f]*|[0-9]+", re.IGNORECASE)


def _is_numeric_ipv4_form(host: str) -> bool:
    """True for any IPv4 spelling a system resolver may accept (``127.1``,
    ``2130706433``, ``0x7f000001``, octal parts, leading-zero octets), which
    ``ipaddress`` rejects."""
    if _NUMERIC_LAST_LABEL_RE.fullmatch(host.rsplit(".", 1)[-1]):
        return True
    try:
        socket.inet_aton(host)
    except (OSError, ValueError, UnicodeError):
        return False
    return True


def _public_ip_literal(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if ip.is_multicast:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None and not ip.ipv4_mapped.is_global:
            return False
        if any(ip in net for net in _EMBEDDED_IPV4_NETWORKS):
            return False
    return ip.is_global


def _health_url_host(base_url: str, allow_http: bool) -> str | None:
    """Return the normalized host of a structurally acceptable base URL, or
    ``None`` when the scheme, netloc, credentials, ``?``/``#`` or host
    characters are not acceptable."""
    # ``urlparse`` silently strips tabs, newlines and surrounding whitespace, so
    # reject them before parsing to keep the validated host equal to the one
    # that would be requested.
    if re.search(r"[^\x21-\x7e]", base_url):
        return None
    try:
        parsed = urllib.parse.urlparse(base_url)
        host = parsed.hostname
        has_credentials = parsed.username is not None or parsed.password is not None
        parsed.port  # noqa: B018 - raises ValueError on an invalid port
    except ValueError:
        return None
    allowed_schemes = ("https", "http") if allow_http else ("https",)
    if parsed.scheme not in allowed_schemes or not parsed.netloc or has_credentials:
        return None
    if "?" in base_url or "#" in base_url or host is None:
        return None
    if _INVALID_HOST_CHARS_RE.search(host):
        return None
    if parsed.netloc.rpartition("@")[2].startswith("["):
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            return None
    host = host.removesuffix(".")
    if not host or host.startswith(".") or host.endswith(".") or ".." in host:
        return None
    return host


def _public_host(host: str) -> bool:
    if host in _LOCAL_HOST_NAMES or host.endswith(_LOCAL_HOST_SUFFIXES):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return not _is_numeric_ipv4_form(host)
    return _public_ip_literal(ip)


def _health_url_acceptable(base_url: str, allow_http: bool = False) -> bool:
    """Return True only if ``base_url`` is a safe, public health base.

    Rejects non-HTTPS schemes (plain ``http`` only when ``allow_http`` is set),
    missing netloc, embedded credentials, any ``?`` or ``#`` (even when empty),
    local host names (including trailing-dot and ``.local``/``.localdomain``/
    ``.internal`` forms), non-canonical numeric IPv4 spellings, and literal
    loopback/private/link-local/reserved addresses, including IPv6 forms that
    embed an IPv4 address. Uses only the standard library; no DNS or network
    resolution is performed, so a non-literal public hostname is accepted.
    """
    host = _health_url_host(base_url, allow_http)
    return host is not None and _public_host(host)


# Malformed responses raise http.client errors that urllib does not wrap; their
# messages can echo response bytes, so they must be mapped to a plain failure.
_PROBE_ERRORS = (
    urllib.error.URLError,
    urllib.error.HTTPError,
    http.client.HTTPException,
    ValueError,
    OSError,
)


def _region_states(state: str) -> dict[str, str]:
    return dict.fromkeys(FORMAL_REGIONS, state)


def _regions_from_not_ready(body: str) -> dict[str, str]:
    """Per-region readiness from a 503 readiness body.

    Only the boolean ``ready`` of each formal region is read; nothing else from
    the body is kept. Anything unparseable counts as not ready for every region.
    """
    states = _region_states("fail")
    try:
        payload = json.loads(body)
    except ValueError:
        return states
    reported = payload.get("regions") if isinstance(payload, dict) else None
    if not isinstance(reported, dict):
        return states
    for region in FORMAL_REGIONS:
        status = reported.get(region)
        if isinstance(status, dict) and status.get("ready") is True:
            states[region] = "pass"
    return states


def validate_health(
    base_url: str | None,
    timeout: float = DEFAULT_HEALTH_TIMEOUT,
    get: Callable[[str, float], tuple[int, str]] = http_get,
    allow_http: bool = False,
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
            "regions": _region_states("not_run"),
        }

    if not _health_url_acceptable(base_url, allow_http):
        return {
            "status": "fail",
            "live": "fail",
            "ready": "fail",
            "regions": _region_states("fail"),
        }

    base = base_url.rstrip("/")
    live_url = base + HEALTH_LIVE_PATH
    ready_url = base + HEALTH_READY_PATH

    result = {"live": "fail", "ready": "fail"}
    regions = _region_states("fail")
    try:
        live_code, _ = get(live_url, timeout)
    except _PROBE_ERRORS:
        result["live"] = "fail"
    else:
        result["live"] = "pass" if live_code == 200 else "fail"

    try:
        ready_code, ready_body = get(ready_url, timeout)
    except _PROBE_ERRORS:
        result["ready"] = "fail"
    else:
        result["ready"] = "pass" if ready_code == 200 else "fail"
        if ready_code == 200:
            # The endpoint answers 200 only when every region is ready.
            regions = _region_states("pass")
        elif ready_code == _NOT_READY_STATUS:
            regions = _regions_from_not_ready(ready_body)

    health_ok = result["live"] == "pass" and result["ready"] == "pass"
    status = "pass" if health_ok else "fail"
    transport = urllib.parse.urlparse(base_url).scheme
    return {
        "status": status,
        "live": result["live"],
        "ready": result["ready"],
        "regions": regions,
        "transport": transport,
    }


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
        "regions": {
            region: {"pm2": "fail", "gunicorn": "fail", "stable": "fail"}
            for region in FORMAL_REGIONS
        },
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
        "duplicate_count": 0,
    }


def run_acceptance(
    pm2_binary: str = DEFAULT_PM2_BINARY,
    health_base_url: str | None = None,
    health_timeout: float = DEFAULT_HEALTH_TIMEOUT,
    allow_not_run: bool = False,
    get: Callable[[str, float], tuple[int, str]] = http_get,
    allow_http_health: bool = False,
    min_uptime: float = DEFAULT_MIN_UPTIME,
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
        proc = validate_processes(entries, min_uptime=min_uptime)
    except (subprocess.SubprocessError, OSError, PM2ParseError):
        proc = _fail_process_sections()

    report["sections"]["pm2"] = proc["pm2"]
    report["sections"]["gunicorn"] = proc["gunicorn"]
    report["sections"]["missing_count"] = proc["missing_count"]
    report["sections"]["duplicate_count"] = proc["duplicate_count"]

    health = validate_health(
        health_base_url, health_timeout, get=get, allow_http=allow_http_health
    )
    report["sections"]["health"] = {
        "status": health["status"],
        "live": health["live"],
        "ready": health["ready"],
    }
    if "transport" in health:
        report["sections"]["health"]["transport"] = health["transport"]
    report["regions"] = {
        region: {
            "pm2": proc["regions"][region]["pm2"],
            "gunicorn": proc["regions"][region]["gunicorn"],
            "ready": health["regions"][region],
            "stable": proc["regions"][region]["stable"],
        }
        for region in FORMAL_REGIONS
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
    transport = health.get("transport")
    health_suffix = f", transport {transport}" if transport else ""
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
        "  health: {} (live {}, ready {}{})".format(
            health["status"], health["live"], health["ready"], health_suffix
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
        "--allow-http-health",
        action="store_true",
        help=(
            "Permit a plain-HTTP health base URL for deployments whose public "
            "endpoint is not served over TLS. HTTPS remains the default; all "
            "other URL restrictions still apply."
        ),
    )
    parser.add_argument(
        "--min-uptime",
        type=float,
        default=DEFAULT_MIN_UPTIME,
        help=(
            "Seconds a shared client must have been online to count as stable "
            "in the per-region report (restart visibility). Does not change "
            "the overall pass/fail."
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
        allow_http_health=args.allow_http_health,
        min_uptime=args.min_uptime,
    )
    print(format_report(report, args.format))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
