"""Phase 4.2 acceptance evidence: real cross-process update-cycle locking.

These tests exercise the *real* ``check_update._run_update_cycle`` outer wrapper
(and its real inner ``_run_update_cycle_locked`` body) under genuine
cross-process ``fcntl.flock`` contention, using ``multiprocessing`` with the
**spawn** start method (separate processes == separate open-file descriptions,
the real contention scenario).

The lock *helpers* (``ProcessCycleLock``, ``repo_file_locks``) and the outer
wrapper itself are never mocked. Only the cycle *body* / *prepare* callbacks are
monkeypatched, so no network or real repository mutation occurs. The repository
lock file paths are redirected to a temporary directory per test.

Covers the four acceptance lanes:

  1. A child process that truly holds the master flock makes the parent's real
     ``_run_update_cycle`` return ``skipped:repo_lock``; ``_run_update_cycle_locked``
     / ``prepare_repo_for_update`` never run; afterward the process lock is free
     again and a subsequent call runs to completion.
  2. With master + i18n enabled, during the first *real* ``prepare`` callback both
     real file locks are held by the current process (a separate spawn process
     trying each independently is denied on both), proving prepare runs *after*
     the locks are acquired.
  3. A body ``RuntimeError`` propagates through the real outer wrapper (it is NOT
     reported as ``skipped``), and afterwards both the process lock and the two
     file locks are re-acquirable.
  4. A partial multi-lock acquisition failure returns ``skipped`` via the real
     outer wrapper; the already-acquired lock is then free for another process,
     and the process lock is released.

No production code (``check_update.py`` or other tests) is modified.
"""

import multiprocessing
import os
import sys

import pytest

import check_update as cu
from utils.git_lock import repo_file_locks
from utils.update_transaction import (
    RepoState,
    TransactionJournal,
    TxnPhase,
    new_transaction_id,
    staging_dir_for,
)

# Project root so spawned children can ``import utils.git_lock`` (spawn re-execs
# Python without pytest's injected sys.path).
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _spawn_ctx():
    return multiprocessing.get_context("spawn")


def _ensure_importable():
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)


# --------------------------------------------------------------------------- #
# Top-level worker processes (must be picklable / importable for spawn)
# --------------------------------------------------------------------------- #
def _hold_lock_while_waiting(lock_path, ready, release, result):
    """Acquire ``lock_path``, signal ``ready``, then block until ``release``."""
    _ensure_importable()
    from utils.git_lock import repo_file_locks

    try:
        with repo_file_locks([lock_path], non_blocking=True):
            ready.set()
            release.wait(timeout=30)
        result.put(("ok", None))
    except Exception as exc:  # noqa: BLE001 - report to parent
        result.put(("err", repr(exc)))


def _try_acquire_each_independently(lock_paths, result):
    """Try to acquire each path *independently*; report acquired/denied per path.

    This proves which of the candidate locks is currently held by another
    process, without the multi-lock helper short-circuiting on the first.
    """
    _ensure_importable()
    from utils.git_lock import RepoLockUnavailable, repo_file_locks

    outcomes = []
    for p in lock_paths:
        try:
            with repo_file_locks([p], non_blocking=True):
                outcomes.append((p, "acquired"))
        except RepoLockUnavailable:
            outcomes.append((p, "denied"))
        except Exception as exc:  # noqa: BLE001
            outcomes.append((p, f"err:{exc!r}"))
    result.put(outcomes)


def _try_acquire_one(lock_path, result):
    """Try to acquire a single lock; report acquired/denied."""
    _ensure_importable()
    from utils.git_lock import repo_file_locks

    try:
        with repo_file_locks([lock_path], non_blocking=True):
            result.put(("acquired", lock_path))
    except Exception:  # noqa: BLE001
        result.put(("denied", lock_path))


# --------------------------------------------------------------------------- #
# Local helpers (run in the parent test process)
# --------------------------------------------------------------------------- #
def _prepare_ok():
    return cu.GitResult(outcome=cu.GitOutcome.OK, reason="equal", operation="prepare")


