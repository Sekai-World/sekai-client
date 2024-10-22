import logging
import sys
import time
import requests

from os import getenv
from pytz import timezone
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.schedulers.base import STATE_RUNNING, EVENT_JOB_MAX_INSTANCES
from apscheduler.triggers.cron import CronTrigger
from apscheduler.events import JobEvent

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


def get_current_world_link_character(event_id, curr_time):
    json_url_base = 'https://sekai-world.github.io/sekai-master-db-diff' if pjsk_region == "jp" else f'https://sekai-world.github.io/sekai-master-db-{pjsk_region}-diff'
    json_url = f'{json_url_base}/worldBlooms.json'
    json_data = requests.get(json_url).json()

    # find the current world link character by event_id and current time
    for world_link in json_data:
        if world_link["eventId"] == event_id and world_link[
                "chapterStartAt"] < curr_time and world_link[
                    "aggregateAt"] > curr_time:
            return world_link["gameCharacterId"]

    return -1


def track_event_func():
    logger.info("Track event score triggered by cron job")

    ver_res = None
    try:
        logger.info("[track_event_func] Check game versions")
        ver_res = jsonrpc_client.request("check_versions", [version_info])
    except:
        logger.warning(
            "[track_event_func] Failed to execute check_versions, restart bootstraping..."
        )
        bootstrap()
        ver_res = jsonrpc_client.request("check_versions", [version_info])
    logger.debug('[track_event_func] got ver_res')

    global is_in_maintenance
    if ver_res["maintenance"]:
        logger.warning("PJSK server is in maintenance, skipping...")
        is_in_maintenance = True
        return

    is_in_maintenance = False
    if ver_res["new_version"]:
        logger.info("Got a new version during tracking event score")
        refresh_version()

    curr_time = int(time.time() * 1000)
    logger.debug(
        f'[track_event_func] call track_event_scores now at {curr_time}')
    try:
        track_event_scores(curr_time)
    except:
        logger.warning(
            '[W] Failed to track event scores, refresh version info and retrying...'
        )
        refresh_version()
        track_event_scores(curr_time)


def scheduler_listener(event: JobEvent):
    global track_event_job, track_event_cron_trigger
    if event.code == EVENT_JOB_MAX_INSTANCES:
        logger.error(
            f"Scheduler error: maximum number of running instances reached, job {event.job_id} skipped"
        )
        if event.job_id == track_event_job.id:
            logger.error("Track event job skipped, reset job...")
            track_event_job.remove()
            track_event_job = scheduler.add_job(track_event_func,
                                                track_event_cron_trigger,
                                                name="track_event_job")


scheduler = BlockingScheduler(timezone=timezone('Asia/Tokyo'))
track_event_cron_trigger = CronTrigger(minute='*/3')
track_event_job = scheduler.add_job(track_event_func,
                                    track_event_cron_trigger,
                                    name="track_event_job")
scheduler.add_listener(scheduler_listener, EVENT_JOB_MAX_INSTANCES)


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
    event_id = event_data["id"]

    # first 100
    logger.debug("[track_event_scores] fetching first 100 ranked players")
    first100_data = jsonrpc_client.request("fetch_event_rank_first_100",
                                           [event_id])
    if first100_data["isEventAggregate"]:
        logger.debug("[track_event_scores] event is aggregating, skipping...")
        return
    ranking_data["first100"] = first100_data["rankings"]
    logger.debug("[track_event_scores] fetched first 100 ranked players")

    logger.debug("[track_event_scores] fetching border cutoffs")
    border_data = jsonrpc_client.request("fetch_event_rank_border", [event_id])
    ranking_data["border"] = [
        x for x in border_data["borderRankings"] if x["rank"] > 100
    ]

    # logger.debug(
    #     f"[track_event_scores] posting event ranking result to api, result={ranking_data}, api_key={sekai_api_key}"
    # )
    try:
        r = requests.post(
            f'https://api.sekai.best/event/{event_data["id"]}/rankings',
            json=ranking_data,
            headers={"X-API-Key": sekai_api_key},
            params={"region": pjsk_region},
            timeout=30)
        r.raise_for_status()
        logger.debug("[track_event_scores] event ranking posted")
    except requests.Timeout as err:
        logger.error(f'Error posting event ranking result to api, {err}')
    except requests.HTTPError as err:
        logger.error(f'Error posting event ranking result to api, {r.content}')

    if event_data["eventType"] == "world_bloom":
        logger.debug(
            "[track_event_scores] world link event detected, posting world bloom chapter rankings"
        )
        curr_character_id = get_current_world_link_character(
            event_id, curr_time)
        if curr_character_id == -1:
            logger.error(
                "[track_event_scores] failed to get current world link character, skipping..."
            )
            return

        logger.debug(
            f"[track_event_scores] current world link chapter character id: {curr_character_id}"
        )
        chapter_ranking_data = {"time": curr_time}
        chapter_ranking_data["first100"] = [
            x for x in first100_data["userWorldBloomChapterRankings"]
            if x["gameCharacterId"] == curr_character_id
        ]
        chapter_ranking_data["border"] = [
            x for x in border_data["userWorldBloomChapterRankingBorders"]
            if x["gameCharacterId"] == curr_character_id
        ]
        for border in chapter_ranking_data["border"]:
            border["borderRankings"] = [
                x for x in border["borderRankings"] if x["rank"] > 100
            ]

        try:
            r = requests.post(
                f'https://api.sekai.best/event/{event_data["id"]}/chapter_rankings',
                json=chapter_ranking_data,
                headers={"X-API-Key": sekai_api_key},
                params={"region": pjsk_region},
                timeout=30)
            r.raise_for_status()
            logger.debug("[track_event_scores] event chapter ranking posted")
        except requests.Timeout as err:
            logger.error(f'Error posting event ranking result to api, {err}')
        except requests.HTTPError as err:
            logger.error(
                f'Error posting event ranking result to api, {r.content}')


def bootstrap():
    if not jsonrpc_client.request("is_init") and not jsonrpc_client.request(
            "init", [pjsk_region]):
        sys.exit(1)
    logger.info("[bootstrap] PJSK client inited")

    try:
        check_version_res = jsonrpc_client.request("check_versions")
        if check_version_res["maintenance"]:
            logger.warning(
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
        "[bootstrap] Finished, will track event result every 3 minutes")
    if scheduler.state != STATE_RUNNING:
        scheduler.start()


if __name__ == "__main__":
    bootstrap()
