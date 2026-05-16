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
from typing import Any

logger = logging.getLogger(__name__)

# Global job queue for enqueueing work
job_queue: queue.Queue[tuple[Callable[[], Any], queue.Queue[Any]]] = queue.Queue(
    maxsize=1
)
"""Queue of (callable_job, response_queue) tuples to be processed"""

_worker_thread: threading.Thread | None = None
_start_lock = threading.Lock()


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
        job, response_queue = job_queue.get()
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
            job_queue.task_done()


def start_worker() -> None:
    """Start the worker daemon thread once.

    Call this from application entrypoints before enqueueing jobs.
    """
    global _worker_thread
    with _start_lock:
        if _worker_thread and _worker_thread.is_alive():
            return

        _worker_thread = threading.Thread(target=worker, daemon=True)
        _worker_thread.start()
