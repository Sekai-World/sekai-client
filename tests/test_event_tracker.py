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
    post = Mock(return_value=response)
    monkeypatch.setattr(event_tracker.requests, "post", post)

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
        timeout=60,
    )
    response.raise_for_status.assert_called_once_with()


def test_drain_reports_distinct_delivery_state(monkeypatch, caplog):
    outbox = Mock()
    outbox.drain.return_value = {"sent": 0, "failed": 0, "retained": 1}
    outbox.metrics.return_value = Mock(
        pending=1, sending=0, failed=0, oldest_pending_age_seconds=4.5
    )
    monkeypatch.setattr(event_tracker, "ranking_outbox", outbox)

    event_tracker._drain_ranking_outbox()

    assert "sent=0 failed=0 retained=1 pending=1" in caplog.text
