import sqlite3
import time

import pytest

import utils.event_outbox as event_outbox_module
from utils.event_outbox import EventRankingOutbox


def _enqueue(outbox: EventRankingOutbox) -> str:
    return outbox.enqueue(
        region="tw",
        event_id=123,
        collected_at=456,
        data_type="ranking",
        endpoint="https://api.example.test/rankings",
        payload={"time": 456, "first100": []},
    )


def test_enqueue_is_idempotent_and_file_is_private(tmp_path):
    path = tmp_path / "state" / "outbox.sqlite3"
    outbox = EventRankingOutbox(str(path))

    assert _enqueue(outbox) == _enqueue(outbox)
    assert outbox.metrics().pending == 1
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"limit": -1}, "limit must be non-negative"),
        ({"lease_seconds": 0}, "lease_seconds must be positive"),
        ({"max_attempts": 0}, "max_attempts must be positive"),
        ({"max_duration_seconds": 0}, "max_duration_seconds must be positive"),
    ],
)
def test_drain_validates_options_before_touching_outbox(tmp_path, kwargs, message):
    outbox = EventRankingOutbox(str(tmp_path / "outbox.sqlite3"))
    _enqueue(outbox)

    with pytest.raises(ValueError, match=message):
        outbox.drain(lambda *_: None, **kwargs)

    assert outbox.metrics().pending == 1


def test_terminal_retention_index_is_created(tmp_path):
    outbox = EventRankingOutbox(str(tmp_path / "outbox.sqlite3"))

    with sqlite3.connect(outbox.path) as connection:
        indexes = {
            row[1]
            for row in connection.execute("PRAGMA index_list(event_ranking_outbox)")
        }

    assert "event_ranking_outbox_terminal" in indexes


def test_successful_delivery_is_acknowledged(tmp_path):
    outbox = EventRankingOutbox(str(tmp_path / "outbox.sqlite3"))
    key = _enqueue(outbox)
    delivered = []

    result = outbox.drain(
        lambda endpoint, payload, idempotency_key: delivered.append(
            (endpoint, payload, idempotency_key)
        )
    )

    assert result == {"sent": 1, "failed": 0, "retained": 0}
    assert delivered == [
        ("https://api.example.test/rankings", {"first100": [], "time": 456}, key)
    ]
    assert outbox.metrics().pending == 0


def test_transient_failure_survives_restart_and_retries(tmp_path):
    path = tmp_path / "outbox.sqlite3"
    outbox = EventRankingOutbox(str(path), retry_base_seconds=0.01)
    _enqueue(outbox)

    result = outbox.drain(
        lambda *_: (_ for _ in ()).throw(ConnectionError("temporary"))
    )
    assert result == {"sent": 0, "failed": 0, "retained": 1}
    assert outbox.metrics().pending == 1

    time.sleep(0.02)
    restarted = EventRankingOutbox(str(path), retry_base_seconds=0.01)
    assert restarted.drain(lambda *_: None)["sent"] == 1


def test_expired_sending_claim_is_recovered(tmp_path):
    path = tmp_path / "outbox.sqlite3"
    outbox = EventRankingOutbox(str(path))
    _enqueue(outbox)
    assert outbox._claim(0.01) is not None

    time.sleep(0.02)
    restarted = EventRankingOutbox(str(path))
    assert restarted.drain(lambda *_: None)["sent"] == 1


def test_attempt_limit_moves_record_to_failed(tmp_path):
    path = tmp_path / "outbox.sqlite3"
    outbox = EventRankingOutbox(str(path), retry_base_seconds=0.01)
    _enqueue(outbox)

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE event_ranking_outbox SET attempts=9,next_attempt_at=0"
        )
    result = outbox.drain(lambda *_: (_ for _ in ()).throw(OSError("down")))

    assert result == {"sent": 0, "failed": 1, "retained": 0}
    assert outbox.metrics().failed == 1


def test_distinct_chapters_have_distinct_idempotency_keys(tmp_path):
    outbox = EventRankingOutbox(str(tmp_path / "outbox.sqlite3"))

    first = outbox.enqueue(
        region="en",
        event_id=7,
        collected_at=10,
        data_type="chapter:1",
        endpoint="https://api.example.test/chapters",
        payload={"time": 10},
    )
    second = outbox.enqueue(
        region="en",
        event_id=7,
        collected_at=10,
        data_type="chapter:2",
        endpoint="https://api.example.test/chapters",
        payload={"time": 10},
    )

    assert first != second
    assert outbox.metrics().pending == 2


def test_retention_removes_old_terminal_rows_but_keeps_pending(tmp_path):
    outbox = EventRankingOutbox(str(tmp_path / "outbox.sqlite3"), retention_seconds=60)
    sent = _enqueue(outbox)
    outbox.drain(lambda *_: None)
    pending = outbox.enqueue(
        region="tw",
        event_id=123,
        collected_at=456,
        data_type="chapter:7",
        endpoint="https://api.example.test/chapters",
        payload={"time": 456},
    )
    with sqlite3.connect(outbox.path) as connection:
        connection.execute(
            "UPDATE event_ranking_outbox SET collected_at=0 WHERE idempotency_key=?",
            (sent,),
        )

    assert outbox.prune_terminal() == 1
    with sqlite3.connect(outbox.path) as connection:
        rows = connection.execute(
            "SELECT idempotency_key,status FROM event_ranking_outbox"
        ).fetchall()
    assert rows == [(pending, "pending")]


def test_retention_keeps_recent_terminal_rows(tmp_path):
    outbox = EventRankingOutbox(str(tmp_path / "outbox.sqlite3"), retention_seconds=60)
    key = outbox.enqueue(
        region="tw",
        event_id=123,
        collected_at=int(time.time() * 1000),
        data_type="ranking",
        endpoint="https://api.example.test/rankings",
        payload={"time": 456},
    )
    outbox.drain(lambda *_: None)

    assert outbox.prune_terminal() == 0
    with sqlite3.connect(outbox.path) as connection:
        assert connection.execute(
            "SELECT status FROM event_ranking_outbox WHERE idempotency_key=?",
            (key,),
        ).fetchone() == ("sent",)


def test_drain_duration_budget_stops_before_claiming_more_rows(tmp_path, monkeypatch):
    outbox = EventRankingOutbox(str(tmp_path / "outbox.sqlite3"))
    _enqueue(outbox)
    outbox.enqueue(
        region="tw",
        event_id=124,
        collected_at=457,
        data_type="ranking",
        endpoint="https://api.example.test/rankings",
        payload={"time": 457},
    )

    clock = iter((0.0, 0.0, 0.0005, 0.002))
    monkeypatch.setattr(event_outbox_module.time, "monotonic", lambda: next(clock))

    result = outbox.drain(lambda *_: None, max_duration_seconds=0.001)

    assert result == {"sent": 1, "failed": 0, "retained": 0}
    assert outbox.metrics().pending == 1
