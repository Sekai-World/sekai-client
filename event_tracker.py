import logging
import os
import sys
import time
from os import getenv
from typing import Any

import requests
from apscheduler.schedulers.base import STATE_RUNNING
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from pytz import timezone

from logging_config import configure_logging
from utils.constants import pjsk_region, sekai_api_key, strapi_base_url
from utils.event_outbox import EventRankingOutbox
from utils.jsonrpc_client import JSONRPCClient

LOGLEVEL = getenv("LOGLEVEL", "INFO").upper()
configure_logging(level=LOGLEVEL)
logger = logging.getLogger(__name__)

jsonrpc_client = JSONRPCClient(f"http://localhost:{getenv('JSONRPC_PORT', '3939')}/")

version_info: dict[str, Any] | None = None
event_data: dict[str, Any] | None = None
is_in_maintenance = False

_OUTBOX_PATH = getenv(
    "EVENT_TRACKER_OUTBOX_PATH",
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        ".runtime",
        f"event-tracker-{pjsk_region}.sqlite3",
    ),
)
ranking_outbox: EventRankingOutbox | None = None


def _ranking_outbox() -> EventRankingOutbox:
    global ranking_outbox
    if ranking_outbox is None:
        ranking_outbox = EventRankingOutbox(_OUTBOX_PATH)
    return ranking_outbox


curr_event_url = (
    f"{strapi_base_url}/sekai-current-event"
    if pjsk_region == "jp"
    else f"{strapi_base_url}/sekai-current-event-{pjsk_region}"
)


def get_current_world_link_character(event_id, curr_time):
    json_url_base = (
        "https://sekai-world.github.io/sekai-master-db-diff"
        if pjsk_region == "jp"
        else f"https://sekai-world.github.io/sekai-master-db-{pjsk_region}-diff"
    )
    if pjsk_region == "tw":
        json_url_base = "https://sekai-world.github.io/sekai-master-db-tc-diff"
    json_url = f"{json_url_base}/worldBlooms.json"
    json_data = requests.get(json_url, timeout=60).json()

    # find the current world link character by event_id and current time
    curr_id = -1
    aggr_id = -1
    for world_link in json_data:
        if (
            world_link["eventId"] == event_id
            and world_link["chapterStartAt"] < curr_time
            and world_link["aggregateAt"] > curr_time
        ):
            curr_id = world_link["gameCharacterId"]
        if (
            world_link["eventId"] == event_id
            and world_link["chapterEndAt"] < curr_time
            and world_link["chapterEndAt"] + 5 * 60 * 1000 > curr_time
        ):
            aggr_id = world_link["gameCharacterId"]

    return curr_id, aggr_id


def track_event_func():
    started_at = time.monotonic()
    logger.info("Track event score triggered by cron job")

    ver_res = None
    try:
        logger.info("[track_event_func] Check game versions")
        ver_res = jsonrpc_client.request("check_versions", [version_info])
    except Exception:
        logger.exception(
            "[track_event_func] Failed to execute check_versions, restart bootstrapping"
        )
        logger.warning(
            "[track_event_func] Failed to execute check_versions, restart "
            "bootstrapping..."
        )
        bootstrap()
        ver_res = jsonrpc_client.request("check_versions", [version_info])
    logger.debug("[track_event_func] got ver_res")

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
    logger.debug("[track_event_func] call track_event_scores now at %s", curr_time)
    try:
        _drain_ranking_outbox()
        track_event_scores(curr_time)
    except Exception:
        logger.exception("[track_event_func] Failed to track event scores")
        logger.warning(
            "[W] Failed to track event scores, refresh version info and retrying..."
        )
        refresh_version()
        track_event_scores(curr_time)
    finally:
        _drain_ranking_outbox()
        logger.info(
            "[track_event_func] execution_seconds=%.3f",
            time.monotonic() - started_at,
        )


scheduler = BlockingScheduler(timezone=timezone("Asia/Tokyo"))
track_event_cron_trigger = CronTrigger(minute="*/3")
track_event_job = scheduler.add_job(
    track_event_func,
    track_event_cron_trigger,
    name="track_event_job",
    max_instances=1,
    coalesce=True,
)


def refresh_version():
    logger.info("Refresh version info")

    global event_data
    event_data = requests.get(curr_event_url, timeout=60).json()["eventJson"]

    global version_info
    version_info = jsonrpc_client.request("version_info")
    return version_info


def _is_tracking_window_closed(curr_time: int) -> bool:
    if not event_data:
        return True
    return bool(curr_time >= (event_data["closedAt"] - 15 * 60 * 1000))


def _should_skip_event_tracking(curr_time: int) -> bool:
    return (
        (not event_data)
        or curr_time < event_data["startAt"]
        or (
            curr_time > (event_data["rankingAnnounceAt"] + 6 * 60 * 1000)
            and curr_time < (event_data["closedAt"] - 15 * 60 * 1000)
        )
        or (
            curr_time > event_data["aggregateAt"]
            and curr_time < event_data["rankingAnnounceAt"]
        )
    )


def _deliver_ranking(endpoint: str, payload: dict[str, Any], key: str) -> None:
    response = requests.post(
        endpoint,
        json=payload,
        headers={"X-API-Key": sekai_api_key, "Idempotency-Key": key},
        params={"region": pjsk_region},
        timeout=60,
    )
    response.raise_for_status()