def _stub_gate(monkeypatch):
    """Stub the JSONRPC client so the in-cycle gate always proceeds (no server)."""

    def _request(method, params=None):
        if method in ("check_versions", "check_versions_simple"):
            return {"maintenance": False, "new_version": True}
        return {}

    monkeypatch.setattr(cu.jsonrpc_client, "request", _request)


def _lock_paths(tmp_path):
    """Return (master_folder, i18n_folder, master_lock, i18n_lock) for tmp."""
    master_folder = str(tmp_path / "masterDBDiff")
    i18n_folder = str(tmp_path / "i18n")
    return (
        master_folder,
        i18n_folder,
        master_folder + ".lock",
        i18n_folder + ".lock",
    )


def _release_process_lock_if_held():
    """Guarantee a clean in-process lock state between sub-calls/tests."""
    if cu._PROCESS_LOCK.acquire():
        cu._PROCESS_LOCK.release()


# --------------------------------------------------------------------------- #
# Lane 1: held master flock -> outer wrapper returns skipped:repo_lock
# --------------------------------------------------------------------------- #
def test_held_master_lock_returns_skipped_repo_lock_then_runs(monkeypatch, tmp_path):
    """A child truly holding the master flock makes the real outer wrapper skip,
    without running the locked body; afterward the cycle can run normally."""
    _release_process_lock_if_held()

    master_folder, _i18n_folder, master_lock, _i18n_lock = _lock_paths(tmp_path)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_folder)
    monkeypatch.setattr(cu, "i18n_diff_folder_path", _i18n_folder)
    # i18n disabled -> only the master flock is part of the cycle's lock set.
    monkeypatch.setattr(
        cu, "update_options", {"master": True, "i18n": False, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")

    # Isolate the body so no network/repo mutation happens on a successful run.
    prepare_calls = []
    monkeypatch.setattr(
        cu,
        "prepare_repo_for_update",
        lambda repo, branch="main", allow_push=True: (
            prepare_calls.append(repo) or _prepare_ok()
        ),
    )
    monkeypatch.setattr(cu, "_generate_and_publish", lambda daily, **kwargs: {})
    monkeypatch.setattr(cu, "_commit_enabled_repositories", lambda *a: {})
    monkeypatch.setattr(cu, "_push_enabled_repositories", lambda *a: None)
    _stub_gate(monkeypatch)

    # Child process acquires the master flock and holds it.
    ctx = _spawn_ctx()
    ready = ctx.Event()
    release = ctx.Event()
    result = ctx.Queue()
    holder = ctx.Process(
        target=_hold_lock_while_waiting, args=(master_lock, ready, release, result)
    )
    holder.start()
    assert ready.wait(timeout=30), "child never acquired the master lock"

    # Real outer wrapper: must skip because the master flock is held elsewhere.
    status1 = cu._run_update_cycle(daily=False)
    assert status1 == "skipped:repo_lock"

    # The locked body / prepare must NOT have executed.
    assert prepare_calls == []

    # The in-process cycle lock is released on this skip path.
    assert cu._PROCESS_LOCK.acquire() is True
    cu._PROCESS_LOCK.release()

    # Release the holder and confirm the next call runs to completion.
    release.set()
    holder.join(timeout=30)
    assert result.get(timeout=30) == ("ok", None)

    status2 = cu._run_update_cycle(daily=False)
    assert status2 == "ok"
    # Now the body really executed (prepare ran against the master repo slot).
    assert prepare_calls  # non-empty -> prepare_repo_for_update was invoked


# --------------------------------------------------------------------------- #
# Lane 2: master+i18n -> both real locks held during first prepare
# --------------------------------------------------------------------------- #
def test_both_real_locks_held_during_first_prepare(monkeypatch, tmp_path):
    """With master+i18n enabled, during the first real prepare callback both file
    locks are held by the current process (a separate spawn process is denied on
    both), proving prepare runs after the locks are acquired."""
    _release_process_lock_if_held()

    master_folder, i18n_folder, master_lock, i18n_lock = _lock_paths(tmp_path)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_folder)
    monkeypatch.setattr(cu, "i18n_diff_folder_path", i18n_folder)
    monkeypatch.setattr(
        cu, "update_options", {"master": True, "i18n": True, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    _stub_gate(monkeypatch)

    # Stub the rest of the body so the cycle completes after the prepare probe.
    monkeypatch.setattr(cu, "_generate_and_publish", lambda daily, **kwargs: {})
    monkeypatch.setattr(cu, "_commit_enabled_repositories", lambda *a: {})
    monkeypatch.setattr(cu, "_push_enabled_repositories", lambda *a: None)

    lock_paths = [master_lock, i18n_lock]
    first_probe = {"done": False}

    def _fake_prepare(repo, branch="main", allow_push=True):
        # Only probe on the very first (master) prepare call.
        if not first_probe["done"]:
            first_probe["done"] = True
            ctx = _spawn_ctx()
            q = ctx.Queue()
            child = ctx.Process(
                target=_try_acquire_each_independently, args=(lock_paths, q)
            )
            child.start()
            outcomes = q.get(timeout=30)
            child.join(timeout=30)
            statuses = {os.path.realpath(p): s for p, s in outcomes}
            # Both real locks must be held by THIS process (the spawn child is
            # denied on each independently), proving prepare runs after locking.
            assert statuses[os.path.realpath(master_lock)] == "denied"
            assert statuses[os.path.realpath(i18n_lock)] == "denied"
        return _prepare_ok()

    monkeypatch.setattr(cu, "prepare_repo_for_update", _fake_prepare)

    # Real outer wrapper -> acquires both file locks, runs the locked body, and
    # the first prepare callback observes both locks already held.
    status = cu._run_update_cycle(daily=False)
    assert status == "ok"
    assert first_probe["done"] is True


# --------------------------------------------------------------------------- #
# Lane 3: body RuntimeError propagates (not skipped); locks re-acquirable
# --------------------------------------------------------------------------- #
def test_body_runtime_error_propagates_not_skipped(monkeypatch, tmp_path):
    """A body ``RuntimeError`` propagates through the real outer wrapper (it is
    NOT a skip), and afterward both the process lock and the two file locks can
    be re-acquired."""
    _release_process_lock_if_held()

    master_folder, i18n_folder, master_lock, i18n_lock = _lock_paths(tmp_path)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_folder)
    monkeypatch.setattr(cu, "i18n_diff_folder_path", i18n_folder)
    monkeypatch.setattr(
        cu, "update_options", {"master": True, "i18n": True, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    _stub_gate(monkeypatch)

    # The RuntimeError must be raised by the prepare step (NOT inside
    # ``_generate_and_publish``, which would be caught and reported as
    # ``generation_failed``). A body exception is meant to propagate unchanged.
    def _boom_prepare(repo, branch="main", allow_push=True):
        raise RuntimeError("boom in cycle body")

    monkeypatch.setattr(cu, "prepare_repo_for_update", _boom_prepare)

    # The real outer wrapper must let the RuntimeError escape (not return a skip).
    with pytest.raises(RuntimeError) as exc_info:
        cu._run_update_cycle(daily=False)
    assert exc_info.value.args == ("boom in cycle body",)

    # The in-process cycle lock was released on the exception path.
    assert cu._PROCESS_LOCK.acquire() is True
    cu._PROCESS_LOCK.release()

    # Both file locks are free again: a fresh process can acquire either.
    with repo_file_locks([master_lock, i18n_lock], non_blocking=True):
        pass  # acquired cleanly after the failed cycle


# --------------------------------------------------------------------------- #
# Lane 4: partial multi-lock failure -> skip; acquired lock freed for another proc
# --------------------------------------------------------------------------- #
def test_partial_lock_failure_skips_and_frees_acquired_lock(monkeypatch, tmp_path):
    """When one of several file locks is held by another process, the real outer
    wrapper returns ``skipped:repo_lock``; the lock that was acquired first is
    released (another process may take it), and the process lock is freed."""
    _release_process_lock_if_held()

    master_folder, i18n_folder, master_lock, i18n_lock = _lock_paths(tmp_path)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_folder)
    monkeypatch.setattr(cu, "i18n_diff_folder_path", i18n_folder)
    monkeypatch.setattr(
        cu, "update_options", {"master": True, "i18n": True, "userInfo": False}
    )

    # Determine the deterministic acquisition order so we hold the LAST-acquired
    # lock; the first-acquired one then becomes the "already-acquired -> freed"
    # lock that another process can grab afterward.
    from utils.git_lock import sorted_lock_paths

    ordered = sorted_lock_paths([master_lock, i18n_lock])
    held_by_child = ordered[-1]
    released_one = ordered[0]

    # Child process holds one lock and blocks.
    ctx = _spawn_ctx()
    ready = ctx.Event()
    release = ctx.Event()
    result = ctx.Queue()
    holder = ctx.Process(
        target=_hold_lock_while_waiting, args=(held_by_child, ready, release, result)
    )
    holder.start()
    assert ready.wait(timeout=30), "child never acquired its lock"

    # Real outer wrapper: acquires released_one, then fails on held_by_child.
    status = cu._run_update_cycle(daily=False)
    assert status == "skipped:repo_lock"

    # The in-process cycle lock is released on the skip path.
    assert cu._PROCESS_LOCK.acquire() is True
    cu._PROCESS_LOCK.release()

    # The lock that was acquired-then-released must be free for another process.
    q = ctx.Queue()
    acquirer = ctx.Process(target=_try_acquire_one, args=(released_one, q))
    acquirer.start()
    acquirer.join(timeout=30)
    assert q.get(timeout=30) == ("acquired", released_one)

    # Release the original holder and confirm full acquisition is then possible.
    release.set()
    holder.join(timeout=30)
    assert result.get(timeout=30) == ("ok", None)
    with repo_file_locks([master_lock, i18n_lock], non_blocking=True):
        pass  # both free now


def test_recovery_journal_adds_lock_for_now_disabled_i18n(monkeypatch, tmp_path):
    """Recovery locks journal-enabled repos even after options disable i18n."""
    master_folder, i18n_folder, master_lock, i18n_lock = _lock_paths(tmp_path)
    master_repo = cu.Repo.init(master_folder)
    i18n_repo = cu.Repo.init(i18n_folder)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_folder)
    monkeypatch.setattr(cu, "i18n_diff_folder_path", i18n_folder)
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "i18n_diff_repo", i18n_repo)
    monkeypatch.setattr(
        cu, "update_options", {"master": True, "i18n": False, "userInfo": False}
    )

    txn_id = new_transaction_id()
    repos = {
        "master": RepoState(
            staging_dir=staging_dir_for(master_folder, txn_id),
            repo_root=os.path.realpath(master_folder),
        ),
        "i18n": RepoState(
            staging_dir=staging_dir_for(i18n_folder, txn_id),
            repo_root=os.path.realpath(i18n_folder),
        ),
    }
    # This test exercises lock selection only; the journal need not contain a
    # publishable file set because no recovery body is entered here.
    journal = TransactionJournal(
        master_git_dir=master_repo.git_dir,
        transaction_id=txn_id,
        candidate={},
        enabled_repos=["master", "i18n"],
        publish_order=["master", "i18n"],
        repos=repos,
        phase=TxnPhase.PUBLISHING,
    )
    journal.write()

    selected = {os.path.realpath(p) for p in cu._cycle_lock_paths()}
    assert os.path.realpath(master_lock) in selected
    assert os.path.realpath(i18n_lock) in selected
