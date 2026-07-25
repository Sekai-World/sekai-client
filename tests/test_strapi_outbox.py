"""Focused tests for Strapi outbox delivery concurrency semantics."""

import threading
from unittest.mock import Mock

import requests

from utils.strapi_outbox import StrapiOutbox


def _successful_response() -> Mock:
    response = Mock()
    response.raise_for_status.return_value = None
    return response


def test_drain_does_not_hold_lock_during_http_and_preserves_changed_record(tmp_path):
    outbox = StrapiOutbox(str(tmp_path / "outbox.json"))
    outbox.enqueue("cards/fromDB", [501], transaction_id="txn-ready")
    assert outbox.mark_transaction_ready("txn-ready") == 1

    def post(url, **kwargs):
        outbox.enqueue("cards/fromDB", [501], transaction_id="txn-new")
        return _successful_response()

    result = outbox.drain(base_url="http://strapi:3000", token="SECRET", post=post)
    assert result == {"sent": 1, "failed": 0, "retained": 1}
    assert outbox.pending_count() == 1


def test_drain_failure_updates_only_the_unchanged_ready_record(tmp_path):
    outbox = StrapiOutbox(str(tmp_path / "outbox.json"))
    outbox.enqueue("cards/fromDB", [501], transaction_id="txn-ready")
    assert outbox.mark_transaction_ready("txn-ready") == 1

    def post(url, **kwargs):
        outbox.enqueue("cards/fromDB", [501], transaction_id="txn-new")
        raise requests.RequestException("Authorization: Bearer SECRET failed")

    result = outbox.drain(base_url="http://strapi:3000", token="SECRET", post=post)
    assert result == {"sent": 0, "failed": 1, "retained": 1}
    assert outbox.pending_count() == 1


def test_concurrent_drainers_claim_a_record_once(tmp_path):
    outbox = StrapiOutbox(str(tmp_path / "outbox.json"))
    outbox.enqueue("cards/fromDB", [501], transaction_id="txn-ready")
    outbox.mark_transaction_ready("txn-ready")
    started = threading.Event()
    release = threading.Event()
    posts = []

    def post(url, **kwargs):
        posts.append(url)
        started.set()
        assert release.wait(5)
        return _successful_response()

    results = []

    def drain():
        results.append(
            outbox.drain(base_url="http://strapi:3000", token="SECRET", post=post)
        )

    first = threading.Thread(target=drain)
    second = threading.Thread(target=drain)
    first.start()
    assert started.wait(5)
    second.start()
    release.set()
    first.join(5)
    second.join(5)
    assert posts == ["http://strapi:3000/cards/fromDB"]
    assert sorted(result["sent"] for result in results) == [0, 1]
    assert outbox.pending_count() == 0


def test_expired_claim_is_retried(tmp_path):
    outbox = StrapiOutbox(str(tmp_path / "outbox.json"))
    outbox.enqueue("cards/fromDB", [501], transaction_id="txn-ready")
    outbox.mark_transaction_ready("txn-ready")
    import ujson

    with open(outbox.file_path, encoding="utf-8") as stream:
        data = ujson.load(stream)
    record = next(iter(data["records"].values()))
    record["claim_id"] = "crashed"
    record["lease_until"] = 0
    with open(outbox.file_path, "w", encoding="utf-8") as stream:
        ujson.dump(data, stream)
    post = Mock(return_value=_successful_response())
    assert outbox.drain(base_url="http://strapi:3000", token="SECRET", post=post) == {
        "sent": 1,
        "failed": 0,
        "retained": 0,
    }
