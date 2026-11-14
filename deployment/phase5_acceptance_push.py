"""Run the Phase 5 acceptance and push the aggregate result to a monitor.

Designed for an Uptime Kuma *Push* monitor (any service that accepts a
``GET <push-url>?status=up|down&msg=...`` heartbeat works). Run it from a
scheduler on the deployment host; the monitor alerts when a section fails
(``status=down``) and when the heartbeat stops arriving (the scheduler or the
host is down).

Monitors are configured independently through environment variables; each one
that is set receives its own heartbeat:

- ``PHASE5_PUSH_URL``: overall acceptance. The message carries only the three
  aggregate section states (``pm2``, ``gunicorn``, ``health``).
- ``PHASE5_PUSH_URL_JP`` / ``_EN`` / ``_TW`` / ``_KR``: one region. Up only when
  that region's PM2 process, Gunicorn launch settings, readiness and stability
  (online for at least ``--min-uptime`` seconds, default 600) all pass; the
  message carries only those four states.

Each push URL carries a secret token, so it is read only from the environment
and is never printed, logged or put in an exception message.

Exit code: the acceptance exit code, or ``2`` when any push failed or no push
URL is configured.
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
import urllib.request

import phase5_acceptance as acceptance

PUSH_URL_ENV = "PHASE5_PUSH_URL"
DEFAULT_PUSH_TIMEOUT = 10.0
EXIT_PUSH_FAILED = 2
_SECTIONS = ("pm2", "gunicorn", "health")
_REGION_CHECKS = ("pm2", "gunicorn", "ready", "stable")
_STATES = frozenset({"pass", "fail", "not_run"})
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def build_message(report: dict) -> str:
    """Aggregate-only message: ``pm2=pass gunicorn=pass health=fail``."""
    parts = []
    for name in _SECTIONS:
        state = report.get("sections", {}).get(name, {}).get("status")
        parts.append(f"{name}={state if state in _STATES else 'fail'}")
    return " ".join(parts)


def region_push_env(region: str) -> str:
    return f"{PUSH_URL_ENV}_{region.upper()}"


def build_region_message(report: dict, region: str) -> tuple[bool, str]:
    """Aggregate-only region result: ``(is_up, "pm2=pass gunicorn=... ready=...")``."""
    states = report.get("regions", {}).get(region, {})
    parts = {
        name: states.get(name) if states.get(name) in _STATES else "fail"
        for name in _REGION_CHECKS
    }
    message = " ".join(f"{name}={value}" for name, value in parts.items())
    return all(value == "pass" for value in parts.values()), message


def build_push_url(base_url: str, is_up: bool, message: str) -> str | None:
    """Return the heartbeat URL, or ``None`` if ``base_url`` is not http(s)."""
    try:
        parsed = urllib.parse.urlparse(base_url.strip())
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    query = urllib.parse.urlencode(
        {"status": "up" if is_up else "down", "msg": message, "ping": ""}
    )
    return urllib.parse.urlunparse(parsed._replace(query=query, fragment=""))


def push(url: str, timeout: float) -> bool:
    """Send the heartbeat; True only on a 2xx response. Never raises."""
    try:
        request = urllib.request.Request(url, method="GET")
        with _OPENER.open(request, timeout=timeout) as response:
            return 200 <= response.status < 300
    except Exception:  # noqa: BLE001
        # Never surface error text: it can contain the secret push URL.
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the Phase 5 acceptance and push the aggregate result to a "
            f"push monitor whose URL is read from ${PUSH_URL_ENV}."
        )
    )
    parser.add_argument("--pm2-binary", default=acceptance.DEFAULT_PM2_BINARY)
    parser.add_argument(
        "--health-base-url",
        default=os.environ.get("PHASE5_PUBLIC_HEALTH_BASE_URL", ""),
    )
    parser.add_argument(
        "--health-timeout", type=float, default=acceptance.DEFAULT_HEALTH_TIMEOUT
    )
    parser.add_argument("--allow-http-health", action="store_true")
    parser.add_argument(
        "--min-uptime", type=float, default=acceptance.DEFAULT_MIN_UPTIME
    )
    parser.add_argument("--push-timeout", type=float, default=DEFAULT_PUSH_TIMEOUT)
    args = parser.parse_args(argv)

    configured = {
        name: os.environ[name].strip()
        for name in (PUSH_URL_ENV, *map(region_push_env, acceptance.FORMAL_REGIONS))
        if os.environ.get(name, "").strip()
    }
    if not configured:
        print(f"push monitor not configured: set {PUSH_URL_ENV}", file=sys.stderr)
        return EXIT_PUSH_FAILED

    try:
        exit_code, report = acceptance.run_acceptance(
            pm2_binary=args.pm2_binary,
            health_base_url=args.health_base_url,
            health_timeout=args.health_timeout,
            allow_not_run=False,
            allow_http_health=args.allow_http_health,
            min_uptime=args.min_uptime,
        )
    except Exception:  # noqa: BLE001
        # Report as a failed run without any error detail.
        exit_code, report = 1, {}
    print(acceptance.format_report(report, "text") if report else "acceptance error")

    heartbeats = []
    if PUSH_URL_ENV in configured:
        heartbeats.append((PUSH_URL_ENV, exit_code == 0, build_message(report)))
    for region in acceptance.FORMAL_REGIONS:
        name = region_push_env(region)
        if name in configured:
            is_up, message = build_region_message(report, region)
            heartbeats.append((name, is_up, message))

    all_pushed = True
    for name, is_up, message in heartbeats:
        url = build_push_url(configured[name], is_up, message)
        if url is None or not push(url, args.push_timeout):
            all_pushed = False
    if not all_pushed:
        print("push failed", file=sys.stderr)
        return EXIT_PUSH_FAILED
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
