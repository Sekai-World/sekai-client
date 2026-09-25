import logging
from unittest.mock import Mock

import event_tracker


def test_scheduler_is_single_coalescing_job():
    jobs = event_tracker.scheduler.get_jobs()

    assert len(jobs) == 1
    assert jobs[0].max_instances == 1
    assert jobs[0].coalesce is True


def test_ranking_is_persisted_before_delivery(monkeypatch):
    outbox = Mock()
    monkeypatch.setattr(event_tracker, "ranking_outbox", outbox)

    event_tracker._enqueue_event_ranking(12, 345, {"time": 345})

    outbox.enqueue.assert_called_once_with(
        region=event_tracker.pjsk_region,
        event_id=12,
        collected_at=345,
        data_type="ranking",
        endpoint="https://api.sekai.best/event/12/rankings",
        payload={"time": 345},
    )


def test_delivery_sends_idempotency_key(monkeypatch):
    response = Mock()
    response.status_code = 200
    post = Mock(return_value=response)
    monkeypatch.setattr(event_tracker._external_session, "post", post)

    event_tracker._deliver_ranking(
        "https://api.example.test/rankings", {"time": 1}, "tw:2:1:ranking"
    )

    post.assert_called_once_with(
        "https://api.example.test/rankings",
        json={"time": 1},
        headers={
            "X-API-Key": event_tracker.sekai_api_key,
            "Idempotency-Key": "tw:2:1:ranking",
        },
        params={"region": event_tracker.pjsk_region},
        timeout=15.0,
    )
    response.raise_for_status.assert_called_once_with()


def test_world_bloom_metadata_is_cached(monkeypatch):
    response = Mock()
    response.json.return_value = [
        {
            "eventId": 12,
            "chapterStartAt": 1,
            "chapterEndAt": 50,
            "aggregateAt": 100,
            "gameCharacterId": 7,
        }
    ]
    get = Mock(return_value=response)
    monkeypatch.setattr(event_tracker._external_session, "get", get)
    monkeypatch.setattr(event_tracker, "_world_blooms_cache", None)

    assert event_tracker.get_current_world_link_character(12, 25) == (7, -1)
    assert event_tracker.get_current_world_link_character(12, 25) == (7, -1)

    get.assert_called_once()
    response.raise_for_status.assert_called_once_with()


def test_finale_world_bloom_metadata_without_character_returns_sentinels(monkeypatch):
    response = Mock()
    response.json.return_value = [
        {
            "eventId": 180,
            "chapterStartAt": 1,
            "chapterEndAt": 50,
            "aggregateAt": 100,
        }
    ]
    monkeypatch.setattr(
        event_tracker._external_session, "get", Mock(return_value=response)
    )
    monkeypatch.setattr(event_tracker, "_world_blooms_cache", None)

    assert event_tracker.get_current_world_link_character(180, 75) == (-1, -1)


def test_finale_without_character_enqueues_only_event_ranking(monkeypatch):
    outbox = Mock()
    monkeypatch.setattr(event_tracker, "ranking_outbox", outbox)
    monkeypatch.setattr(
        event_tracker,
        "event_data",
        {
            "id": 180,
            "eventType": "world_bloom",
            "startAt": 0,
            "aggregateAt": 1_000_000,
            "rankingAnnounceAt": 1_100_000,
            "closedAt": 2_000_000,
        },
    )
    request = Mock(
        return_value={
            "first100": {
                "isEventAggregate": False,
                "rankings": [],
                "userWorldBloomChapterRankings": [],
            },
            "border": {
                "borderRankings": [],
                "userWorldBloomChapterRankingBorders": [],
            },
        }
    )
    monkeypatch.setattr(event_tracker.jsonrpc_client, "request", request)
    response = Mock()
    response.json.return_value = [
        {
            "eventId": 180,
            "chapterStartAt": 1,
            "chapterEndAt": 50,
            "aggregateAt": 100,
        }
    ]
    monkeypatch.setattr(
        event_tracker._external_session, "get", Mock(return_value=response)
    )
    monkeypatch.setattr(event_tracker, "_world_blooms_cache", None)

    event_tracker.track_event_scores(25)

    assert outbox.enqueue.call_count == 1
    assert [call.kwargs["data_type"] for call in outbox.enqueue.call_args_list] == [
        "ranking"
    ]


