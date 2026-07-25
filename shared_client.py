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
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hmac import compare_digest
from os import getenv, path
from threading import Lock, RLock
from time import monotonic
from typing import Any

import jwt
import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask, abort, request
from jsonrpc.exceptions import JSONRPCDispatchException, JSONRPCInternalError
from pytz import timezone

from api_client import APIClient, AuthTransition, AuthTransitionKind
from config import Config
from logging_config import enable_log_redaction
from utils.jsonrpc_client import INTERNAL_RPC_TOKEN_HEADER
from utils.redaction import redact_structure, redact_text
from utils.task_queue import job_queue, start_worker
from utils.ujsonrpcapi import api

enable_log_redaction()
logger = logging.getLogger(__name__)

dirname = path.dirname(__file__)

# Header used to authenticate internal JSON-RPC calls between the
# sekai-client processes. All such calls must run on loopback only.

# ``shared_client`` is deliberately a single-region process.  The configured
# region is captured once at import time; accepting a different region through
# RPC would make credentials and cached API data ambiguous.
_configured_region = getenv("SEKAI_REGION", "").strip().lower()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _format_utc(value: datetime) -> str:
    return (
        value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


class LifecycleState(StrEnum):
    """Authoritative lifecycle state of this process's shared API client."""

    UNINITIALIZED = "UNINITIALIZED"
    INITIALIZING = "INITIALIZING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    REAUTHENTICATING = "REAUTHENTICATING"
    FAILED = "FAILED"


class _ClientOperation(StrEnum):
    """Classify queued work that is allowed to change lifecycle readiness."""

    NORMAL = "NORMAL"
    INITIALIZATION = "INITIALIZATION"
    AUTHENTICATION = "AUTHENTICATION"
    LIFECYCLE = "LIFECYCLE"


@dataclass
class _LifecycleRuntime:
    """Single owner for client, authentication, and lifecycle observations."""

    region: str
    client: APIClient | None = None
    authenticated: bool = False
    user: dict[str, Any] | None = None
    state: LifecycleState = LifecycleState.UNINITIALIZED
    error: dict[str, str] | None = None
    failure_count: int = 0
    # Monotonic values are intentionally internal and are never serialized.
    last_attempt_mono: float | None = None
    retry_until_mono: float = 0.0
    # RPC-facing values are UTC ISO-8601 strings with a trailing ``Z``.
    last_attempt_at: str | None = None
    next_retry_at: str | None = None
    hidden_auth_failure_pending: bool = False
    active_auth_transaction_id: int | None = None
    auth_generation: int = 0
    lock: RLock = field(default_factory=RLock, repr=False)

    def status(self) -> dict[str, Any]:
        """Return a prompt, secret-free lifecycle snapshot."""
        with self.lock:
            retry_after = max(0.0, self.retry_until_mono - monotonic())
            return {
                "region": self.region,
                "state": self.state.value,
                "initialized": self.client is not None,
                "authenticated": self.authenticated,
                "ready": self.state is LifecycleState.READY
                and self.client is not None
                and self.authenticated,
                "retry_after": retry_after if retry_after else None,
                "last_attempt_at": self.last_attempt_at,
                "next_retry_at": self.next_retry_at,
                "error": deepcopy(self.error),
            }

    def record_failure(self, error: BaseException, state: LifecycleState) -> None:
        with self.lock:
            self.state = state
            self.failure_count += 1
            delay = min(
                Config.LIFECYCLE_RETRY_MAX_SECONDS,
                Config.LIFECYCLE_RETRY_BASE_SECONDS * (2 ** (self.failure_count - 1)),
            )
            # The attempt start remains exposed as ``last_attempt_at``.  The
            # retry deadline, however, starts at the failure instant so a
            # slow authentication attempt cannot consume its own backoff.
            failure_mono = monotonic()
            failure_utc = _utc_now()
            if self.last_attempt_mono is None:
                self.mark_attempt()
            self.retry_until_mono = failure_mono + delay
            next_retry = failure_utc + timedelta(seconds=delay)
            self.next_retry_at = _format_utc(next_retry)
            self.error = {
                "type": type(error).__name__,
                # Do not expose exception text: API errors can include tokens,
                # credentials, or server payloads that are not key-labelled.
                "message": "lifecycle operation failed",
            }

    def clear_failure(self) -> None:
        with self.lock:
            self.failure_count = 0
            self.retry_until_mono = 0.0
            self.next_retry_at = None
            self.error = None

    def mark_attempt(self) -> None:
        with self.lock:
            now = monotonic()
            self.last_attempt_mono = now
            self.last_attempt_at = _format_utc(_utc_now())


_lifecycle = _LifecycleRuntime(region=_configured_region)
# Compatibility snapshots only.  No production path reads these values.
api_client: APIClient | None = None
client_region: str = _configured_region
user_logged_in: bool = False
user_info: dict[str, Any] | None = None
scheduler_start_lock = Lock()
scheduler_started: bool = False


def _validate_configured_region() -> None:
    """Fail closed before worker, scheduler, or liveness services start."""
    if not _configured_region:
        raise RuntimeError(
            "SEKAI_REGION must be set for shared_client; refusing implicit jp default"
        )
    if _configured_region not in Config.REGIONS:
        raise RuntimeError(
            f"Unsupported shared_client SEKAI_REGION: {_configured_region!r}"
        )


_validate_configured_region()
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
    if _is_logged_in():
        try:
            _client_job(lambda: login_account(True), _ClientOperation.AUTHENTICATION)
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
    client = _effective_client()
    if client is None:
        raise RuntimeError("Init before calling this method")
    return client


def _effective_client() -> APIClient | None:
    """Return the committed process client."""
    with _lifecycle.lock:
        return _lifecycle.client


def _is_logged_in() -> bool:
    with _lifecycle.lock:
        return _lifecycle.authenticated


def _effective_user_info() -> dict[str, Any] | None:
    with _lifecycle.lock:
        return _lifecycle.user


def _client_job(
    job: Callable[[], Any],
    operation: _ClientOperation = _ClientOperation.NORMAL,
) -> Any:
    """Run API work behind the lifecycle read/write barrier."""

    def guarded() -> Any:
        with _lifecycle.lock:
            client = _lifecycle.client
            if client is not None:
                previous = (
                    _snapshot_client_state(client),
                    deepcopy(_lifecycle.user),
                    _lifecycle.authenticated,
                )
            else:
                previous = None
            auth_generation = _lifecycle.auth_generation

        try:
            return job()
        except Exception as error:
            with _lifecycle.lock:
                # A queued job may only restore the client it observed.  In
                # particular, never overwrite a candidate committed by a
                # nested initialization or authentication transition.
                if _lifecycle.client is client and previous is not None:
                    auth_changed = _lifecycle.auth_generation != auth_generation
                    if not auth_changed:
                        assert client is not None
                        _restore_client_state(client, previous[0])
                        _lifecycle.user = previous[1]
                        _lifecycle.authenticated = previous[2]
                        hidden_failure = _consume_hidden_auth_failure()
                        if (
                            operation
                            in (
                                _ClientOperation.INITIALIZATION,
                                _ClientOperation.AUTHENTICATION,
                                _ClientOperation.LIFECYCLE,
                            )
                            and _lifecycle.state is not LifecycleState.DEGRADED
                            and not hidden_failure
                        ):
                            _lifecycle.record_failure(error, LifecycleState.DEGRADED)
                        _publish_snapshot_locked()
            raise

    return run_job(guarded)


def _read_job(read: Callable[[], Any]) -> Any:
    """Run a client read behind the same barrier as mutating API work."""
    return _client_job(read)


def _publish_snapshot_locked() -> None:
    """Publish compatibility observations only after a committed transition."""
    global api_client, client_region, user_logged_in, user_info
    api_client = _lifecycle.client
    client_region = _lifecycle.region
    user_logged_in = _lifecycle.authenticated
    user_info = deepcopy(_lifecycle.user)


def _api_lifecycle_transition(event: AuthTransition) -> None:
    """Reconcile APIClient's internal automatic auth transitions."""
    with _lifecycle.lock:
        if event.kind is AuthTransitionKind.ATTEMPT:
            _lifecycle.mark_attempt()
            _lifecycle.active_auth_transaction_id = event.transaction_id
        elif event.kind is AuthTransitionKind.SUCCESS:
            if event.transaction_id != _lifecycle.active_auth_transaction_id:
                return
            _lifecycle.active_auth_transaction_id = None
            client = _lifecycle.client
            if client is not None and client.user_info:
                _lifecycle.auth_generation += 1
                _lifecycle.user = deepcopy(client.user_info)
                _lifecycle.authenticated = True
                _lifecycle.state = LifecycleState.READY
                _lifecycle.clear_failure()
                _lifecycle.hidden_auth_failure_pending = False
                _publish_snapshot_locked()
        elif event.kind is AuthTransitionKind.FAILURE and event.error is not None:
            if event.transaction_id != _lifecycle.active_auth_transaction_id:
                return
            _lifecycle.active_auth_transaction_id = None
            _lifecycle.record_failure(event.error, LifecycleState.DEGRADED)
            _lifecycle.hidden_auth_failure_pending = True
            _publish_snapshot_locked()


def _consume_hidden_auth_failure() -> bool:
    """Consume callback failure ownership exactly once."""
    with _lifecycle.lock:
        if not _lifecycle.hidden_auth_failure_pending:
            return False
        _lifecycle.hidden_auth_failure_pending = False
        return True


def _attach_lifecycle_callback(client: APIClient) -> None:
    client.lifecycle_callback = _api_lifecycle_transition


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
    region = _lifecycle.region
    if region in ("jp", "en"):
        filepath = path.join(dirname, f"sharedAccount.{region}.yaml")
        if path.exists(filepath):
            _enforce_account_yaml_permissions(filepath)
            with open(filepath, encoding="utf-8") as f:
                account_info = yaml.safe_load(f)
            if not isinstance(account_info, dict):
                raise ValueError(f"Invalid account info file: {filepath}")
            return account_info
        else:
            logger.warning("no %s account found, registering a new one", region)
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

    if region in ("cn", "tw", "kr"):
        access_token = getenv(f"SEKAI_{region.upper()}_ACCESS_TOKEN", None)
        sdk_open_id = getenv(f"SEKAI_{region.upper()}_SDK_OPEN_ID", None)
        if not access_token or not sdk_open_id:
            raise ValueError(
                f"Missing access token and/or SDK open id for {region} server"
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
    with _lifecycle.lock:
        _lifecycle.mark_attempt()
    if _is_logged_in() and not forced:
        cached_user = _effective_user_info()
        if cached_user is None:
            raise RuntimeError("Logged in client has no user info")
        return cached_user

    client = require_api_client()
    was_authenticated = _is_logged_in()
    previous_state = _snapshot_client_state(client)
    with _lifecycle.lock:
        previous_auth_generation = _lifecycle.auth_generation

    with _lifecycle.lock:
        _lifecycle.state = (
            LifecycleState.REAUTHENTICATING
            if was_authenticated
            else LifecycleState.INITIALIZING
        )
    day_change_job.pause()
    try:
        client.account_info = get_account_info()
        candidate_user = client.login()
        with _lifecycle.lock:
            _lifecycle.user = deepcopy(candidate_user)
            _lifecycle.authenticated = True
            _lifecycle.state = LifecycleState.READY
            _lifecycle.clear_failure()
            _lifecycle.hidden_auth_failure_pending = False
            _publish_snapshot_locked()
        return candidate_user
    except Exception as error:
        with _lifecycle.lock:
            auth_committed = (
                _lifecycle.auth_generation != previous_auth_generation
                and _lifecycle.authenticated
                and _lifecycle.state is LifecycleState.READY
            )
            if not auth_committed:
                _restore_client_state(client, previous_state)
            # A nested hidden authentication may have committed a newer
            # session before an ordinary outer operation error propagates.
            # That committed state owns the lifecycle outcome and must remain
            # ready; only an uncommitted explicit login/relogin can degrade.
            if not auth_committed and not was_authenticated:
                _lifecycle.authenticated = False
                _lifecycle.user = None
            if not auth_committed and not _consume_hidden_auth_failure():
                _lifecycle.record_failure(error, LifecycleState.DEGRADED)
            _publish_snapshot_locked()
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
    _validate_configured_region()
    requested_region = region or _configured_region
    if requested_region != _configured_region:
        raise ValueError(
            f"Region mismatch: process is configured for {_configured_region!r}, "
            f"not {requested_region!r}"
        )
    if requested_region not in Config.REGIONS:
        raise ValueError(f"Unsupported formal shared-client region: {requested_region}")
    result = _client_job(_initialize_client, _ClientOperation.INITIALIZATION)
    return bool(result)


def _initialize_client() -> bool:
    """Build and validate a candidate before committing it to the runtime."""
    with _lifecycle.lock:
        if _lifecycle.client is not None:
            # Same-region init is intentionally a true no-op.
            return True
        if _lifecycle.retry_until_mono > monotonic():
            raise RuntimeError("shared client lifecycle retry backoff is active")
        _lifecycle.state = LifecycleState.INITIALIZING
        _lifecycle.mark_attempt()

    try:
        candidate = APIClient(region=_lifecycle.region, logger=logger)
        _attach_lifecycle_callback(candidate)
        if _lifecycle.region == "jp":
            candidate.init_cookie()
    except Exception as error:
        with _lifecycle.lock:
            _lifecycle.record_failure(error, LifecycleState.FAILED)
            _publish_snapshot_locked()
        raise

    with _lifecycle.lock:
        _lifecycle.client = candidate
        # Every successful init starts a fresh auth lifecycle.  Do not carry
        # authentication flags or profile data across a same-region reinit.
        _lifecycle.authenticated = False
        _lifecycle.user = None
        _lifecycle.state = LifecycleState.DEGRADED
        _lifecycle.clear_failure()
        _publish_snapshot_locked()
    logger.info("Initialized API client for %s server", _lifecycle.region)
    return True


@api.dispatcher.add_method
def is_init() -> bool:
    """Check if API client is initialized."""
    with _lifecycle.lock:
        return _lifecycle.client is not None


@api.dispatcher.add_method
def is_login() -> bool:
    """Check if user is logged in."""
    return _is_logged_in()


@api.dispatcher.add_method
def lifecycle_status() -> dict[str, Any]:
    """Return structured, secret-free lifecycle information."""
    return _lifecycle.status()


@api.dispatcher.add_method
def readiness() -> dict[str, Any]:
    """Return the readiness snapshot without performing I/O."""
    return _lifecycle.status()


@api.dispatcher.add_method
def liveness() -> dict[str, Any]:
    """Cheap process liveness probe; it never touches the API client."""
    _validate_configured_region()
    return {"ok": True, "region": _lifecycle.region}


@api.dispatcher.add_method
def ensure_ready() -> dict[str, Any]:
    """Make one serialized initialization-plus-login attempt."""

    def attempt() -> dict[str, Any]:
        status = _lifecycle.status()
        if status["ready"]:
            return status
        if status["retry_after"] is not None:
            return {**status, "attempted": False, "reason": "retry backoff active"}
        _lifecycle.mark_attempt()
        try:
            _initialize_client()
            login_account(True)
        except Exception:
            return {**_lifecycle.status(), "attempted": True}
        return {**_lifecycle.status(), "attempted": True}

    return dict(_client_job(attempt, _ClientOperation.LIFECYCLE))


@api.dispatcher.add_method
def login() -> Any:
    """
    Log in the account.

    Returns:
        User profile or JSONRPCInternalError
    """
    return _client_job(lambda: login_account(), _ClientOperation.AUTHENTICATION)


@api.dispatcher.add_method
def relogin() -> Any:
    """
    Force relogin (refresh session token).

    Returns:
        User profile or JSONRPCInternalError
    """
    return _client_job(lambda: login_account(True), _ClientOperation.AUTHENTICATION)


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
    return _client_job(lambda: client.check_versions(input_ver_info))


@api.dispatcher.add_method
def version_info() -> dict[str, Any]:
    """Get current game version information."""
    return dict(_read_job(lambda: deepcopy(require_api_client().version_info)))


@api.dispatcher.add_method
def account_info() -> dict[str, Any]:
    """Get logged-in account identifier.

    Returns only the non-sensitive ``userId`` and ``region`` so callers
    (e.g. event_tracker) can correlate requests without exposing
    credentials/signatures.
    """
    if not _is_logged_in():
        raise RuntimeError("Login before calling this method")

    return dict(
        _read_job(
            lambda: {
                "userId": require_api_client().account_info.get("userId"),
                "region": _lifecycle.region,
            }
        )
    )


@api.dispatcher.add_method
def login_user_info() -> dict[str, Any] | None:
    """Get logged-in user profile."""
    if not _is_logged_in():
        raise RuntimeError("Login before calling this method")

    return deepcopy(_effective_user_info())


@api.dispatcher.add_method
def fetch_user_profile(user_id: str) -> Any:
    """
    Fetch user profile by user ID.

    Args:
        user_id: Target user ID

    Returns:
        User profile or JSONRPCInternalError
    """
    if not _is_logged_in():
        raise RuntimeError("Login before calling this method")

    client = require_api_client()
    return _client_job(lambda: client.fetch_user_profile(user_id))


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
    if not _is_logged_in():
        raise RuntimeError("Login before calling this method")

    client = require_api_client()
    return _client_job(
        lambda: client.fetch_user_event_ranking(target_user_id, event_id)
    )


@api.dispatcher.add_method
def fetch_master_data() -> Any:
    """Fetch game master data."""
    client = require_api_client()
    return _client_job(lambda: client.call_pjsk_api("/suite/master"))


@api.dispatcher.add_method
def fetch_system_data() -> Any:
    """Fetch system data (versions, maintenance status, etc)."""
    client = require_api_client()
    return _client_job(lambda: client.fetch_system_data())


@api.dispatcher.add_method
def fetch_information() -> Any:
    """Fetch in-game information/notices."""
    client = require_api_client()
    return _client_job(lambda: client.fetch_information())


@api.dispatcher.add_method
def fetch_event_rank_first_100(event_id: int) -> Any:
    """
    Fetch top 100 event ranking.

    Args:
        event_id: Event ID

    Returns:
        Top 100 rankings or JSONRPCInternalError
    """
    if not _is_logged_in():
        raise RuntimeError("Login before calling this method")

    client = require_api_client()
    return _client_job(lambda: client.fetch_event_rank_first_100(event_id))


@api.dispatcher.add_method
def fetch_event_rank_border(event_id: int) -> Any:
    """
    Fetch event ranking borders/thresholds.

    Args:
        event_id: Event ID

    Returns:
        Ranking borders or JSONRPCInternalError
    """
    if not _is_logged_in():
        raise RuntimeError("Login before calling this method")

    client = require_api_client()
    return _client_job(lambda: client.fetch_event_rank_border(event_id))


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
    return _client_job(lambda: client.call_pjsk_api(endpoint, method, body))


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
    if not _is_logged_in():
        raise RuntimeError("Login before calling this method")

    def fetch() -> Any:
        client = require_api_client()
        if split_path not in client.master_split_paths:
            raise RuntimeError(
                f"Master split path {split_path!r} is not in the allowlist"
            )
        return client.call_pjsk_api(f"/{split_path}")

    return _client_job(fetch)


@api.dispatcher.add_method
def master_split_paths() -> list[str]:
    """Get master data split paths."""
    if not _is_logged_in():
        raise RuntimeError("Login before calling this method")

    return list(_read_job(lambda: deepcopy(require_api_client().master_split_paths)))


@api.dispatcher.add_method
def refresh_master_split_paths() -> Any:
    """Refresh master split paths without running the full login workflow."""
    if not _is_logged_in():
        raise RuntimeError("Login before calling this method")

    client = require_api_client()

    def refresh() -> list[str]:
        previous_state = _snapshot_client_state(client)
        try:
            return client.refresh_master_split_paths()
        except Exception:
            _restore_client_state(client, previous_state)
            raise

    return _client_job(refresh)


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
    return _client_job(lambda: client.request_and_decrypt(url, method, body))


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
