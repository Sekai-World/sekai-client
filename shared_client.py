"""
JSON-RPC API server for Project Sekai game client functionality.

Exposes the sekai-client Python library as a JSON-RPC service,
allowing external clients to access game functionality via HTTP.
Runs per-request background jobs with queue-based task distribution
and automatic daily account refresh on schedule.

Regional Ports (configurable via environment):
- Japan (jp): 39390 (JP_PORT)
- Taiwan (tw): 39391 (TW_PORT)
- English (en): 39392 (EN_PORT)
- Korea (kr): 39393 (KR_PORT)
- China (cn): 39394 (CN_PORT)
"""

import logging
import os
import queue
import tempfile
from collections.abc import Callable
from copy import deepcopy
from hmac import compare_digest
from os import getenv, path
from threading import Lock
from typing import Any

import jwt
import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask, abort, request
from jsonrpc.exceptions import JSONRPCDispatchException, JSONRPCInternalError
from pytz import timezone

from api_client import APIClient
from config import Config
from logging_config import enable_log_redaction
from utils.constants import pjsk_region
from utils.jsonrpc_client import INTERNAL_RPC_TOKEN_HEADER
from utils.redaction import redact_structure, redact_text
from utils.task_queue import job_queue, start_worker
from utils.ujsonrpcapi import api

enable_log_redaction()
logger = logging.getLogger(__name__)

dirname = path.dirname(__file__)

# Header used to authenticate internal JSON-RPC calls between the
# sekai-client processes. All such calls must run on loopback only.

# Global state for the JSON-RPC server
api_client: APIClient | None = None
client_region: str = pjsk_region
user_logged_in: bool = False
user_info: dict[str, Any] | None = None
scheduler_start_lock = Lock()
scheduler_started: bool = False

start_worker()


def enqueue_job(
    job: Callable[[], Any],
) -> tuple[queue.Queue[Any] | None, JSONRPCInternalError | None]:
    """
    Enqueue a job for background processing.

    Args:
        job: Callable that will be executed by the worker thread

    Returns:
        Tuple of (response_queue, error). If error is not None,
        the job was not enqueued and response_queue is None.
    """
    response_queue: queue.Queue[Any] = queue.Queue(maxsize=1)
    try:
        job_queue.put((job, response_queue), timeout=Config.JOB_QUEUE_TIMEOUT)
    except queue.Full:
        return None, JSONRPCInternalError(data="Job queue is full, please retry later")
    return response_queue, None


def get_answer(response_queue: queue.Queue[Any]) -> Any | JSONRPCInternalError:
    """
    Wait for a job result from the worker thread.

    Args:
        response_queue: Queue to wait on for the result

    Returns:
        Job result, or JSONRPCInternalError if timeout or exception occurred
    """
    try:
        res: Any = response_queue.get(timeout=Config.ANSWER_QUEUE_TIMEOUT)
    except queue.Empty:
        return JSONRPCInternalError(data="Timed out waiting for worker response")

    if isinstance(res, RuntimeError):
        err_data = str(res)
        if len(res.args) > 1:
            err_data = str(res.args[1])
        return JSONRPCInternalError(data=err_data)
    elif isinstance(res, Exception):
        return JSONRPCInternalError(data=str(res))
    return res


def run_job(job: Callable[[], Any]) -> Any:
    """
    Enqueue a job and wait for its result.

    Orchestrates enqueue_job + get_answer with proper error handling.

    Args:
        job: Callable to execute in background worker

    Returns:
        Job result

    Raises:
        JSONRPCDispatchException: If the queued job failed or timed out
    """
    response_queue, err = enqueue_job(job)
    result: Any
    if err is not None:
        result = err
    elif response_queue is None:
        result = JSONRPCInternalError(data="Job was not enqueued")
    else:
        result = get_answer(response_queue)

    if isinstance(result, JSONRPCInternalError):
        # Redact any secret that leaked into the error payload before
        # it is serialized back to the (internal) caller.
        redacted_data = result.data
        if isinstance(redacted_data, (dict, list, tuple)):
            redacted_data = redact_structure(redacted_data)
        elif isinstance(redacted_data, str):
            redacted_data = redact_text(redacted_data)
        raise JSONRPCDispatchException(
            code=JSONRPCInternalError.CODE,
            message=JSONRPCInternalError.MESSAGE,
            data=redacted_data,
        )
    return result


def day_change_func() -> None:
    """Scheduled job to relogin once per day (at 4 AM JST)."""
    if user_logged_in:
        try:
            run_job(lambda: login_account(True))
        except JSONRPCDispatchException as error:
            logger.error("Scheduled daily relogin failed: %s", error.error.data)


