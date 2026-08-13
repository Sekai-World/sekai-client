"""
Background worker thread for processing queued jobs.

Provides a daemon thread that processes (job, response_queue) tuples
from a shared job queue. Results are sent back to per-request queues
with timeout handling to avoid deadlocks.
"""

import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from time import monotonic
from typing import Any

from utils.deadline import (
    Deadline,
    DeadlineExceeded,
    reset_current_deadline,
    set_current_deadline,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QueuedJob:
    job: Callable[[], Any]
    response_queue: queue.Queue[Any]
    deadline: Deadline
    enqueued_at: float


type QueueEntry = QueuedJob | tuple[Callable[[], Any], queue.Queue[Any]]


@dataclass
class QueueMetrics:
    accepted_total: int = 0
    rejected_total: int = 0
    timed_out_total: int = 0
    expired_before_start_total: int = 0
    completed_total: int = 0
    queue_wait_seconds_total: float = 0.0
    execution_seconds_total: float = 0.0


job_queue: queue.Queue[QueueEntry] = queue.Queue(maxsize=1)
"""Queue of (callable_job, response_queue) tuples to be processed."""

_worker_thread: threading.Thread | None = None
_start_lock = threading.Lock()
_metrics_lock = threading.Lock()
_metrics = QueueMetrics()


def record_accepted() -> None:
    with _metrics_lock:
        _metrics.accepted_total += 1


def record_rejected() -> None:
    with _metrics_lock:
        _metrics.rejected_total += 1


def record_timed_out() -> None:
    with _metrics_lock:
        _metrics.timed_out_total += 1


def metrics_snapshot() -> dict[str, int | float]:
    with _metrics_lock:
        return {
            "depth": job_queue.qsize(),
            "capacity": job_queue.maxsize,
            "accepted_total": _metrics.accepted_total,
            "rejected_total": _metrics.rejected_total,
            "timed_out_total": _metrics.timed_out_total,
            "expired_before_start_total": _metrics.expired_before_start_total,
            "completed_total": _metrics.completed_total,
            "queue_wait_seconds_total": _metrics.queue_wait_seconds_total,
            "execution_seconds_total": _metrics.execution_seconds_total,
        }


def _run_queued_job(item: QueuedJob) -> Any:
    started_at = monotonic()
    with _metrics_lock:
        _metrics.queue_wait_seconds_total += max(0.0, started_at - item.enqueued_at)
    token = set_current_deadline(item.deadline)
    try:
        try:
            item.deadline.require_remaining()
        except DeadlineExceeded:
            with _metrics_lock:
                _metrics.expired_before_start_total += 1
            raise
        execution_started = monotonic()
        try:
            return item.job()
        finally:
            with _metrics_lock:
                _metrics.execution_seconds_total += max(
                    0.0, monotonic() - execution_started
                )
    finally:
        reset_current_deadline(token)


def worker() -> None:
    """
    Background worker that processes jobs from the job queue.

    Continuously pulls (job, response_queue) tuples from the global job_queue,
    executes the job, and sends the result (or exception) back to the
    response_queue. Handles timeouts gracefully by dropping stale results
    if the caller has already abandoned the response_queue.

    This function runs as a daemon thread and will never return.
    """
    while True:
        entry = job_queue.get()
        job: Callable[[], Any]
        if isinstance(entry, QueuedJob):
            job = partial(_run_queued_job, entry)
            response_queue = entry.response_queue
        else:
            job, response_queue = entry
        logger.debug("Working on %s", job)
        try:
            res: Any = job()
        except Exception as e:
            res = e
        logger.debug("Finished %s", job)
        try:
            response_queue.put(res, timeout=1)
        except queue.Full:
            logger.warning("Dropping stale worker result because caller timed out")
        finally:
            if isinstance(entry, QueuedJob):
                with _metrics_lock:
                    _metrics.completed_total += 1
            job_queue.task_done()


def start_worker() -> None:
    """Start the worker daemon thread once, lazily and thread-safely."""
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return

    with _start_lock:
        if _worker_thread and _worker_thread.is_alive():
            return

        _worker_thread = threading.Thread(target=worker, daemon=True)
        _worker_thread.start()
