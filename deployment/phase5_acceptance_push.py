"""Run the Phase 5 acceptance and push the aggregate result to a monitor.

Designed for an Uptime Kuma *Push* monitor (any service that accepts a
``GET <push-url>?status=up|down&msg=...`` heartbeat works). Run it from a
scheduler on the deployment host; the monitor alerts when a section fails
(``status=down``) and when the heartbeat stops arriving (the scheduler or the
host is down).

The push URL carries a secret token, so it is read only from the
``PHASE5_PUSH_URL`` environment variable and is never printed, logged or put
in an exception message. The pushed message contains only the three aggregate
section states (``pm2``, ``gunicorn``, ``health``); nothing else from the
acceptance report is sent.

Exit code: the acceptance exit code, or ``2`` when the push itself failed or
is not configured.
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
_STATES = frozenset({"pass", "fail", "not_run"})
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def build_message(report: dict) -> str:
    """Aggregate-only message: ``pm2=pass gunicorn=pass health=fail``."""
    parts = []
    for name in _SECTIONS:
        state = report.get("sections", {}).get(name, {}).get("status")
        parts.append(f"{name}={state if state in _STATES else 'fail'}")
    return " ".join(parts)


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
    parser.add_argument("--push-timeout", type=float, default=DEFAULT_PUSH_TIMEOUT)
    args = parser.parse_args(argv)

    if not os.environ.get(PUSH_URL_ENV, "").strip():
        print(f"push monitor not configured: set {PUSH_URL_ENV}", file=sys.stderr)
        return EXIT_PUSH_FAILED

    try:
        exit_code, report = acceptance.run_acceptance(
            pm2_binary=args.pm2_binary,
            health_base_url=args.health_base_url,
            health_timeout=args.health_timeout,
            allow_not_run=False,
            allow_http_health=args.allow_http_health,
        )
    except Exception:  # noqa: BLE001
        # Report as a failed run without any error detail.
        exit_code, report = 1, {}
    print(acceptance.format_report(report, "text") if report else "acceptance error")

    url = build_push_url(
        os.environ[PUSH_URL_ENV], exit_code == 0, build_message(report)
    )
    if url is None or not push(url, args.push_timeout):
        print("push failed", file=sys.stderr)
        return EXIT_PUSH_FAILED
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