# Background scheduler for daily account refresh
scheduler = BackgroundScheduler(timezone=timezone("Asia/Tokyo"))
cron_trigger = CronTrigger(
    hour="4", minute="0", second="0", timezone=timezone("Asia/Tokyo")
)
day_change_job = scheduler.add_job(day_change_func, cron_trigger, name="day_change_job")


def require_api_client() -> APIClient:
    """Return the initialized API client or fail with the public API error."""
    if api_client is None:
        raise RuntimeError("Init before calling this method")
    return api_client


def _snapshot_client_state(client: APIClient) -> dict[str, Any]:
    return {
        "headers": deepcopy(client.headers),
        "account_info": deepcopy(client.account_info),
        "version_info": deepcopy(client.version_info),
        "master_split_paths": deepcopy(client.master_split_paths),
        "user_info": deepcopy(client.user_info),
    }


def _restore_client_state(client: APIClient, state: dict[str, Any]) -> None:
    client.headers = state["headers"]
    client.account_info = state["account_info"]
    client.version_info = state["version_info"]
    client.master_split_paths = state["master_split_paths"]
    client.user_info = state["user_info"]


def _enforce_account_yaml_permissions(filepath: str) -> None:
    """
    Ensure an existing account YAML file is not world/readable.

    On POSIX, chmod the file to 0600 (best-effort: ignores
    errors so non-POSIX platforms such as Windows are unaffected). Callers
    that read the file should not print its contents.
    """
    try:
        if os.stat(filepath).st_mode & 0o077:
            os.chmod(filepath, 0o600)
    except OSError:
        pass


