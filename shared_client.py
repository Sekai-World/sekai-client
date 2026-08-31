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

import atexit
import logging
import queue
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hmac import compare_digest
from os import getenv, path
from threading import Lock, RLock
from time import monotonic
from typing import Any
from uuid import uuid4

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask, abort, has_request_context, request
from jsonrpc.exceptions import JSONRPCDispatchException, JSONRPCInternalError
from pytz import timezone

from accounts import (
    AccountLease,
    AccountProvider,
    AccountProviderError,
    AccountRegion,
    AccountRegistrationAdapter,
    InvalidAccountReason,
    InvalidLeaseError,
    LocalAccountProvider,
    RemoteAccountProvider,
    credential_to_account_info,
)
from accounts.lease_journal import LeaseJournal, LeaseOperation
from api_client import APIClient, AuthTransition, AuthTransitionKind
from config import Config
from logging_config import enable_log_redaction
from utils.deadline import (
    Deadline,
    DeadlineExceeded,
    current_deadline,
    reset_current_deadline,
    set_current_deadline,
)
from utils.jsonrpc_client import INTERNAL_RPC_TIMEOUT_HEADER, INTERNAL_RPC_TOKEN_HEADER
from utils.redaction import redact_structure, redact_text
from utils.task_queue import (
    QueuedJob,
    job_queue,
    metrics_snapshot,
    record_accepted,
    record_rejected,
    record_timed_out,
    start_worker,
)
from utils.ujsonrpcapi import api

enable_log_redaction()
logger = logging.getLogger(__name__)

dirname = path.dirname(__file__)
_account_provider: AccountProvider | None = None
_active_account_lease: AccountLease | None = None
_active_lease_operation: LeaseOperation | None = None
_account_lease_lock = Lock()
_LEASE_RENEW_AHEAD = timedelta(hours=1)


def configure_account_provider(provider: AccountProvider | None) -> None:
    """Inject an account provider; `None` restores the local default."""
    global _account_provider, _active_account_lease, _active_lease_operation
    with _account_lease_lock:
        _account_provider = provider
        _active_account_lease = None
        _active_lease_operation = None


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
    deadline = current_deadline()
    timeout = Config.JOB_QUEUE_TIMEOUT
    if deadline is None:
        deadline = Deadline.after(Config.ANSWER_QUEUE_TIMEOUT)
    else:
        try:
            timeout = min(timeout, deadline.require_remaining())
        except DeadlineExceeded:
            record_timed_out()
            return None, JSONRPCInternalError(data="Request deadline exceeded")
    try:
        job_queue.put(
            QueuedJob(job, response_queue, deadline, monotonic()), timeout=timeout
        )
    except queue.Full:
        record_rejected()
        return None, JSONRPCInternalError(
            data={
                "code": "queue_full",
                "retryable": True,
                "retry_after": 1,
            }
        )
    record_accepted()
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
        deadline = current_deadline()
        timeout = Config.ANSWER_QUEUE_TIMEOUT
        if deadline is not None:
            timeout = min(timeout, deadline.require_remaining())
        res: Any = response_queue.get(timeout=timeout)
    except queue.Empty:
        record_timed_out()
        return JSONRPCInternalError(data="Request deadline exceeded")
    except DeadlineExceeded:
        record_timed_out()
        return JSONRPCInternalError(data="Request deadline exceeded")

    if isinstance(res, RuntimeError):
        err_data = str(res)
        if len(res.args) > 1:
            err_data = str(res.args[1])
        return JSONRPCInternalError(data=err_data)
    elif isinstance(res, Exception):
        return JSONRPCInternalError(data=str(res))
    return res


def _new_request_deadline() -> Deadline:
    budget = Config.ANSWER_QUEUE_TIMEOUT
    if not has_request_context():
        return Deadline.after(budget)

    raw_budget = request.headers.get(INTERNAL_RPC_TIMEOUT_HEADER)
    if raw_budget is None:
        return Deadline.after(budget)
    try:
        parsed_budget = int(raw_budget) / 1000
    except ValueError:
        parsed_budget = 0
    if parsed_budget <= 0:
        raise JSONRPCDispatchException(
            code=JSONRPCInternalError.CODE,
            message=JSONRPCInternalError.MESSAGE,
            data="Invalid request deadline",
        )
    return Deadline.after(min(budget, parsed_budget))


