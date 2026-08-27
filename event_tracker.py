import logging
import os
import sys
import time
from collections import Counter
from os import getenv
from typing import Any

import requests
from apscheduler.events import JobEvent
from apscheduler.schedulers.base import EVENT_JOB_MAX_INSTANCES, STATE_RUNNING
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from pytz import timezone
from requests.adapters import HTTPAdapter

from logging_config import configure_logging
from response_models import (
    ResponseValidationError,
    validate_current_event_response,
    validate_event_ranking_snapshot,
    validate_version_info,
)
from utils.constants import pjsk_region, sekai_api_key, strapi_base_url
from utils.event_outbox import EventRankingOutbox
from utils.jsonrpc_client import JSONRPCClient

LOGLEVEL = getenv("LOGLEVEL", "INFO").upper()
configure_logging(level=LOGLEVEL)
logger = logging.getLogger(__name__)


def _new_http_session() -> requests.Session:
    session = requests.Session()
    adapter = HTTPAdapter(
        pool_connections=4,
        pool_maxsize=4,
        max_retries=0,
        pool_block=True,
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


_rpc_session = _new_http_session()
_external_session = _new_http_session()
jsonrpc_client = JSONRPCClient(
    f"http://localhost:{getenv('JSONRPC_PORT', '3939')}/",
    transport=_rpc_session,
)

version_info: dict[str, Any] | None = None
event_data: dict[str, Any] | None = None
is_in_maintenance = False
_DELIVERY_TIMEOUT_SECONDS = float(getenv("EVENT_TRACKER_DELIVERY_TIMEOUT", "15"))
_DRAIN_MAX_DURATION_SECONDS = float(getenv("EVENT_TRACKER_DRAIN_MAX_DURATION", "30"))
_OUTBOX_RETENTION_SECONDS = float(getenv("EVENT_TRACKER_OUTBOX_RETENTION", "86400"))

_OUTBOX_PATH = getenv(
    "EVENT_TRACKER_OUTBOX_PATH",
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        ".runtime",
        f"event-tracker-{pjsk_region}.sqlite3",
    ),
)
ranking_outbox: EventRankingOutbox | None = None
_world_blooms_cache: tuple[float, list[dict[str, Any]]] | None = None
_WORLD_BLOOMS_CACHE_SECONDS = 300.0
_metric_counts: Counter[str] = Counter()
_metric_duration_totals: dict[str, float] = {}


def _record_stage(stage: str, started_at: float) -> None:
    duration = max(0.0, time.monotonic() - started_at)
    _metric_counts[f"stage.{stage}.observations"] += 1
    _metric_duration_totals[stage] = _metric_duration_totals.get(stage, 0.0) + duration
    logger.info(
        "[event_tracker_metrics] stage=%s duration_seconds=%.3f",
        stage,
        duration,
    )


def _metrics_snapshot() -> dict[str, dict[str, int | float]]:
    return {
        "counts": dict(_metric_counts),
        "duration_seconds_total": dict(_metric_duration_totals),
    }


def _ranking_outbox() -> EventRankingOutbox:
    global ranking_outbox
    if ranking_outbox is None:
        ranking_outbox = EventRankingOutbox(
            _OUTBOX_PATH,
            retention_seconds=_OUTBOX_RETENTION_SECONDS,
        )
    return ranking_outbox


curr_event_url = (
    f"{strapi_base_url}/sekai-current-event"
    if pjsk_region == "jp"
    else f"{strapi_base_url}/sekai-current-event-{pjsk_region}"
)


def get_current_world_link_character(event_id, curr_time):
    global _world_blooms_cache
    json_url_base = (
        "https://sekai-world.github.io/sekai-master-db-diff"
        if pjsk_region == "jp"
        else f"https://sekai-world.github.io/sekai-master-db-{pjsk_region}-diff"
    )
    if pjsk_region == "tw":
        json_url_base = "https://sekai-world.github.io/sekai-master-db-tc-diff"
    json_url = f"{json_url_base}/worldBlooms.json"
    now = time.monotonic()
    if _world_blooms_cache is None or _world_blooms_cache[0] <= now:
        response = _external_session.get(json_url, timeout=60)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list) or not all(
            isinstance(item, dict) for item in payload
        ):
            raise RuntimeError("World Bloom metadata response must be a list")
        _world_blooms_cache = (now + _WORLD_BLOOMS_CACHE_SECONDS, payload)
    json_data = _world_blooms_cache[1]

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


def _track_event_cycle():
    logger.info("Track event score triggered by cron job")

    ver_res = None
    version_started = time.monotonic()
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
    finally:
        _record_stage("version_check", version_started)
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
        _metric_counts["collection.failed"] += 1
        logger.exception("[track_event_func] Failed to track event scores")
        logger.warning(
            "[W] Failed to track event scores, refresh version info and retrying..."
        )
        refresh_version()
        track_event_scores(curr_time)


def track_event_func():
    started_at = time.monotonic()
    try:
        _track_event_cycle()
    finally:
        _drain_ranking_outbox()
        logger.info(
            "[track_event_func] execution_seconds=%.3f",
            time.monotonic() - started_at,
        )
        logger.info("[event_tracker_metrics] snapshot=%s", _metrics_snapshot())


