"""
Public Flask API server for sekai-client JSON-RPC functionality.

Provides HTTP REST endpoints that proxy requests to region-specific
JSON-RPC servers running on localhost. Includes health check,
user profile fetching, and event ranking endpoints.

Authentication:
- All endpoints (except /health) require 'x-api-token' header
- Token is checked against API_TOKEN environment variable

Health Check Semantics:
- Returns 200 only if ALL regions are healthy (strict AND semantics)
- Returns 500 if ANY region is down
- Provides per-region status in response for debugging
"""

import logging
from concurrent.futures import ThreadPoolExecutor, wait
from math import ceil
from pathlib import Path
from typing import Any

from flask import Flask, Response, json, jsonify, send_file
from werkzeug.exceptions import BadRequest
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.wrappers import Response as WerkzeugResponse

from config import Config
from logging_config import enable_log_redaction
from service_dashboard import dashboard_status, restart_region, restart_service
from utils.decorators import require_apikey
from utils.jsonrpc_client import JSONRPCClient

enable_log_redaction()
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.wsgi_app = ProxyFix(  # type: ignore[method-assign]
    app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1
)

# Initialize region-specific JSON-RPC clients. CN is excluded (not a formal
# region; only a standalone simplified checkUpdate-cn process, see D-001).
client_map: dict[str, JSONRPCClient] = {
    "jp": JSONRPCClient(f"http://localhost:{Config.JP_PORT}/"),
    "tw": JSONRPCClient(f"http://localhost:{Config.TW_PORT}/"),
    "en": JSONRPCClient(f"http://localhost:{Config.EN_PORT}/"),
    "kr": JSONRPCClient(f"http://localhost:{Config.KR_PORT}/"),
}

# This is deliberately only a probe timeout.  Readiness is a pure RPC on the
# shared client and must never turn into an initialization attempt.  A public
# request must not wait for an unhealthy region while checking aggregate health.
READINESS_PROBE_TIMEOUT = 5.0


class RegionalLifecycleUnavailable(Exception):
    """A known region cannot currently serve authenticated requests."""

    def __init__(self, status: dict[str, Any]) -> None:
        super().__init__(status.get("reason", "region is not ready"))
        self.status = status


def _validate_public_config() -> None:
    """Fail fast on invalid region mappings without contacting any region."""
    region_errors = Config.validate_region_config()
    if region_errors:
        raise RuntimeError(
            "Region configuration is incomplete; refusing to start: "
            + "; ".join(region_errors)
        )


# Validate pure configuration at process startup.  The request hook below
# repeats this check for workers which were started before a configuration
# reload and, importantly, performs no remote lifecycle work.
_validate_public_config()

# Kept only so older in-process integrations that inspect this name do not
# fail.  It is never read and cannot authorize or initialize a request.
bootstrapped = False


@app.errorhandler(BadRequest)
def handle_bad_request(e: BadRequest) -> WerkzeugResponse:
    """
    Handle BadRequest exceptions with JSON response.

    Args:
        e: BadRequest exception

    Returns:
        JSON error response
    """
    response = e.get_response()
    response.data = json.dumps({"status": "error", "message": e.description})
    response.content_type = "application/json"
    return response


def get_regional_client(region: str) -> JSONRPCClient:
    """
    Get or initialize a regional JSON-RPC client.

    Args:
        region: Region code ('jp', 'en', 'cn', 'tw', 'kr')

    Returns:
        JSONRPCClient for the region.  Lifecycle is intentionally not checked
        here; callers that need authenticated work use
        :func:`get_ready_regional_client`.

    Raises:
        BadRequest: If region is not found
    """
    client = client_map.get(region, None)

    if not client:
        raise BadRequest("No such region.")
    return client


def _safe_lifecycle_status(
    region: str, result: object, *, unavailable: bool = False
) -> dict[str, Any]:
    """Normalize a lifecycle response without forwarding remote error text."""
    raw = result if isinstance(result, dict) else {}
    ready = bool(raw.get("ready", result is True))
    retry_after = raw.get("retry_after")
    if not isinstance(retry_after, (int, float)) or retry_after <= 0:
        retry_after = None

    state = raw.get("state")
    if not isinstance(state, str):
        state = "UNAVAILABLE" if unavailable else ("READY" if ready else "DEGRADED")

    if ready:
        reason = None
    elif unavailable:
        reason = "lifecycle RPC unavailable"
    elif isinstance(raw.get("error"), dict):
        reason = "lifecycle operation failed"
    elif state == "UNINITIALIZED":
        reason = "region is not initialized"
    elif state == "FAILED":
        reason = "region lifecycle failed"
    else:
        reason = "region is not ready"

    return {
        "region": region,
        "state": state,
        "initialized": bool(raw.get("initialized", False)),
        "authenticated": bool(raw.get("authenticated", False)),
        "ready": ready,
        "retry_after": retry_after,
        "next_retry_at": raw.get("next_retry_at")
        if isinstance(raw.get("next_retry_at"), str)
        else None,
        "reason": reason,
    }