def _await_queued_job(job: Callable[[], Any], deadline: Deadline) -> Any:
    def deadline_guarded_job() -> Any:
        token = set_current_deadline(deadline)
        try:
            deadline.require_remaining()
            return job()
        finally:
            reset_current_deadline(token)

    token = set_current_deadline(deadline)
    try:
        response_queue, err = enqueue_job(deadline_guarded_job)
        if err is not None:
            return err
        if response_queue is None:
            return JSONRPCInternalError(data="Job was not enqueued")
        return get_answer(response_queue)
    finally:
        reset_current_deadline(token)


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
    result = _await_queued_job(job, _new_request_deadline())

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
            previous = _snapshot_runtime_locked()
            auth_generation = _lifecycle.auth_generation

        try:
            result = job()
            deadline = current_deadline()
            if deadline is not None:
                deadline.require_remaining()
            return result
        except Exception as error:
            with _lifecycle.lock:
                abandoned = isinstance(error, DeadlineExceeded)
                # A queued job may only restore the client it observed.  In
                # particular, never overwrite a candidate committed by a
                # nested initialization or authentication transition.
                if abandoned:
                    _restore_runtime_locked(previous)
                elif _lifecycle.client is client and client is not None:
                    auth_changed = _lifecycle.auth_generation != auth_generation
                    if not auth_changed:
                        client_state = previous["client_state"]
                        assert isinstance(client_state, dict)
                        _restore_client_state(client, client_state)
                        _lifecycle.user = deepcopy(previous["user"])
                        _lifecycle.authenticated = bool(previous["authenticated"])
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
        deadline = current_deadline()
        if deadline is not None and deadline.remaining() <= 0:
            return
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


def _snapshot_runtime_locked() -> dict[str, Any]:
    """Capture all state a queued task may mutate before its final commit."""
    client = _lifecycle.client
    return {
        "client": client,
        "client_state": _snapshot_client_state(client) if client is not None else None,
        "authenticated": _lifecycle.authenticated,
        "user": deepcopy(_lifecycle.user),
        "state": _lifecycle.state,
        "error": deepcopy(_lifecycle.error),
        "failure_count": _lifecycle.failure_count,
        "last_attempt_mono": _lifecycle.last_attempt_mono,
        "retry_until_mono": _lifecycle.retry_until_mono,
        "last_attempt_at": _lifecycle.last_attempt_at,
        "next_retry_at": _lifecycle.next_retry_at,
        "hidden_auth_failure_pending": _lifecycle.hidden_auth_failure_pending,
        "active_auth_transaction_id": _lifecycle.active_auth_transaction_id,
        "auth_generation": _lifecycle.auth_generation,
    }


def _restore_runtime_locked(state: dict[str, Any]) -> None:
    """Restore a task snapshot after its caller's deadline has elapsed."""
    client = state["client"]
    _lifecycle.client = client
    client_state = state["client_state"]
    if client is not None and isinstance(client_state, dict):
        _restore_client_state(client, client_state)
    _lifecycle.authenticated = bool(state["authenticated"])
    _lifecycle.user = deepcopy(state["user"])
    _lifecycle.state = state["state"]
    _lifecycle.error = deepcopy(state["error"])
    _lifecycle.failure_count = int(state["failure_count"])
    _lifecycle.last_attempt_mono = state["last_attempt_mono"]
    _lifecycle.retry_until_mono = float(state["retry_until_mono"])
    _lifecycle.last_attempt_at = state["last_attempt_at"]
    _lifecycle.next_retry_at = state["next_retry_at"]
    _lifecycle.hidden_auth_failure_pending = bool(state["hidden_auth_failure_pending"])
    _lifecycle.active_auth_transaction_id = state["active_auth_transaction_id"]
    _lifecycle.auth_generation = int(state["auth_generation"])
    _publish_snapshot_locked()


def _restore_client_state(client: APIClient, state: dict[str, Any]) -> None:
    client.headers = state["headers"]
    client.account_info = state["account_info"]
    client.version_info = state["version_info"]
    client.master_split_paths = state["master_split_paths"]
    client.user_info = state["user_info"]


