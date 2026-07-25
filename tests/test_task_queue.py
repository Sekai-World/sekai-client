"""
Unit tests for background task queue.

Tests job enqueueing, worker execution, and timeout handling.
"""

import queue

import pytest

from config import Config
from utils.task_queue import start_worker


@pytest.fixture(autouse=True, scope="session")
def ensure_worker_started():
    start_worker()


class TestTaskQueue:
    """Tests for the task queue module."""

    def test_job_queue_global_exists(self):
        """Test global job_queue is initialized."""
        from utils.task_queue import job_queue

        assert isinstance(job_queue, queue.Queue)
        assert job_queue.maxsize == 1

    def test_worker_thread_started(self):
        """Test worker thread is running."""
        from utils.task_queue import job_queue

        # Test by putting a job and waiting for it
        result_queue = queue.Queue()

        def test_job():
            return "success"

        job_queue.put((test_job, result_queue), timeout=1)

        try:
            result = result_queue.get(timeout=2)
            assert result == "success"
        except queue.Empty:
            pytest.fail("Worker didn't process job")

    def test_worker_handles_exceptions(self):
        """Test worker returns exceptions instead of raising."""
        from utils.task_queue import job_queue

        result_queue = queue.Queue()

        def failing_job():
            raise ValueError("Test error")

        job_queue.put((failing_job, result_queue), timeout=1)

        try:
            result = result_queue.get(timeout=2)
            assert isinstance(result, ValueError)
            assert str(result) == "Test error"
        except queue.Empty:
            pytest.fail("Worker didn't process job")

    def test_worker_drops_stale_results(self, caplog):
        """Test worker drops results if caller already timed out."""
        import logging

        from utils.task_queue import job_queue

        caplog.set_level(logging.WARNING)
        result_queue = queue.Queue(maxsize=1)

        # Fill the queue so put() will fail
        result_queue.put("stale", block=False)

        def test_job():
            return "result"

        job_queue.put((test_job, result_queue), timeout=1)

        # Wait for the background worker to finish this job.
        job_queue.join()

        assert any(
            "Dropping stale worker result" in record.message
            for record in caplog.records
        )


class TestQueueTimeout:
    """Tests for queue timeout configuration."""

    def test_job_queue_timeout_configured(self):
        """Test JOB_QUEUE_TIMEOUT is configured."""
        assert Config.JOB_QUEUE_TIMEOUT > 0

    def test_answer_queue_timeout_configured(self):
        """Test ANSWER_QUEUE_TIMEOUT is configured."""
        assert Config.ANSWER_QUEUE_TIMEOUT > 0

    def test_timeouts_are_positive(self):
        """Test all timeout values are positive."""
        assert Config.JOB_QUEUE_TIMEOUT > 0
        assert Config.ANSWER_QUEUE_TIMEOUT > 0
        assert Config.REQUEST_TIMEOUT > 0