scheduler = BlockingScheduler(timezone=timezone("Asia/Tokyo"))
track_event_cron_trigger = CronTrigger(minute="*/3")
track_event_job = scheduler.add_job(
    track_event_func,
    track_event_cron_trigger,
    name="track_event_job",
    max_instances=1,
    coalesce=True,
)


def _scheduler_listener(event: JobEvent) -> None:
    if event.code == EVENT_JOB_MAX_INSTANCES:
        _metric_counts["scheduler.skipped"] += 1


scheduler.add_listener(_scheduler_listener, EVENT_JOB_MAX_INSTANCES)


def refresh_version():
    logger.info("Refresh version info")

    response = _external_session.get(curr_event_url, timeout=60)
    response.raise_for_status()
    payload = response.json()
    # Validate the current-event boundary (eventJson presence, event identity
    # and timing fields) first. Validation raises before any module-level state
    # is assigned, so a malformed payload never overwrites last-known-good state.
    # The repository has no verified mapping between ``pjsk_region`` and the
    # optional upstream Strapi ``region``/``regionCode`` markers, so we do NOT
    # compare the raw region against them. We still pass no ``expected_region``,
    # which lets the validator type-check those optional fields without rejecting
    # them incorrectly. Callers with a verified region mapping may pass
    # ``expected_region`` to enforce an exact match.
    try:
        current_event = validate_current_event_response(payload)
    except ResponseValidationError as error:
        raise RuntimeError(f"Invalid current event response: {error}") from error

    # Fetch and validate the version_info boundary before assigning it. Both the
    # current-event payload and the version_info are fully validated before any
    # of ``event_data``/``_world_blooms_cache``/``version_info`` is overwritten,
    # so if either fails the last-known-good values are preserved.
    raw_version_info = jsonrpc_client.request("version_info")
    try:
        validated_version_info = validate_version_info(
            raw_version_info, require_cdn_version=pjsk_region in ("cn", "tw", "kr")
        )
    except ResponseValidationError as error:
        raise RuntimeError(f"Invalid version info response: {error}") from error

    global event_data
    event_data = current_event

    global _world_blooms_cache
    _world_blooms_cache = None

    global version_info
    version_info = validated_version_info
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
    started_at = time.monotonic()
    try:
        response = _external_session.post(
            endpoint,
            json=payload,
            headers={"X-API-Key": sekai_api_key, "Idempotency-Key": key},
            params={"region": pjsk_region},
            timeout=_DELIVERY_TIMEOUT_SECONDS,
        )
        _metric_counts[f"delivery.http_{response.status_code // 100}xx"] += 1
        response.raise_for_status()
    except Exception:
        _metric_counts["delivery.failed"] += 1
        raise
    else:
        _metric_counts["delivery.succeeded"] += 1
    finally:
        _record_stage("delivery", started_at)


def _drain_ranking_outbox() -> None:
    result = _ranking_outbox().drain(
        _deliver_ranking,
        max_duration_seconds=_DRAIN_MAX_DURATION_SECONDS,
    )
    _metric_counts["outbox.sent"] += result["sent"]
    _metric_counts["outbox.failed"] += result["failed"]
    _metric_counts["outbox.retained"] += result["retained"]
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
    started_at = time.monotonic()
    try:
        _ranking_outbox().enqueue(
            region=pjsk_region,
            event_id=event_id,
            collected_at=curr_time,
            data_type="ranking",
            endpoint=f"https://api.sekai.best/event/{event_id}/rankings",
            payload=ranking_data,
        )
    finally:
        _record_stage("durable_enqueue", started_at)


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
    started_at = time.monotonic()
    try:
        _ranking_outbox().enqueue(
            region=pjsk_region,
            event_id=event_id,
            collected_at=curr_time,
            data_type=f"chapter:{character_id}",
            endpoint=f"https://api.sekai.best/event/{event_id}/chapter_rankings",
            payload=chapter_ranking_data,
        )
    finally:
        _record_stage("durable_enqueue", started_at)


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

    ranking_data = {"time": curr_time}
    event_id = event_data["id"]

    collection_started = time.monotonic()
    snapshot = jsonrpc_client.request("fetch_event_rank_snapshot", [event_id])
    # Validate the combined ranking snapshot (first100/border shapes, ranking
    # identity and value ranges). ``border`` is optional: when present it must
    # match the expected shape. Validation raises before any ranking data is
    # enqueued, so a malformed snapshot never overwrites the last-known-good
    # delivery state.
    try:
        validate_event_ranking_snapshot(snapshot)
    except ResponseValidationError as error:
        raise RuntimeError(f"Invalid event ranking snapshot: {error}") from error
    first100_data = snapshot["first100"]
    _record_stage("game_snapshot", collection_started)
    if first100_data["isEventAggregate"]:
        logger.debug("[track_event_scores] event is aggregating, skipping...")
        return
    border_data = snapshot["border"]
    ranking_data["first100"] = first100_data["rankings"]
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
