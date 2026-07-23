"""Integration tests for cross-process ``flock`` behavior in ``utils.git_lock``.

These tests deliberately use ``multiprocessing`` with the **spawn** start method
(rather than two file descriptors in a single process) so that the locks are
exercised as genuinely separate open-file descriptions in separate processes --
which is the real-world contention scenario Phase 4.2 cares about.

No production code (``check_update.py`` / cycle tests) is touched here.
"""

import multiprocessing
import os
import sys
import tempfile

import pytest

# Project root so spawned children can ``import utils.git_lock`` (spawn re-execs
# Python without pytest's injected sys.path).
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _spawn_ctx():
    return multiprocessing.get_context("spawn")


def _ensure_importable():
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)


# --------------------------------------------------------------------------- #
# Worker processes (must be top-level / picklable for spawn)
# --------------------------------------------------------------------------- #
def _hold_and_wait(paths, ready, release, result):
    """Acquire ``paths``, signal ``ready``, then block until ``release``."""
    _ensure_importable()
    from utils.git_lock import repo_file_locks

    try:
        with repo_file_locks(paths):
            ready.set()
            release.wait(timeout=30)
        result.put(("ok", None))
    except Exception as exc:  # noqa: BLE001 - report to parent
        result.put(("err", repr(exc)))


def _hold_and_barrier(paths, ready, proceed, result):
    """Acquire ``paths``, signal ``ready``, then wait on a barrier-like event."""
    _ensure_importable()
    from utils.git_lock import repo_file_locks

    try:
        with repo_file_locks(paths):
            ready.set()
            # Only proceed once the parent confirms BOTH holders are ready,
            # proving the two locks are held simultaneously.
            proceed.wait(timeout=30)
        result.put(("ok", paths[0]))
    except Exception as exc:  # noqa: BLE001
        result.put(("err", repr(exc)))


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_second_process_gets_repo_lock_unavailable_then_acquires():
    """A second process cannot acquire a lock held by another process.

    After the holder releases, the same (or another) process may acquire it.
    """
    _ensure_importable()
    from utils.git_lock import RepoLockUnavailable, repo_file_locks

    with tempfile.TemporaryDirectory() as tmp:
        lock = os.path.join(tmp, "repo.lock")

        ctx = _spawn_ctx()
        ready = ctx.Event()
        release = ctx.Event()
        result = ctx.Queue()

        holder = ctx.Process(
            target=_hold_and_wait, args=([lock], ready, release, result)
        )
        holder.start()
        assert ready.wait(timeout=30), "holder never acquired the lock"

        # Second process (the test's own process) must be denied.
        with pytest.raises(RepoLockUnavailable) as exc_info:
            with repo_file_locks([lock]):
                pytest.fail("should not have acquired a held lock")
        assert isinstance(exc_info.value.__cause__, OSError)

        # Release the holder and confirm a fresh acquisition succeeds.
        release.set()
        holder.join(timeout=30)
        assert result.get(timeout=30) == ("ok", None)

        with repo_file_locks([lock]):
            pass  # acquired cleanly after release


def test_different_paths_held_concurrently_by_two_processes():
    """Two processes holding *different* paths can overlap in time."""
    with tempfile.TemporaryDirectory() as tmp:
        lock_a = os.path.join(tmp, "repo_a.lock")
        lock_b = os.path.join(tmp, "repo_b.lock")

        ctx = _spawn_ctx()
        ready_a = ctx.Event()
        ready_b = ctx.Event()
        proceed = ctx.Event()
        result = ctx.Queue()

        proc_a = ctx.Process(
            target=_hold_and_barrier,
            args=([lock_a], ready_a, proceed, result),
        )
        proc_b = ctx.Process(
            target=_hold_and_barrier,
            args=([lock_b], ready_b, proceed, result),
        )
        proc_a.start()
        proc_b.start()

        # Wait until BOTH processes have acquired their distinct lock.
        assert ready_a.wait(timeout=30) and ready_b.wait(timeout=30)
        # If we reached here, both locks are held simultaneously. Allow release.
        proceed.set()

        proc_a.join(timeout=30)
        proc_b.join(timeout=30)
        assert proc_a.exitcode == 0
        assert proc_b.exitcode == 0

        seen = {result.get(timeout=30) for _ in range(2)}
        assert seen == {("ok", lock_a), ("ok", lock_b)}