def test_drain_reports_distinct_delivery_state(monkeypatch, caplog):
    outbox = Mock()
    outbox.drain.return_value = {"sent": 0, "failed": 0, "retained": 1}
    outbox.metrics.return_value = Mock(
        pending=1, sending=0, failed=0, oldest_pending_age_seconds=4.5
    )
    monkeypatch.setattr(event_tracker, "ranking_outbox", outbox)

    event_tracker._drain_ranking_outbox()

    assert "sent=0 failed=0 retained=1 pending=1" in caplog.text
    outbox.drain.assert_called_once_with(
        event_tracker._deliver_ranking,
        max_duration_seconds=event_tracker._DRAIN_MAX_DURATION_SECONDS,
    )


def test_maintenance_path_drains_pending_outbox(monkeypatch):
    drain = Mock()
    monkeypatch.setattr(event_tracker, "_drain_ranking_outbox", drain)
    monkeypatch.setattr(
        event_tracker.jsonrpc_client,
        "request",
        Mock(return_value={"maintenance": True, "new_version": False}),
    )

    event_tracker.track_event_func()

    drain.assert_called_once_with()


def test_collection_uses_combined_snapshot_rpc(monkeypatch):
    outbox = Mock()
    monkeypatch.setattr(event_tracker, "ranking_outbox", outbox)
    monkeypatch.setattr(
        event_tracker,
        "event_data",
        {
            "id": 12,
            "eventType": "marathon",
            "startAt": 0,
            "aggregateAt": 1_000_000,
            "rankingAnnounceAt": 1_100_000,
            "closedAt": 2_000_000,
        },
    )
    request = Mock(
        return_value={
            "first100": {"isEventAggregate": False, "rankings": []},
            "border": {"borderRankings": []},
        }
    )
    monkeypatch.setattr(event_tracker.jsonrpc_client, "request", request)

    event_tracker.track_event_scores(1_000)

    request.assert_called_once_with("fetch_event_rank_snapshot", [12])
    outbox.enqueue.assert_called_once()


def test_collection_recovers_when_snapshot_rpc_fails(monkeypatch):
    outbox = Mock()
    monkeypatch.setattr(event_tracker, "ranking_outbox", outbox)
    monkeypatch.setattr(
        event_tracker,
        "event_data",
        {
            "id": 12,
            "eventType": "marathon",
            "startAt": 0,
            "aggregateAt": 1_000_000,
            "rankingAnnounceAt": 1_100_000,
            "closedAt": 2_000_000,
        },
    )
    request = Mock(
        side_effect=[
            RuntimeError("rpc"),
            {"ready": True},
            {
                "first100": {"isEventAggregate": False, "rankings": []},
                "border": {"borderRankings": []},
            },
        ]
    )
    monkeypatch.setattr(event_tracker.jsonrpc_client, "request", request)

    event_tracker.track_event_scores(1_000)

    assert [call.args for call in request.call_args_list] == [
        ("fetch_event_rank_snapshot", [12]),
        ("ensure_ready",),
        ("fetch_event_rank_snapshot", [12]),
    ]
    outbox.enqueue.assert_called_once()


def test_collection_skips_border_and_enqueue_during_aggregation(monkeypatch):
    outbox = Mock()
    monkeypatch.setattr(event_tracker, "ranking_outbox", outbox)
    monkeypatch.setattr(
        event_tracker,
        "event_data",
        {
            "id": 12,
            "eventType": "marathon",
            "startAt": 0,
            "aggregateAt": 1_000_000,
            "rankingAnnounceAt": 1_100_000,
            "closedAt": 2_000_000,
        },
    )
    request = Mock(
        return_value={
            "first100": {"isEventAggregate": True, "rankings": []},
            "border": None,
        }
    )
    monkeypatch.setattr(event_tracker.jsonrpc_client, "request", request)

    event_tracker.track_event_scores(1_000)

    request.assert_called_once_with("fetch_event_rank_snapshot", [12])
    outbox.enqueue.assert_not_called()


def test_collection_skips_after_event_closed(monkeypatch):
    outbox = Mock()
    monkeypatch.setattr(event_tracker, "ranking_outbox", outbox)
    monkeypatch.setattr(
        event_tracker,
        "event_data",
        {
            "id": 12,
            "eventType": "marathon",
            "startAt": 0,
            "aggregateAt": 1_000,
            "rankingAnnounceAt": 2_000,
            "closedAt": 3_000,
        },
    )
    request = Mock()
    monkeypatch.setattr(event_tracker.jsonrpc_client, "request", request)
    refresh = Mock()
    monkeypatch.setattr(event_tracker, "refresh_version", refresh)

    event_tracker.track_event_scores(3_000)

    refresh.assert_called_once_with()
    request.assert_not_called()
    outbox.enqueue.assert_not_called()


def _closed_event(event_id=12):
    return {
        "id": event_id,
        "eventType": "marathon",
        "startAt": 0,
        "aggregateAt": 1_000,
        "rankingAnnounceAt": 2_000,
        "closedAt": 3_000,
    }


