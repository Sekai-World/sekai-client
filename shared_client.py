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
import queue
from collections.abc import Callable
from os import getenv, path
from threading import Lock
from typing import Any

import jwt
import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask
from jsonrpc.exceptions import JSONRPCInternalError
from pytz import timezone

from api_client import APIClient
from config import Config
from utils.constants import pjsk_region
from utils.task_queue import job_queue, start_worker
from utils.ujsonrpcapi import api

dirname = path.dirname(__file__)
logger = logging.getLogger(__name__)

start_worker()

# Global state for the JSON-RPC server
api_client: APIClient | None = None
client_region: str = pjsk_region
user_logged_in: bool = False
user_info: dict[str, Any] | None = None
scheduler_start_lock = Lock()
scheduler_started: bool = False


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
    start_worker()
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


def run_job(job: Callable[[], Any]) -> Any | JSONRPCInternalError:
    """
    Enqueue a job and wait for its result.

    Orchestrates enqueue_job + get_answer with proper error handling.

    Args:
        job: Callable to execute in background worker

    Returns:
        Job result, or JSONRPCInternalError if job failed or timed out
    """
    response_queue, err = enqueue_job(job)
    if err:
        return err
    return get_answer(response_queue)


def day_change_func() -> None:
    """Scheduled job to relogin once per day (at 4 AM JST)."""
    if user_logged_in:
        login_account(True)


# Background scheduler for daily account refresh
scheduler = BackgroundScheduler(timezone=timezone("Asia/Tokyo"))
cron_trigger = CronTrigger(
    hour="4", minute="0", second="0", timezone=timezone("Asia/Tokyo")
)
day_change_job = scheduler.add_job(day_change_func, cron_trigger, name="day_change_job")


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
            with open(filepath, encoding="utf-8") as f:
                return yaml.safe_load(f)
        else:
            logger.warning("no %s account found, registering a new one", client_region)
            register_info = api_client.register_new_account()
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
            with open(filepath, "w", encoding="utf-8") as f:
                yaml.dump(account_info, f)
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
    global user_logged_in
    if (not user_logged_in) or forced:
        day_change_job.pause()

        global api_client
        api_client.account_info = get_account_info()
        global user_info
        user_info = api_client.login()
        user_logged_in = True

        day_change_job.resume()

        return user_info
    elif user_logged_in:
        return user_info

    return {}


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
    api_client = APIClient(region=client_region, logger=logger)
    if client_region == "jp":
        api_client.init_cookie()

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
    if not api_client:
        raise RuntimeError("Init before calling this method")

    return run_job(lambda: api_client.check_versions(input_ver_info))


@api.dispatcher.add_method
def version_info() -> dict[str, Any]:
    """Get current game version information."""
    if not api_client:
        raise RuntimeError("Init before calling this method")

    return api_client.version_info


@api.dispatcher.add_method
def account_info() -> dict[str, Any]:
    """Get logged-in account information."""
    if not user_logged_in:
        raise RuntimeError("Login before calling this method")

    return api_client.account_info


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

    return run_job(lambda: api_client.fetch_user_profile(user_id))


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

    return run_job(
        lambda: api_client.fetch_user_event_ranking(target_user_id, event_id)
    )


@api.dispatcher.add_method
def fetch_master_data() -> Any:
    """Fetch game master data."""
    if not api_client:
        raise RuntimeError("Init before calling this method")

    return run_job(lambda: api_client.call_pjsk_api("/suite/master"))


@api.dispatcher.add_method
def fetch_system_data() -> Any:
    """Fetch system data (versions, maintenance status, etc)."""
    if not api_client:
        raise RuntimeError("Init before calling this method")

    return run_job(lambda: api_client.fetch_system_data())


@api.dispatcher.add_method
def fetch_information() -> Any:
    """Fetch in-game information/notices."""
    if not api_client:
        raise RuntimeError("Init before calling this method")

    return run_job(lambda: api_client.fetch_information())


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

    return run_job(lambda: api_client.fetch_event_rank_first_100(event_id))


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

    return run_job(lambda: api_client.fetch_event_rank_border(event_id))


@api.dispatcher.add_method
def call_pjsk_api(endpoint: str, method: str = "get", body: str | dict = "") -> Any:
    """
    Make a direct API call to game server.

    Args:
        endpoint: API endpoint path
        method: HTTP method ('get', 'post', 'put', 'patch')
        body: Request body

    Returns:
        API response or JSONRPCInternalError
    """
    if not api_client:
        raise RuntimeError("Init before calling this method")

    return run_job(lambda: api_client.call_pjsk_api(endpoint, method, body))


@api.dispatcher.add_method
def master_split_paths() -> list[str]:
    """Get master data split paths."""
    if not user_logged_in:
        raise RuntimeError("Login before calling this method")

    return api_client.master_split_paths


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
    if not api_client:
        raise RuntimeError("Init before calling this method")
    return run_job(lambda: api_client.request_and_decrypt(url, method, body))


app = Flask(__name__)
app.register_blueprint(api.as_blueprint())


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
def ensure_scheduler_started():
    start_scheduler()