def _write_account_yaml_atomic(filepath: str, account_info: dict[str, Any]) -> None:
    """
    Write account YAML atomically with restrictive permissions.

    Writes to a temp file in the same directory, flushes + fsyncs, then
    atomically ``os.replace`` onto the target. The file is chmod'd 0600 so
    credentials are not world-readable. The temp file is removed on any
    failure. Never prints the secret contents.
    """
    directory = path.dirname(filepath) or "."
    fd, tmp_path = tempfile.mkstemp(
        dir=directory, prefix=".sharedAccount.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(account_info, f)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, filepath)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def get_account_info() -> dict[str, Any]:
    """
    Load or generate account credentials for the current region.

    For jp/en regions: loads from sharedAccount.{region}.yaml file
    or registers new account if file doesn't exist.

    For cn/tw/kr regions: reads credentials from environment variables
    SEKAI_{REGION_UPPER}_ACCESS_TOKEN and SEKAI_{REGION_UPPER}_SDK_OPEN_ID.

    Returns:
        Account info dictionary with userId, credential, signature

    Raises:
        ValueError: If credentials not found for cn/tw/kr regions
    """
    if client_region in ("jp", "en"):
        filepath = path.join(dirname, f"sharedAccount.{client_region}.yaml")
        if path.exists(filepath):
            _enforce_account_yaml_permissions(filepath)
            with open(filepath, encoding="utf-8") as f:
                account_info = yaml.safe_load(f)
            if not isinstance(account_info, dict):
                raise ValueError(f"Invalid account info file: {filepath}")
            return account_info
        else:
            logger.warning("no %s account found, registering a new one", client_region)
            register_info = require_api_client().register_new_account()
            credential = register_info["credential"]
            signature = register_info["userRegistration"]["signature"]
            user_id = jwt.decode(credential, options={"verify_signature": False})[
                "userId"
            ]

            account_info = {
                "signature": signature,
                "credential": credential,
                "userId": user_id,
            }
            _write_account_yaml_atomic(filepath, account_info)
            return account_info

    if client_region in ("cn", "tw", "kr"):
        access_token = getenv(f"SEKAI_{client_region.upper()}_ACCESS_TOKEN", None)
        sdk_open_id = getenv(f"SEKAI_{client_region.upper()}_SDK_OPEN_ID", None)
        if not access_token or not sdk_open_id:
            raise ValueError(
                f"Missing access token and/or SDK open id for {client_region} server"
            )
        return {"loginInfo": {"accessToken": access_token}, "userId": sdk_open_id}

    return {}


def login_account(forced: bool = False) -> dict[str, Any]:
    """
    Authenticate the account with the game server.

    Pauses daily scheduler during login to avoid conflicts,
    resumes afterward.

    Args:
        forced: If True, force relogin even if already logged in

    Returns:
        User profile information from the server
    """
    global user_logged_in, user_info
    if user_logged_in and not forced:
        if user_info is None:
            raise RuntimeError("Logged in client has no user info")
        return user_info

    client = require_api_client()
    previous_state: dict[str, Any] | None = None
    if user_logged_in:
        previous_state = _snapshot_client_state(client)

    day_change_job.pause()
    try:
        client.account_info = get_account_info()
        user_info = client.login()
        user_logged_in = True
        return user_info
    except Exception:
        if previous_state is not None:
            _restore_client_state(client, previous_state)
        raise
    finally:
        day_change_job.resume()


@api.dispatcher.add_method
def init(region: str) -> bool:
    """
    Initialize the JSON-RPC client for a specific region.

    Must be called before any other methods.

    Args:
        region: Game region ('jp', 'en', 'cn', 'tw', 'kr')

    Returns:
        True if initialization successful
    """
    global client_region
    if region:
        client_region = region

    global api_client
    client = APIClient(region=client_region, logger=logger)
    if client_region == "jp":
        client.init_cookie()
    api_client = client

    logger.info("Initialized API client for %s server", client_region)
    return True


@api.dispatcher.add_method
def is_init() -> bool:
    """Check if API client is initialized."""
    return bool(api_client)


@api.dispatcher.add_method
def is_login() -> bool:
    """Check if user is logged in."""
    return user_logged_in


@api.dispatcher.add_method
def login() -> Any:
    """
    Log in the account.

    Returns:
        User profile or JSONRPCInternalError
    """
    return run_job(lambda: login_account())


@api.dispatcher.add_method
def relogin() -> Any:
    """
    Force relogin (refresh session token).

    Returns:
        User profile or JSONRPCInternalError
    """
    return run_job(lambda: login_account(True))


@api.dispatcher.add_method
def check_versions(input_ver_info: dict[str, Any] | None = None) -> Any:
    """
    Check and update game version information.

    Args:
        input_ver_info: Optional version info to compare against

    Returns:
        Version status or JSONRPCInternalError
    """
    client = require_api_client()
    return run_job(lambda: client.check_versions(input_ver_info))


@api.dispatcher.add_method
def version_info() -> dict[str, Any]:
    """Get current game version information."""
    return require_api_client().version_info


@api.dispatcher.add_method
def account_info() -> dict[str, Any]:
    """Get logged-in account identifier.

    Returns only the non-sensitive ``userId`` and ``region`` so callers
    (e.g. event_tracker) can correlate requests without exposing
    credentials/signatures.
    """
    if not user_logged_in:
        raise RuntimeError("Login before calling this method")

    return {
        "userId": require_api_client().account_info.get("userId"),
        "region": client_region,
    }


@api.dispatcher.add_method
def login_user_info() -> dict[str, Any] | None:
    """Get logged-in user profile."""
    if not user_logged_in:
        raise RuntimeError("Login before calling this method")

    return user_info


@api.dispatcher.add_method
def fetch_user_profile(user_id: str) -> Any:
    """
    Fetch user profile by user ID.

    Args:
        user_id: Target user ID

    Returns:
        User profile or JSONRPCInternalError
    """
    if not user_logged_in:
        raise RuntimeError("Login before calling this method")

    client = require_api_client()
    return run_job(lambda: client.fetch_user_profile(user_id))


@api.dispatcher.add_method
def fetch_user_event_ranking(target_user_id: str, event_id: int) -> Any:
    """
    Fetch user's event ranking.

    Args:
        target_user_id: Target user ID
        event_id: Event ID

    Returns:
        Event ranking or JSONRPCInternalError
    """
    if not user_logged_in:
        raise RuntimeError("Login before calling this method")

    client = require_api_client()
    return run_job(lambda: client.fetch_user_event_ranking(target_user_id, event_id))


@api.dispatcher.add_method
def fetch_master_data() -> Any:
    """Fetch game master data."""
    client = require_api_client()
    return run_job(lambda: client.call_pjsk_api("/suite/master"))


@api.dispatcher.add_method
def fetch_system_data() -> Any:
    """Fetch system data (versions, maintenance status, etc)."""
    client = require_api_client()
    return run_job(lambda: client.fetch_system_data())


@api.dispatcher.add_method
def fetch_information() -> Any:
    """Fetch in-game information/notices."""
    client = require_api_client()
    return run_job(lambda: client.fetch_information())


@api.dispatcher.add_method
def fetch_event_rank_first_100(event_id: int) -> Any:
    """
    Fetch top 100 event ranking.

    Args:
        event_id: Event ID

    Returns:
        Top 100 rankings or JSONRPCInternalError
    """
    if not user_logged_in:
        raise RuntimeError("Login before calling this method")

    client = require_api_client()
    return run_job(lambda: client.fetch_event_rank_first_100(event_id))


@api.dispatcher.add_method
def fetch_event_rank_border(event_id: int) -> Any:
    """
    Fetch event ranking borders/thresholds.

    Args:
        event_id: Event ID

    Returns:
        Ranking borders or JSONRPCInternalError
    """
    if not user_logged_in:
        raise RuntimeError("Login before calling this method")

    client = require_api_client()
    return run_job(lambda: client.fetch_event_rank_border(event_id))


@api.dispatcher.add_method
def call_pjsk_api(endpoint: str, method: str = "get", body: str | dict = "") -> Any:
    """
    Make a direct API call to game server.

    Intentionally disabled by default: the generic passthrough forwards
    arbitrary endpoints/methods/body to the game server and must not be
    exposed. Use scoped helpers (e.g. ``fetch_master_split``) instead.
    Enable only with ENABLE_UNSAFE_PJSK_RPC=true.

    Args:
        endpoint: API endpoint path
        method: HTTP method ('get', 'post', 'put', 'patch')
        body: Request body

    Returns:
        API response or JSONRPCInternalError
    """
    if not Config.enable_unsafe_pjsk_rpc():
        raise RuntimeError(
            "Generic call_pjsk_api RPC is disabled. Set "
            "ENABLE_UNSAFE_PJSK_RPC=true to enable it (unsafe)."
        )
    client = require_api_client()
    return run_job(lambda: client.call_pjsk_api(endpoint, method, body))


@api.dispatcher.add_method
def fetch_master_split(split_path: str) -> Any:
    """
    Fetch a single master-data split by path.

    Only allows ``split_path`` values already present in the client's
    ``master_split_paths`` (populated during login), always using GET.
    This is the safe replacement for ``call_pjsk_api("/<split>")``.

    Args:
        split_path: Master split path (must be in master_split_paths)

    Returns:
        Decrypted split data or JSONRPCInternalError

    Raises:
        RuntimeError: If split_path is not in the allowlist
    """
    if not user_logged_in:
        raise RuntimeError("Login before calling this method")

    client = require_api_client()
    allowed = client.master_split_paths
    if split_path not in allowed:
        raise RuntimeError(f"Master split path {split_path!r} is not in the allowlist")

    return run_job(lambda: client.call_pjsk_api(f"/{split_path}"))


@api.dispatcher.add_method
def master_split_paths() -> list[str]:
    """Get master data split paths."""
    if not user_logged_in:
        raise RuntimeError("Login before calling this method")

    return require_api_client().master_split_paths


@api.dispatcher.add_method
def refresh_master_split_paths() -> Any:
    """Refresh master split paths without running the full login workflow."""
    if not user_logged_in:
        raise RuntimeError("Login before calling this method")

    client = require_api_client()

    def refresh() -> list[str]:
        previous_state = _snapshot_client_state(client)
        try:
            return client.refresh_master_split_paths()
        except Exception:
            _restore_client_state(client, previous_state)
            raise

    return run_job(refresh)


@api.dispatcher.add_method
def request_and_decrypt(url: str, method: str = "get", body: str | dict = "") -> Any:
    """
    Make HTTP request and decrypt response.

    Args:
        url: Full URL to request
        method: HTTP method
        body: Request body

    Returns:
        Decrypted response or JSONRPCInternalError
    """
    client = require_api_client()
    return run_job(lambda: client.request_and_decrypt(url, method, body))


app = Flask(__name__)
app.register_blueprint(api.as_blueprint())


def _is_loopback(remote_addr: str | None) -> bool:
    return remote_addr in ("127.0.0.1", "::1", "::ffff:127.0.0.1")


def _require_internal_rpc_auth() -> None:
    """Fail-closed guard for internal JSON-RPC requests.

    - Missing configured token -> 500 (server misconfigured), unless
      loopback + ALLOW_INSECURE_INTERNAL_RPC=true (dev bypass). Once a
      token IS configured, the dev bypass no longer applies: a missing or
      wrong token is always 401, even on loopback.
    - Non-loopback caller -> 401 (the channel is loopback-only by design,
      even with a valid token or the dev bypass).
    - Correct token -> allowed.
    """
    token = Config.get_internal_rpc_token()
    if not token:
        # Dev mode: with no token configured, loopback callers may bypass.
        if Config.allow_insecure_internal_rpc() and _is_loopback(request.remote_addr):
            return
        abort(500, description="INTERNAL_RPC_TOKEN is not configured")

    if not _is_loopback(request.remote_addr):
        abort(401, description="Unauthorized internal RPC request (non-loopback)")

    provided = request.headers.get(INTERNAL_RPC_TOKEN_HEADER, "")
    if compare_digest(provided, token):
        return

    abort(401, description="Unauthorized internal RPC request")


def start_scheduler():
    global scheduler_started
    if scheduler_started:
        return

    with scheduler_start_lock:
        if scheduler_started:
            return
        if not scheduler.running:
            scheduler.start()
        scheduler_started = True


@app.before_request
def enforce_internal_rpc_auth():
    """Authenticate every internal JSON-RPC request before handling.

    Unauthenticated requests must not, among other things, start the
    scheduler. Authorization happens first.
    """
    _require_internal_rpc_auth()


@app.before_request
def ensure_scheduler_started():
    start_scheduler()