def get_account_info() -> dict[str, Any]:  # noqa: C901 - lease lifecycle branches
    """Acquire a lease and adapt it to the current game client payload."""
    global _account_provider, _active_account_lease, _active_lease_operation

    region = AccountRegion(_lifecycle.region)
    with _account_lease_lock:
        if (
            _active_account_lease is not None
            and _active_account_lease.region is region
            and not _active_account_lease.is_expired()
        ):
            operation = _active_lease_operation
            if (
                _account_provider is not None
                and datetime.now(UTC)
                >= _active_account_lease.expires_at - _LEASE_RENEW_AHEAD
                and operation is not None
                and operation.expires_at is not None
                and getattr(_account_provider, "requires_durable_idempotency", False)
                is True
            ):
                renew_key = (
                    f"renew-{operation.idempotency_key}-"
                    f"{operation.expires_at.astimezone(UTC).isoformat()}"
                )
                try:
                    provider = _account_provider
                    assert provider is not None
                    new_expires_at = provider.renew(
                        _active_account_lease.lease_id,
                        extend_seconds=24 * 60 * 60,
                        idempotency_key=renew_key,
                    )
                    journal = _remote_lease_journal(provider)
                    assert journal is not None
                    operation = journal.mark_renewed(operation, new_expires_at)
                    _active_lease_operation = operation
                    _active_account_lease = replace(
                        _active_account_lease, expires_at=new_expires_at
                    )
                    return credential_to_account_info(_active_account_lease.credential)
                except InvalidLeaseError:
                    logger.warning("Account lease renewal failed; reacquiring")
                except AccountProviderError as error:
                    logger.warning("Account lease renewal failed")
                    if error.retryable:
                        return credential_to_account_info(
                            _active_account_lease.credential
                        )
            else:
                return credential_to_account_info(_active_account_lease.credential)
        if _account_provider is None:
            _account_provider = _build_account_provider()
        consumer = f"shared-client-{region.value}"
        journal = _remote_lease_journal(_account_provider)
        operation = None
        if journal is not None:
            operation = journal.load_or_create(region.value, consumer)
            if operation.release_pending:
                try:
                    _account_provider.release(operation.lease_id or "")
                except InvalidLeaseError:
                    pass
                journal.clear(operation)
                operation = journal.load_or_create(region.value, consumer)
        lease = _account_provider.acquire(
            region,
            consumer,
            ttl_seconds=24 * 60 * 60,
            idempotency_key=(
                operation.idempotency_key if operation else f"login-{uuid4()}"
            ),
        )
        if journal is not None and operation is not None:
            operation = journal.mark_acquired(
                operation, lease.lease_id, lease.expires_at
            )
        _active_account_lease = lease
        _active_lease_operation = operation
        return credential_to_account_info(lease.credential)


def _remote_lease_journal(provider: AccountProvider) -> LeaseJournal | None:
    if getattr(provider, "requires_durable_idempotency", False) is not True:
        return None
    directory = getenv(
        "SEKAI_ACCOUNT_LEASE_STATE_DIR",
        path.join(dirname, ".runtime", "account-leases"),
    )
    return LeaseJournal(directory)


def _release_account_lease(
    provider: AccountProvider,
    lease: AccountLease,
    operation: LeaseOperation | None,
) -> None:
    journal = _remote_lease_journal(provider)
    if journal is not None and operation is not None:
        current = journal.load(operation.region, operation.consumer)
        if current is not None and current.idempotency_key == operation.idempotency_key:
            operation = journal.mark_release_pending(operation)
        else:
            operation = None
    provider.release(lease.lease_id)
    if journal is not None and operation is not None:
        journal.clear(operation)


def _best_effort_release(
    provider: AccountProvider,
    lease: AccountLease,
    operation: LeaseOperation | None,
) -> None:
    try:
        _release_account_lease(provider, lease, operation)
    except Exception:
        logger.warning("Failed to release account lease")


def _best_effort_report_authentication_failure(
    provider: AccountProvider,
    lease: AccountLease,
    operation: LeaseOperation | None,
) -> bool:
    try:
        provider.report_invalid(
            lease.lease_id, InvalidAccountReason.AUTHENTICATION_FAILED
        )
    except Exception:
        logger.warning("Failed to report invalid account lease")
        return False
    journal = _remote_lease_journal(provider)
    if journal is not None and operation is not None:
        try:
            journal.clear(operation)
        except Exception:
            logger.warning("Failed to clear invalid account lease journal")
    return True


def _is_explicit_authentication_rejection(error: Exception) -> bool:
    message = str(error).lower()
    return "http 401" in message or "http 403" in message


