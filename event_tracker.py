import logging
import sys
import time
import requests

from os import getenv
from pytz import timezone
from git.repo import Repo
from git.util import Actor
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.schedulers.base import STATE_RUNNING
from apscheduler.triggers.cron import CronTrigger

from utils.jsonrpc_client import JSONRPCClient
from utils.constants import strapi_base_url, pjsk_region, sekai_api_key

LOGLEVEL = getenv('LOGLEVEL', 'INFO').upper()
logging.basicConfig(level=LOGLEVEL, format='%(asctime)s %(message)s')
logger = logging.getLogger(__name__)

jsonrpc_client = JSONRPCClient(
    f'http://localhost:{getenv("JSONRPC_PORT", "3939")}/')

version_info = None
event_data = None
is_in_maintenance = False

curr_event_url = f'{strapi_base_url}/sekai-current-event' if pjsk_region == "jp" else f'{strapi_base_url}/sekai-current-event-{pjsk_region}'


def track_event_func():
    logger.info("Track event score triggered by cron job")

    ver_res = None
    try:
        ver_res = jsonrpc_client.request("check_versions", [version_info])
    except:
        bootstrap()
        ver_res = jsonrpc_client.request("check_versions", [version_info])
    logger.debug('[track_event_func] got ver_res')

    global is_in_maintenance
    if ver_res["maintenance"]:
        logger.warn("PJSK server is in maintenance, skipping...")
        is_in_maintenance = True
        return

    is_in_maintenance = False
    if ver_res["new_version"]:
        logger.info("Got a new version during tracking event score")
        refresh_version()

    curr_time = int(time.time() * 1000)
    logger.debug(f'[track_event_func] call track_event_scores now at {curr_time}')
    try:
        track_event_scores(curr_time)
    except:
        refresh_version()
        track_event_scores(curr_time)


scheduler = BlockingScheduler(timezone=timezone('Asia/Tokyo'))
track_event_cron_trigger = CronTrigger(second='58')
track_event_job = scheduler.add_job(track_event_func,
                                    track_event_cron_trigger,
                                    name="track_event_job")


def refresh_version():
    logger.info("Refersh version info")

    global event_data
    event_data = requests.get(curr_event_url).json()["eventJson"]

    global version_info
    version_info = jsonrpc_client.request("version_info")
    return version_info


def track_event_scores(curr_time):
    if not event_data or curr_time < event_data["startAt"] or (
            curr_time >
        (event_data["rankingAnnounceAt"] + 6 * 60 * 1000) and curr_time <
        (event_data["closedAt"] - 10 * 1000)) or (
            curr_time > event_data["aggregateAt"]
            and curr_time < event_data["rankingAnnounceAt"]):
        logger.warning("No ongoing event, skipping...")
        return
    elif curr_time >= (event_data["closedAt"] - 10 * 1000):
        logger.warning("Current event will expire soon")
        raise RuntimeError("Current event will expire soon")

    user_id = jsonrpc_client.request("account_info")["userId"]
    logger.debug(f"[track_event_scores] got user id {user_id}")

    ranking_data = {"time": curr_time}

    ranking_data["first10"] = jsonrpc_client.request("call_pjsk_api", [
        f'/user/{user_id}/event/{event_data["id"]}/ranking?targetRank=1&lowerLimit=9'
    ])["rankings"]
    logger.debug("[track_event_scores] fetched first ten ranked players")

    logger.debug("[track_event_scores] fetching critical cutoffs")
    for i in range(2, 6):
        ranking_data[f"rank{i}0"] = jsonrpc_client.request(
            "call_pjsk_api", [
                f'/user/{user_id}/event/{event_data["id"]}/ranking?targetRank={i}0&lowerLimit=0'
            ])["rankings"]
    for i in range(1, 6):
        ranking_data[f"rank{i}00"] = jsonrpc_client.request(
            "call_pjsk_api", [
                f'/user/{user_id}/event/{event_data["id"]}/ranking?targetRank={i}00&lowerLimit=0'
            ])["rankings"]
    for i in range(1, 6):
        ranking_data[f"rank{i}000"] = jsonrpc_client.request(
            "call_pjsk_api", [
                f'/user/{user_id}/event/{event_data["id"]}/ranking?targetRank={i}000&lowerLimit=0'
            ])["rankings"]
    for i in range(1, 6):
        ranking_data[f"rank{i}0000"] = jsonrpc_client.request(
            "call_pjsk_api", [
                f'/user/{user_id}/event/{event_data["id"]}/ranking?targetRank={i}0000&lowerLimit=0'
            ])["rankings"]
    ranking_data[f"rank100000"] = jsonrpc_client.request(
        "call_pjsk_api", [
            f'/user/{user_id}/event/{event_data["id"]}/ranking?targetRank=100000&lowerLimit=0'
        ])["rankings"]

    logger.debug(f"[track_event_scores] posting event ranking result to api, result={ranking_data}, api_key={sekai_api_key}")
    try:
        r = requests.post(
            f'https://api.sekai.best/event/{event_data["id"]}/rankings',
            json=ranking_data,
            headers={"X-API-Key": sekai_api_key},
            params={"region": pjsk_region})
        r.raise_for_status()
    except requests.HTTPError as err:
        logger.error(f'Error posting event ranking result to api, {r.content}')


def bootstrap():
    if not jsonrpc_client.request("is_init") and not jsonrpc_client.request(
            "init", [pjsk_region]):
        sys.exit(1)
    logger.info("[bootstrap] PJSK client inited")
    
    try:
        check_version_res = jsonrpc_client.request("check_versions")
        if check_version_res["maintenance"]:
            logger.warn(
                "[bootstrap] Server in maintenance, retry after 10 minutes")
            time.sleep(10 * 60)
            bootstrap()
            return

        jsonrpc_client.request("login")
        refresh_version()
    except:
        logger.error(
            "[bootstrap] Failed to bootstrap, possible reasons: connection error or account info expired (for tw and kr servers). Retry after 10 minutes."
        )
        time.sleep(10 * 60)
        bootstrap()
        return
    logger.info("[bootstrap] Fetched current available version info")
    
    logger.info(
        "[bootstrap] Finished, will track event result at 58 seconds of each minute"
    )
    if scheduler.state != STATE_RUNNING:
        scheduler.start()

if __name__ == "__main__":
    bootstrap()