def _lifecycle_error_status(region: str, error: BaseException) -> dict[str, Any]:
    """Create a redacted status for an unreachable or malformed RPC response."""
    # The exception object is intentionally not serialized.  Its text may
    # contain credentials or an upstream response body.
    logger.warning("Lifecycle RPC unavailable for %s: %s", region, type(error).__name__)
    return _safe_lifecycle_status(region, {}, unavailable=True)


def get_ready_regional_client(region: str) -> JSONRPCClient:
    """Return a known-region client after a target-only readiness transition."""
    client = get_regional_client(region)
    try:
        lifecycle = client.request("ensure_ready", [])
    except Exception as error:
        raise RegionalLifecycleUnavailable(
            _lifecycle_error_status(region, error)
        ) from error

    status = _safe_lifecycle_status(region, lifecycle)
    if not status["ready"]:
        raise RegionalLifecycleUnavailable(status)
    return client


def _regional_lifecycle_response(
    error: RegionalLifecycleUnavailable,
) -> tuple[Response, int]:
    """Return the stable public response for a known but unavailable region."""
    status = error.status
    response = jsonify(
        {
            "status": "error",
            "code": "region_not_ready",
            "region": status["region"],
            "reason": status["reason"],
            "lifecycle": status,
        }
    )
    retry_after = status.get("retry_after")
    if isinstance(retry_after, (int, float)) and retry_after > 0:
        response.headers["Retry-After"] = str(max(1, ceil(retry_after)))
    return response, 503


@app.errorhandler(RegionalLifecycleUnavailable)
def handle_regional_lifecycle_unavailable(
    error: RegionalLifecycleUnavailable,
) -> tuple[Response, int]:
    return _regional_lifecycle_response(error)


def init_regional_client(region: str) -> None:
    """
    Initialize a regional JSON-RPC client.

    Calls init, check_versions, and login methods on the client.

    Args:
        region: Region code
    """
    if not is_regional_client_inited(region):
        client_map[region].request("init", [region])

    client_map[region].request("check_versions", [])
    client_map[region].request("login", [])


def is_regional_client_inited(region: str) -> bool:
    """
    Check if a regional client is initialized.

    Args:
        region: Region code

    Returns:
        True if client is initialized, False otherwise
    """
    return bool(client_map[region].request("is_init", []))


def bootstrap() -> None:
    """
    Validate public configuration for legacy callers.

    Historically this function initialized every region and stored a global
    ``bootstrapped`` flag.  It is retained as a compatibility entry point, but
    is now pure and has no lifecycle state or network side effects.
    """
    _validate_public_config()


@app.before_request
def validate_public_request_config() -> None:
    """Validate pure public configuration; never bootstrap remote regions."""
    _validate_public_config()


def _readiness_probe(region: str) -> dict[str, Any]:
    """Read one region's lifecycle snapshot without mutating it."""
    client = client_map[region]
    try:
        return _safe_lifecycle_status(
            region,
            client.request("readiness", [], timeout=READINESS_PROBE_TIMEOUT),
        )
    except Exception as error:
        return _lifecycle_error_status(region, error)


def _collect_readiness() -> dict[str, dict[str, Any]]:
    """Probe all regions concurrently, without initialization or login."""
    # The timeout is enforced by each JSONRPCClient call.  The context manager
    # can therefore join every worker without leaving requests or executor
    # threads running after this function returns.
    with ThreadPoolExecutor(max_workers=max(1, len(client_map))) as executor:
        futures = {
            executor.submit(_readiness_probe, region): region for region in client_map
        }
        wait(futures)
        statuses: dict[str, dict[str, Any]] = {}
        for future, region in futures.items():
            try:
                statuses[region] = future.result()
            except Exception as error:
                statuses[region] = _lifecycle_error_status(region, error)
        return statuses


def _health_payload(
    statuses: dict[str, dict[str, Any]], *, legacy_regions: bool = False
) -> dict[str, Any]:
    all_ready = all(status["ready"] for status in statuses.values())
    payload: dict[str, Any] = {
        "status": "success" if all_ready else "error",
        "healthyRegions": sum(1 for status in statuses.values() if status["ready"]),
        "totalRegions": len(statuses),
        "regions": {
            region: status["ready"] if legacy_regions else status
            for region, status in statuses.items()
        },
    }
    if legacy_regions:
        payload["lifecycle"] = statuses
    return payload