def release_active_account_lease() -> None:
    """Best-effort idempotent release for graceful worker shutdown."""
    global _active_account_lease, _active_lease_operation
    with _account_lease_lock:
        lease = _active_account_lease
        operation = _active_lease_operation
        provider = _account_provider
        _active_account_lease = None
        _active_lease_operation = None
    if lease is None or provider is None:
        return
    _best_effort_release(provider, lease, operation)


atexit.register(release_active_account_lease)


def _build_account_provider() -> AccountProvider:
    provider_kind = getenv("SEKAI_ACCOUNT_PROVIDER", "local").strip().lower()
    if provider_kind == "local":
        return LocalAccountProvider(
            dirname,
            register_account=lambda target_region: AccountRegistrationAdapter(
                require_api_client()
            ).register(target_region),
        )
    if provider_kind == "remote":
        try:
            timeout = float(getenv("SEKAI_ACCOUNT_SERVICE_TIMEOUT", "10"))
            max_attempts = int(getenv("SEKAI_ACCOUNT_SERVICE_MAX_ATTEMPTS", "3"))
        except ValueError:
            raise RuntimeError(
                "Invalid remote account provider configuration"
            ) from None
        try:
            return RemoteAccountProvider(
                getenv("SEKAI_ACCOUNT_SERVICE_URL", "").strip(),
                getenv("SEKAI_ACCOUNT_SERVICE_TOKEN", "").strip(),
                timeout=timeout,
                max_attempts=max_attempts,
            )
        except ValueError:
            raise RuntimeError(
                "Invalid remote account provider configuration"
            ) from None
    raise RuntimeError("Unsupported account provider configuration")


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

    global _active_account_lease, _active_lease_operation

    client = require_api_client()
    was_authenticated = _is_logged_in()
    previous_state = _snapshot_client_state(client)
    previous_account_lease = _active_account_lease
    previous_lease_operation = _active_lease_operation
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
        if (
            previous_account_lease is not None
            and previous_account_lease is not _active_account_lease
            and _account_provider is not None
        ):
            _best_effort_release(
                _account_provider,
                previous_account_lease,
                previous_lease_operation,
            )
        with _lifecycle.lock:
            _lifecycle.user = deepcopy(candidate_user)
            _lifecycle.authenticated = True
            _lifecycle.state = LifecycleState.READY
            _lifecycle.clear_failure()
            _lifecycle.hidden_auth_failure_pending = False
            _publish_snapshot_locked()
        return candidate_user
    except Exception as error:
        failed_lease = _active_account_lease
        failed_lease_operation = _active_lease_operation
        invalid_reported = False
        if (
            failed_lease is not None
            and failed_lease.region in (AccountRegion.TW, AccountRegion.KR)
            and _account_provider is not None
            and _is_explicit_authentication_rejection(error)
        ):
            invalid_reported = _best_effort_report_authentication_failure(
                _account_provider, failed_lease, failed_lease_operation
            )
        if (
            not invalid_reported
            and failed_lease is not None
            and failed_lease is not previous_account_lease
            and _account_provider is not None
        ):
            _best_effort_release(
                _account_provider,
                failed_lease,
                failed_lease_operation,
            )
        _active_account_lease = (
            None
            if invalid_reported and failed_lease is previous_account_lease
            else previous_account_lease
        )
        _active_lease_operation = (
            None
            if invalid_reported and failed_lease is previous_account_lease
            else previous_lease_operation
        )
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
    return {**_lifecycle.status(), "queue": metrics_snapshot()}


@api.dispatcher.add_method
def readiness() -> dict[str, Any]:
    """Return the readiness snapshot without performing I/O."""
    return {**_lifecycle.status(), "queue": metrics_snapshot()}


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
def fetch_event_rank_snapshot(event_id: int) -> dict[str, Any]:
    """Fetch top rankings and borders in one serialized client job."""
    if not _is_logged_in():
        raise RuntimeError("Login before calling this method")

    client = require_api_client()

    def fetch() -> dict[str, Any]:
        first100 = client.fetch_event_rank_first_100(event_id)
        if first100.get("isEventAggregate"):
            return {"first100": first100, "border": None}
        return {
            "first100": first100,
            "border": client.fetch_event_rank_border(event_id),
        }

    return dict(_client_job(fetch))


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
