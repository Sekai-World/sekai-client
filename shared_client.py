import yaml
import jwt
import queue

from pytz import timezone
from os import path, getenv
from threading import Lock
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask
from jsonrpc.exceptions import JSONRPCInternalError

from utils.constants import pjsk_region
from utils.crypto import decrypt_msgpack
from utils.task_queue import job_queue, answer_queue
from utils.ujsonrpcapi import api
from api_client import APIClient

dirname = path.dirname(__file__)
api_client: APIClient = None
client_region = pjsk_region
user_logged_in = False
user_info = None
scheduler_start_lock = Lock()
scheduler_started = False
job_queue_timeout = float(getenv('JOB_QUEUE_TIMEOUT', '30'))
answer_queue_timeout = float(getenv('ANSWER_QUEUE_TIMEOUT', '180'))


def enqueue_job(job):
    try:
        job_queue.put(job, timeout=job_queue_timeout)
    except queue.Full as err:
        raise RuntimeError('Job queue is full, please retry later') from err


def get_answer():
    try:
        res = answer_queue.get(timeout=answer_queue_timeout)
    except queue.Empty:
        return JSONRPCInternalError(data='Timed out waiting for worker response')

    if isinstance(res, RuntimeError):
        answer_queue.task_done()
        err_data = str(res)
        if len(res.args) > 1:
            err_data = str(res.args[1])
        return JSONRPCInternalError(data=err_data)
    elif isinstance(res, Exception):
        answer_queue.task_done()
        return JSONRPCInternalError(data=str(res))
    else:
        answer_queue.task_done()
        return res


def day_change_func():
    if user_logged_in:
        login_account(True)


scheduler = BackgroundScheduler(timezone=timezone('Asia/Tokyo'))
cron_trigger = CronTrigger(hour='4',
                           minute='0',
                           second='0',
                           timezone=timezone('Asia/Tokyo'))
day_change_job = scheduler.add_job(day_change_func,
                                   cron_trigger,
                                   name="day_change_job")


def get_account_info():
    if client_region in ("jp", "en"):
        filepath = path.join(dirname, f'sharedAccount.{client_region}.yaml')
        if path.exists(filepath):
            with open(filepath, 'r') as f:
                return yaml.safe_load(f)
        else:
            app.logger.warning(
                f'no {client_region} account found, registering a new one')
            register_info = api_client.register_new_account()
            credential = register_info["credential"]
            signature = register_info["userRegistration"]["signature"]
            user_id = jwt.decode(credential,
                                 options={"verify_signature": False})["userId"]

            account_info = {
                "signature": signature,
                "credential": credential,
                "userId": user_id
            }
            with open(filepath, 'w') as f:
                yaml.dump(account_info, f)
            return account_info

    if client_region in ("cn", "tw", "kr"):
        access_token = getenv(f"SEKAI_{client_region.upper()}_ACCESS_TOKEN",
                              None)
        sdk_open_id = getenv(f"SEKAI_{client_region.upper()}_SDK_OPEN_ID",
                             None)
        if not access_token or not sdk_open_id:
            raise ValueError(
                f"Missing access token and/or SDK open id for {client_region} server"
            )
        return {
            "loginInfo": {
                "accessToken": access_token
            },
            "userId": sdk_open_id
        }


def login_account(forced=False):
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
def init(region):
    global client_region
    if region:
        client_region = region

    global api_client
    api_client = APIClient(region=client_region, logger=app.logger)
    if client_region in ("jp"):
        api_client.init_cookie()

    app.logger.info(f"Initialized API client for {client_region} server")
    return True


@api.dispatcher.add_method
def is_init():
    return not not api_client


@api.dispatcher.add_method
def is_login():
    return user_logged_in


@api.dispatcher.add_method
def login():
    enqueue_job(lambda: login_account())
    return get_answer()


@api.dispatcher.add_method
def relogin():
    enqueue_job(lambda: login_account(True))
    return get_answer()


@api.dispatcher.add_method
def check_versions(input_ver_info=None):
    if not api_client:
        raise RuntimeError("Init before calling this method")

    enqueue_job(lambda: api_client.check_versions(input_ver_info))
    return get_answer()


@api.dispatcher.add_method
def version_info():
    if not api_client:
        raise RuntimeError("Init before calling this method")

    return api_client.version_info


@api.dispatcher.add_method
def account_info():
    if not user_logged_in:
        raise RuntimeError("Login before calling this method")

    return api_client.account_info


@api.dispatcher.add_method
def login_user_info():
    if not user_logged_in:
        raise RuntimeError("Login before calling this method")

    return user_info


@api.dispatcher.add_method
def fetch_user_profile(user_id):
    if not user_logged_in:
        raise RuntimeError("Login before calling this method")

    enqueue_job(lambda: api_client.fetch_user_profile(user_id))
    return get_answer()


@api.dispatcher.add_method
def fetch_user_event_ranking(target_user_id, event_id):
    if not user_logged_in:
        raise RuntimeError("Login before calling this method")

    enqueue_job(
        lambda: api_client.fetch_user_event_ranking(target_user_id, event_id))
    return get_answer()


@api.dispatcher.add_method
def fetch_master_data():
    if not api_client:
        raise RuntimeError("Init before calling this method")

    enqueue_job(lambda: api_client.call_pjsk_api("/suite/master"))
    return get_answer()


@api.dispatcher.add_method
def fetch_system_data():
    if not api_client:
        raise RuntimeError("Init before calling this method")

    enqueue_job(lambda: api_client.fetch_system_data())
    return get_answer()


@api.dispatcher.add_method
def fetch_information():
    if not api_client:
        raise RuntimeError("Init before calling this method")

    enqueue_job(lambda: api_client.fetch_information())
    return get_answer()


@api.dispatcher.add_method
def fetch_event_rank_first_100(event_id):
    if not user_logged_in:
        raise RuntimeError("Login before calling this method")

    enqueue_job(lambda: api_client.fetch_event_rank_first_100(event_id))
    return get_answer()


@api.dispatcher.add_method
def fetch_event_rank_border(event_id):
    if not user_logged_in:
        raise RuntimeError("Login before calling this method")

    enqueue_job(lambda: api_client.fetch_event_rank_border(event_id))
    return get_answer()


@api.dispatcher.add_method
def call_pjsk_api(endpoint: str, method="get", body: str | dict = ""):
    if not api_client:
        raise RuntimeError("Init before calling this method")

    enqueue_job(lambda: api_client.call_pjsk_api(endpoint, method, body))
    return get_answer()


@api.dispatcher.add_method
def master_split_paths():
    if not user_logged_in:
        raise RuntimeError("Login before calling this method")

    return api_client.master_split_paths


@api.dispatcher.add_method
def request_and_decrypt(url: str, method="get", body: str | dict = ""):
    if not api_client:
        raise RuntimeError("Init before calling this method")

    enqueue_job(lambda: api_client.request_and_decrypt(url, method, body))
    return get_answer()


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
