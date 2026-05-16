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
from threading import Lock
from typing import Any

from flask import Flask, Response, json, jsonify
from werkzeug.exceptions import BadRequest
from werkzeug.middleware.proxy_fix import ProxyFix

from config import Config
from utils.decorators import require_apikey
from utils.jsonrpc_client import JSONRPCClient

logger = logging.getLogger(__name__)

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Initialize region-specific JSON-RPC clients
client_map: dict[str, JSONRPCClient] = {
    "jp": JSONRPCClient(f"http://localhost:{Config.JP_PORT}/"),
    "tw": JSONRPCClient(f"http://localhost:{Config.TW_PORT}/"),
    "en": JSONRPCClient(f"http://localhost:{Config.EN_PORT}/"),
    "kr": JSONRPCClient(f"http://localhost:{Config.KR_PORT}/"),
    "cn": JSONRPCClient(f"http://localhost:{Config.CN_PORT}/"),
}
bootstrap_lock = Lock()
bootstrapped = False


@app.errorhandler(BadRequest)
def handle_bad_request(e: BadRequest) -> Response:
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
        Initialized JSONRPCClient for the region

    Raises:
        BadRequest: If region not found or initialization fails
    """
    client = client_map.get(region, None)

    if not client:
        raise BadRequest("No such region.")
    if not is_regional_client_inited(region):
        try:
            init_regional_client(region)
        except Exception as err:
            logger.exception("Failed to init regional client: %s", region)
            raise BadRequest(f"Failed to init {region} client") from err

    return client


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
    return client_map[region].request("is_init", [])


def bootstrap() -> None:
    """
    Initialize all regional clients on startup.

    Uses double-checked locking pattern to ensure initialization
    happens only once across concurrent requests.
    """
    global bootstrapped
    if bootstrapped:
        return

    with bootstrap_lock:
        if bootstrapped:
            return

        for region in client_map:
            try:
                init_regional_client(region)
            except Exception:
                logger.exception("skip %s region for bootstrap", region)

        bootstrapped = True


@app.before_request
def ensure_bootstrap() -> None:
    """Ensure bootstrap has run before handling requests."""
    bootstrap()


@app.route("/health", methods=["GET"])
def health() -> tuple[dict[str, Any], int]:
    """
    Health check endpoint for monitoring.

    Returns 200 only if ALL regions are healthy (fail-safe behavior).
    Returns 500 if ANY region is down.

    Returns:
        JSON response with per-region status and health code
    """
    region_status: dict[str, bool] = {}
    for region in client_map:
        try:
            region_status[region] = bool(
                client_map.get(region) and is_regional_client_inited(region)
            )
        except Exception:
            region_status[region] = False

    healthy_count = sum(1 for ok in region_status.values() if ok)
    is_healthy = all(region_status.values())

    return jsonify(
        {
            "status": "success" if is_healthy else "error",
            "healthyRegions": healthy_count,
            "totalRegions": len(region_status),
            "regions": region_status,
        }
    ), 200 if is_healthy else 500


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
    client = get_regional_client(region)
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
    client = get_regional_client(region)
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
    client = get_regional_client(region)
    user_profile = client.request(
        "fetch_user_event_ranking", [target_user_id, event_id]
    )

    return jsonify({"status": "success", "data": user_profile})