def test_partial_acquisition_releases_already_acquired_locks():
    """When one of several locks fails, the already-acquired ones are freed."""
    _ensure_importable()
    from utils.git_lock import RepoLockUnavailable, repo_file_locks

    with tempfile.TemporaryDirectory() as tmp:
        p1 = os.path.join(tmp, "p1.lock")
        p2 = os.path.join(tmp, "p2.lock")  # held by the child (middle, sorted)
        p3 = os.path.join(tmp, "p3.lock")

        ctx = _spawn_ctx()
        ready = ctx.Event()
        release = ctx.Event()
        result = ctx.Queue()

        holder = ctx.Process(target=_hold_and_wait, args=([p2], ready, release, result))
        holder.start()
        assert ready.wait(timeout=30), "child never acquired p2"

        # Parent tries to acquire all three; p1 acquires, p2 is held -> failure.
        with pytest.raises(RepoLockUnavailable):
            with repo_file_locks([p1, p2, p3]):
                pytest.fail("should not acquire while p2 is held")

        # p1 must have been released by the failing context's ``finally``:
        # re-acquiring p1 alone now succeeds (the child still holds p2).
        with repo_file_locks([p1]):
            pass

        # Once the holder releases, the full set is acquirable.
        release.set()
        holder.join(timeout=30)
        assert result.get(timeout=30) == ("ok", None)
        with repo_file_locks([p1, p2, p3]):
            pass


def test_body_runtime_error_propagates_not_wrapped():
    """A body exception is propagated unchanged (not as RepoLockUnavailable)."""
    _ensure_importable()
    from utils.git_lock import RepoLockUnavailable, repo_file_locks

    with tempfile.TemporaryDirectory() as tmp:
        lock = os.path.join(tmp, "repo.lock")

        with pytest.raises(RuntimeError) as exc_info:
            with repo_file_locks([lock]):
                raise RuntimeError("boom in body")
        # It must be the original RuntimeError, NOT the lock-unavailable type.
        assert not isinstance(exc_info.value, RepoLockUnavailable)
        assert exc_info.value.args == ("boom in body",)

        # After the (failed) body exits, the lock is free again.
        with repo_file_locks([lock]):
            pass


def test_sorted_lock_paths_dedup_and_order():
    """``sorted_lock_paths`` normalizes, de-duplicates, and sorts by realpath."""
    _ensure_importable()
    from utils.git_lock import sorted_lock_paths

    with tempfile.TemporaryDirectory() as tmp:
        a = os.path.join(tmp, "a.lock")
        b = os.path.join(tmp, "b.lock")
        c = os.path.join(tmp, "c.lock")
        open(a, "w").close()
        open(b, "w").close()
        open(c, "w").close()

        # Different spellings of the same file (relative segments) dedupe.
        dupes = [a, os.path.join(tmp, "sub", "..", "a.lock"), b, c]
        out = sorted_lock_paths(dupes)
        assert out == [os.path.realpath(a), os.path.realpath(b), os.path.realpath(c)]

        # Already-canonical, sorted, de-duplicated order is preserved.
        out2 = sorted_lock_paths([c, a, b, a])
        assert out2 == [os.path.realpath(a), os.path.realpath(b), os.path.realpath(c)]

        # Symlinked spelling resolves to the same canonical realpath.
        link = os.path.join(tmp, "link_a.lock")
        os.symlink(a, link)
        out3 = sorted_lock_paths([a, link])
        assert out3 == [os.path.realpath(a)]