def _drain_ranking_outbox() -> None:
    result = _ranking_outbox().drain(_deliver_ranking)
    metrics = _ranking_outbox().metrics()
    logger.info(
        "[ranking_outbox] sent=%s failed=%s retained=%s pending=%s sending=%s "
        "failed_total=%s oldest_pending_age_seconds=%.3f",
        result["sent"],
        result["failed"],
        result["retained"],
        metrics.pending,
        metrics.sending,
        metrics.failed,
        metrics.oldest_pending_age_seconds,
    )


def _enqueue_event_ranking(event_id: int, curr_time: int, ranking_data: dict) -> None:
    _ranking_outbox().enqueue(
        region=pjsk_region,
        event_id=event_id,
        collected_at=curr_time,
        data_type="ranking",
        endpoint=f"https://api.sekai.best/event/{event_id}/rankings",
        payload=ranking_data,
    )


def _build_chapter_ranking_data(
    curr_time: int,
    character_id: int,
    first100_data: dict,
    border_data: dict,
) -> dict:
    chapter_ranking_data: dict[str, Any] = {"time": curr_time}
    chapter_ranking_data["first100"] = [
        x
        for x in first100_data["userWorldBloomChapterRankings"]
        if x["gameCharacterId"] == character_id
    ]
    chapter_ranking_data["border"] = [
        x
        for x in border_data["userWorldBloomChapterRankingBorders"]
        if x["gameCharacterId"] == character_id
    ]
    for border in chapter_ranking_data["border"]:
        border["borderRankings"] = [
            x for x in border["borderRankings"] if x["rank"] > 100
        ]
    return chapter_ranking_data


def _enqueue_world_bloom_chapter_ranking(
    event_id: int, curr_time: int, character_id: int, chapter_ranking_data: dict
) -> None:
    _ranking_outbox().enqueue(
        region=pjsk_region,
        event_id=event_id,
        collected_at=curr_time,
        data_type=f"chapter:{character_id}",
        endpoint=f"https://api.sekai.best/event/{event_id}/chapter_rankings",
        payload=chapter_ranking_data,
    )


def _track_world_bloom_chapters(
    event_id: int,
    curr_time: int,
    first100_data: dict,
    border_data: dict,
) -> None:
    logger.debug(
        "[track_event_scores] world link event detected, posting world bloom "
        "chapter rankings"
    )
    curr_character_id, aggregated_character_id = get_current_world_link_character(
        event_id, curr_time
    )

    if curr_character_id == -1:
        logger.warning(
            "[track_event_scores] no ongoing world link chapter, skipping..."
        )
    else:
        logger.debug(
            "[track_event_scores] current world link chapter character id: %s",
            curr_character_id,
        )
        chapter_ranking_data = _build_chapter_ranking_data(
            curr_time, curr_character_id, first100_data, border_data
        )
        _enqueue_world_bloom_chapter_ranking(
            event_id, curr_time, curr_character_id, chapter_ranking_data
        )

    if aggregated_character_id == -1:
        logger.debug(
            "[track_event_scores] no aggregated world link chapter, skipping..."
        )
        return

    logger.debug(
        "[track_event_scores] aggregated world link chapter character id: %s",
        aggregated_character_id,
    )
    aggregated_chapter_ranking_data = _build_chapter_ranking_data(
        curr_time, aggregated_character_id, first100_data, border_data
    )
    _enqueue_world_bloom_chapter_ranking(
        event_id, curr_time, aggregated_character_id, aggregated_chapter_ranking_data
    )


def track_event_scores(curr_time):
    if _should_skip_event_tracking(curr_time):
        logger.warning("No ongoing event, skipping...")
        return
    if _is_tracking_window_closed(curr_time):
        logger.warning("Current event will expire soon")
        raise RuntimeError("Current event will expire soon")

    user_id = jsonrpc_client.request("account_info")["userId"]
    logger.debug("[track_event_scores] got user id %s", user_id)

    ranking_data = {"time": curr_time}
    event_id = event_data["id"]

    # first 100
    logger.debug("[track_event_scores] fetching first 100 ranked players")
    first100_data = jsonrpc_client.request("fetch_event_rank_first_100", [event_id])
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
    #     "[track_event_scores] posting event ranking result to api, "
    #     "result=%s",
    #     ranking_data,
    # )
    _enqueue_event_ranking(event_data["id"], curr_time, ranking_data)

    if event_data["eventType"] == "world_bloom":
        _track_world_bloom_chapters(event_id, curr_time, first100_data, border_data)


def bootstrap():
    if not jsonrpc_client.request("is_init") and not jsonrpc_client.request(
        "init", [pjsk_region]
    ):
        sys.exit(1)
    logger.info("[bootstrap] PJSK client inited")

    while True:
        try:
            check_version_res = jsonrpc_client.request("check_versions")
            if check_version_res["maintenance"]:
                logger.warning(
                    "[bootstrap] Server in maintenance, retry after 10 minutes"
                )
                time.sleep(10 * 60)
                continue

            jsonrpc_client.request("login")
            refresh_version()
            break
        except Exception:
            logger.exception(
                "[bootstrap] Failed to bootstrap, possible reasons: "
                "connection error or account info expired (for tw and kr "
                "servers). Retry after 10 minutes."
            )
            time.sleep(10 * 60)
    logger.info("[bootstrap] Fetched current available version info")

    logger.info("[bootstrap] Finished, will track event result every 3 minutes")
    if scheduler.state != STATE_RUNNING:
        scheduler.start()


if __name__ == "__main__":
    bootstrap()