@app.route("/health/live", methods=["GET"])
def health_live() -> tuple[Response, int]:
    """Cheap process liveness endpoint with no regional RPC calls."""
    return jsonify({"status": "success", "ok": True}), 200


@app.route("/health/ready", methods=["GET"])
def health_ready() -> tuple[Response, int]:
    """Aggregate, read-only readiness probe with per-region reasons."""
    statuses = _collect_readiness()
    all_ready = all(status["ready"] for status in statuses.values())
    return jsonify(_health_payload(statuses)), 200 if all_ready else 503


@app.route("/health", methods=["GET"])
def health() -> tuple[Response, int]:
    """
    Health check endpoint for monitoring.

    Returns 200 only if ALL regions are healthy (fail-safe behavior).
    Returns 500 if ANY region is down.

    Returns:
        JSON response with per-region status and health code
    """
    statuses = _collect_readiness()
    all_ready = all(status["ready"] for status in statuses.values())
    return jsonify(_health_payload(statuses, legacy_regions=True)), (
        200 if all_ready else 500
    )


@app.route("/dashboard", methods=["GET"])
def dashboard() -> Response:
    return send_file(Path(__file__).parent / "dashboard" / "index.html")


@app.route("/dashboard/api/status", methods=["GET"])
@require_apikey
def dashboard_api_status() -> Response | tuple[Response, int]:
    try:
        return jsonify(dashboard_status())
    except RuntimeError as err:
        return jsonify({"status": "error", "message": str(err)}), 500


@app.route(
    "/dashboard/api/regions/<region>/services/<service_type>/restart", methods=["POST"]
)
@require_apikey
def dashboard_restart_service(
    region: str, service_type: str
) -> Response | tuple[Response, int]:
    try:
        result = restart_service(region, service_type)
        return _normalize_restart_response(result, 200)
    except (RuntimeError, ValueError) as err:
        return jsonify({"status": "error", "message": str(err)}), 400


@app.route("/dashboard/api/regions/<region>/restart", methods=["POST"])
@require_apikey
def dashboard_restart_region(region: str) -> Response | tuple[Response, int]:
    try:
        result = restart_region(region)
        return _normalize_restart_response(result, 200)
    except (RuntimeError, ValueError) as err:
        return jsonify({"status": "error", "message": str(err)}), 400


def _normalize_restart_response(
    result: dict[str, object], ok_code: int
) -> Response | tuple[Response, int]:
    """Preserve the legacy top-level ``status`` field for API compatibility.

    Older clients read ``result["status"]``; we derive it from the structured
    ``restartStatus`` so both coexist without contradiction.

    - "success"      -> status "success"     (HTTP 200)
    - "restart_failed"  -> status "error"    (HTTP 400)
    - "refresh_failed"  -> status "partial"  (HTTP 200: restart ran, but
      health could not be confirmed — still useful, not a hard error)
    """
    restart_status = result.get("restartStatus", "success")
    if restart_status == "success":
        legacy = "success"
        code = ok_code
    elif restart_status == "restart_failed":
        legacy = "error"
        code = 400
    else:  # refresh_failed
        legacy = "partial"
        code = ok_code
    return jsonify({**result, "status": legacy}), code


@app.route("/<region>/refresh", methods=["POST"])
@require_apikey
def refresh_regional_client(region: str) -> Response:
    """
    Force relogin for a specific region.

    Args:
        region: Region code

    Returns:
        Success response
    """
    client = get_ready_regional_client(region)
    client.request("relogin")

    return jsonify({"status": "success"})


@app.route("/<region>/user/<user_id>/profile", methods=["GET"])
@require_apikey
def fetch_user_profile_by_user_id(region: str, user_id: str) -> Response:
    """
    Fetch user profile by user ID.

    Args:
        region: Region code
        user_id: Target user ID

    Returns:
        JSON response with user profile
    """
    client = get_ready_regional_client(region)
    user_profile = client.request("fetch_user_profile", [user_id])

    return jsonify({"status": "success", "data": user_profile})


@app.route("/<region>/user/<target_user_id>/event/<event_id>/ranking", methods=["GET"])
@require_apikey
def fetch_event_ranking_by_user_id(
    region: str, target_user_id: str, event_id: str
) -> Response:
    """
    Fetch user's event ranking.

    Args:
        region: Region code
        target_user_id: Target user ID
        event_id: Event ID

    Returns:
        JSON response with event ranking data
    """
    client = get_ready_regional_client(region)
    user_profile = client.request(
        "fetch_user_event_ranking", [target_user_id, event_id]
    )

    return jsonify({"status": "success", "data": user_profile})