def test_idle_tracker_picks_up_next_event_after_close(monkeypatch):
    outbox = Mock()
    monkeypatch.setattr(event_tracker, "ranking_outbox", outbox)
    monkeypatch.setattr(event_tracker, "event_data", _closed_event())
    next_event = {
        "id": 13,
        "eventType": "marathon",
        "startAt": 4_000,
        "aggregateAt": 1_000_000,
        "rankingAnnounceAt": 1_100_000,
        "closedAt": 2_000_000,
    }

    def refresh():
        event_tracker.event_data = next_event

    monkeypatch.setattr(event_tracker, "refresh_version", Mock(side_effect=refresh))
    request = Mock(
        return_value={
            "first100": {"isEventAggregate": False, "rankings": []},
            "border": {"borderRankings": []},
        }
    )
    monkeypatch.setattr(event_tracker.jsonrpc_client, "request", request)

    event_tracker.track_event_scores(5_000)

    request.assert_called_once_with("fetch_event_rank_snapshot", [13])
    outbox.enqueue.assert_called_once()


def test_closing_window_refreshes_without_raising(monkeypatch):
    outbox = Mock()
    monkeypatch.setattr(event_tracker, "ranking_outbox", outbox)
    event = {
        "id": 12,
        "eventType": "marathon",
        "startAt": 0,
        "aggregateAt": 1_000,
        "rankingAnnounceAt": 2_000,
        "closedAt": 20 * 60 * 1000,
    }
    monkeypatch.setattr(event_tracker, "event_data", event)
    refresh = Mock()
    monkeypatch.setattr(event_tracker, "refresh_version", refresh)
    request = Mock()
    monkeypatch.setattr(event_tracker.jsonrpc_client, "request", request)

    # Inside the final 15 minutes before closedAt.
    event_tracker.track_event_scores(10 * 60 * 1000)

    refresh.assert_called_once_with()
    request.assert_not_called()
    outbox.enqueue.assert_not_called()


def test_idle_cycles_log_the_pause_once(monkeypatch, caplog):
    monkeypatch.setattr(event_tracker, "event_data", _closed_event())
    monkeypatch.setattr(event_tracker, "refresh_version", Mock())
    monkeypatch.setattr(event_tracker, "_tracking_idle", False)
    caplog.set_level(logging.INFO, logger=event_tracker.logger.name)

    for _ in range(3):
        event_tracker.track_event_scores(5_000)

    idle_records = [r for r in caplog.records if "No ongoing event" in r.message]
    assert [r.levelno for r in idle_records] == [logging.INFO]


def test_drain_without_activity_logs_at_debug(monkeypatch, caplog):
    outbox = Mock()
    outbox.drain.return_value = {"sent": 0, "failed": 0, "retained": 0}
    outbox.metrics.return_value = Mock(
        pending=0, sending=0, failed=0, oldest_pending_age_seconds=0.0
    )
    monkeypatch.setattr(event_tracker, "ranking_outbox", outbox)
    caplog.set_level(logging.INFO, logger=event_tracker.logger.name)

    event_tracker._drain_ranking_outbox()

    assert "[ranking_outbox]" not in caplog.text


def test_http_sessions_use_bounded_connection_pools():
    session = event_tracker._new_http_session()

    adapter = session.get_adapter("https://")
    assert adapter._pool_connections == 4
    assert adapter._pool_maxsize == 4
    assert adapter._pool_block is True


def test_refresh_version_accepts_mismatched_upstream_region_marker(monkeypatch):
    """There is no verified mapping between ``pjsk_region`` and the optional
    upstream Strapi ``region``/``regionCode`` markers, so ``refresh_version``
    must not reject a payload whose markers disagree with the configured region.
    """
    good_version = {"appVersion": "1", "dataVersion": "1", "assetVersion": "1"}
    payload = {
        "region": "kr",
        "regionCode": "kr",
        "eventJson": {
            "id": 12,
            "eventType": "marathon",
            "startAt": 0,
            "closedAt": 2_000_000,
            "rankingAnnounceAt": 1_100_000,
            "aggregateAt": 1_000_000,
        },
    }

    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = payload
    monkeypatch.setattr(
        event_tracker._external_session, "get", Mock(return_value=response)
    )
    monkeypatch.setattr(
        event_tracker.jsonrpc_client, "request", Mock(return_value=good_version)
    )
    monkeypatch.setattr(event_tracker, "pjsk_region", "jp")

    # A conflicting upstream region marker must NOT cause rejection.
    result = event_tracker.refresh_version()
    assert result == good_version
    assert event_tracker.event_data is not None
    assert event_tracker.event_data["id"] == 12
