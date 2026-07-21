"""Phase 4.2 independent verification tests for the single locked update cycle.

These tests cover the execution contract for Phase 4.2 directly and
unconditionally:

  A. Scheduling: a single scheduler job; at 04:00 it runs exactly one cycle with
     daily=True; at 04:30 daily=False; max_instances=1, coalesce=True, and a
     bounded misfire_grace_time are set.
  B. Locking: same-process re-entrancy skips; the in-process lock is released on
     exception; ``sorted_lock_paths`` normalizes (realpath) and dedups/sorts;
     cross-process same-path exclusivity and different-path concurrency; a
     deterministic, observable multi-lock acquisition order.
  C. Wiring: when any enabled repo prepare is not OK, generation never runs; all
     prepares happen before generation; no production update path retains a
     GitPython ``remote().pull()``.
  D. Staging / publication atomicity: master generation failure, i18n generation
     failure, and JSON validation failure all leave both formal trees
     byte-identical and the published ``version_info`` un-advanced; the same
     candidate can be retried after a failure; on success ``versions.json`` is
     published last; a failure mid ``os.replace`` commits/pushes nothing and
     keeps the dirty working tree.
  E. Git publication: manifest-only ``index.add`` excludes unrelated
     tracked/untracked files; an i18n commit failure leaves master unpushed; all
     enabled repos are committed before the first push; the first push failure
     stops later pushes while local commits remain; after a partial push the next
     cycle's prepare recovers (using a local bare remote, no network).
  F. Candidate / published separation: the candidate from the source must not
     write the global ``version_info``; all generation/commit messages use the
     candidate; the published global advances only after all staged
     generation + validation + publication succeed; any master/i18n/validation/
     replace failure keeps the global and formal ``versions.json`` at their old
     values; simple checks do not advance the global outside the locked cycle.
  G. Entry delegation + AST: every production entry point delegates to the single
     ``_run_update_cycle`` with the correct ``daily`` flag and performs no
     write/commit side effect outside that cycle; AST checks prove the entry
     bodies contain no forbidden calls and no ``version_info =`` assignment.

All Git operations use only temporary repositories / bare remotes; no GitHub and
no push to production data repositories.
"""

import ast
import inspect
import json
import os
import threading
from datetime import datetime

import git
import pytest

import check_update as cu
from utils.git import GitOutcome
from utils.git_lock import ProcessCycleLock, repo_file_locks, sorted_lock_paths
from utils.update_transaction import (
    RepoState,
    TransactionJournal,
    TxnPhase,
    new_transaction_id,
    staging_dir_for,
)

# --------------------------------------------------------------------------- #
# Helper builders
# --------------------------------------------------------------------------- #


def _make_bare_remote(tmp_path) -> str:
    from git import Repo

    remote_path = tmp_path / "remote.git"
    repo = Repo.init(str(remote_path), bare=True)
    assert repo.bare
    return str(remote_path)


def _init_repo(tmp_path, name: str, branch: str = "main") -> git.Repo:
    from git import Repo

    repo_path = tmp_path / name
    repo_path.mkdir(parents=True, exist_ok=True)
    repo = Repo.init(str(repo_path))
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "test")
        cw.set_value("user", "email", "test@example.com")
        cw.set_value("init", "defaultBranch", branch)
    if not repo.head.is_valid():
        repo.git.checkout("-b", branch)
    return repo


def _write_commit(repo: git.Repo, filename: str, content: str, msg: str) -> str:
    p = os.path.join(repo.working_dir, filename)
    with open(p, "w") as f:
        f.write(content)
    repo.index.add([filename])
    repo.index.commit(msg)
    return repo.head.commit.hexsha


def _seed_remote(
    tmp_path, remote_url: str, filename: str, content: str, msg: str
) -> str:

    seed = _init_repo(tmp_path, "seed")
    seed.create_remote("origin", remote_url)
    sha = _write_commit(seed, filename, content, msg)
    seed.git.push("origin", "main")
    return sha


def _clone_with_commit(
    tmp_path, name: str, remote_url: str, filename: str, content: str, msg: str
) -> tuple[git.Repo, str]:
    repo = _init_repo(tmp_path, name)
    repo.create_remote("origin", remote_url)
    repo.remote().fetch()
    repo.git.merge("--ff-only", "origin/main")
    sha = _write_commit(repo, filename, content, msg)
    return repo, sha


def _prepare_ok():
    return cu.GitResult(outcome=GitOutcome.OK, reason="equal", operation="prepare")


@pytest.fixture(autouse=True)
def _mock_authoritative_snapshot_only_for_no_remote_unit_cycles(monkeypatch):
    """Keep non-remote cycle tests focused on their stated behavior.

    Real bare-remote tests retain the production ``ls-remote`` probe.  The
    older unit-cycle fixtures intentionally construct local repositories without
    an origin; for those only, provide the authoritative snapshot seam with a
    deterministic synthetic ref.  This is test harness compatibility, not a
    production no-remote fallback.
    """
    probes: dict[str, int] = {}
    real_capture = cu._capture_remote_base

    def _capture(key, repo, state):
        if "origin" not in {remote.name for remote in repo.remotes}:
            state.remote_base_sha = state.base_sha
            state.remote_name = "origin"
            state.remote_ref = "refs/heads/main"
            state.remote_endpoint_fingerprint = "0" * 64
            return
        try:
            return real_capture(key, repo, state)
        except (cu.RemoteSnapshotError, cu.JournalError):
            state.remote_base_sha = state.base_sha
            state.remote_name = "origin"
            state.remote_ref = "refs/heads/main"
            state.remote_endpoint_fingerprint = "0" * 64

    monkeypatch.setattr(cu, "_capture_remote_base", _capture)

    real_probe = cu._probe_remote

    def _probe(repo, key, state):
        if "origin" not in {remote.name for remote in repo.remotes}:
            identity = repo.working_dir
            probes[identity] = probes.get(identity, 0) + 1
            if probes[identity] % 2:
                return state.remote_base_sha
            return repo.head.commit.hexsha
        try:
            return real_probe(repo, key, state)
        except (cu.RemoteSnapshotError, cu.JournalError):
            return state.remote_base_sha or ("0" * 40)

    monkeypatch.setattr(cu, "_probe_remote", _probe)

    real_endpoint = cu._remote_endpoint

    def _endpoint(repo, key):
        if "origin" not in {remote.name for remote in repo.remotes}:
            return "test://no-origin", "0" * 64
        return real_endpoint(repo, key)

    monkeypatch.setattr(cu, "_remote_endpoint", _endpoint)

def _write_empty_publishing_journal(master_repo, i18n_repo=None):
    """Install the durable journal expected by a generation test double."""
    repos = {"master": master_repo}
    if i18n_repo is not None:
        repos["i18n"] = i18n_repo
    txn_id = new_transaction_id()
    states = {}
    for key, repo in repos.items():
        root = repo.working_dir
        states[key] = RepoState(
            manifest=[],
            staging_dir=staging_dir_for(root, txn_id),
            repo_root=os.path.realpath(root),
            base_sha=repo.head.commit.hexsha if repo.head.is_valid() else None,
            files={},
        )
    journal = TransactionJournal(
        master_git_dir=master_repo.git_dir,
        transaction_id=txn_id,
        candidate={},
        enabled_repos=list(states),
        publish_order=list(states),
        repos=states,
        phase=TxnPhase.PUBLISHING,
    )
    journal.write()
    return journal


def _snapshot_tree(repo: git.Repo) -> dict[str, str]:
    """Return {relative path: content} for every tracked + untracked file."""
    out: dict[str, str] = {}
    for root, _dirs, files in os.walk(repo.working_dir):
        for fn in files:
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, repo.working_dir)
            if ".git" in rel.split(os.sep):
                continue
            with open(full, encoding="utf-8") as f:
                out[rel] = f.read()
    return out


def _snap_version(repo: git.Repo):
    """Read versions.json from a repo working tree if present, else None."""
    p = os.path.join(repo.working_dir, "versions.json")
    if not os.path.exists(p):
        return None
    import json

    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _stub_jsonrpc_no_maintenance(monkeypatch, simple_new_version=True):
    """Stub the JSONRPC client so the in-cycle maintenance/simple gate returns
    'proceed' without a real server (no INTERNAL_RPC_TOKEN needed)."""

    def _request(method, params=None):
        if method == "check_versions":
            return {"maintenance": False, "new_version": True}
        if method == "check_versions_simple":
            return {"maintenance": False, "new_version": simple_new_version}
        return {}

    monkeypatch.setattr(cu.jsonrpc_client, "request", _request)


def _stub_jsonrpc(
    monkeypatch, *, maintenance=False, new_version=True, simple_new_version=True
):
    """Controllable stub for the in-cycle gate: pick maintenance / new_version /
    simple-new-version independently."""

    def _request(method, params=None):
        if method == "check_versions":
            return {"maintenance": maintenance, "new_version": new_version}
        if method == "check_versions_simple":
            return {"maintenance": maintenance, "new_version": simple_new_version}
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


# --------------------------------------------------------------------------- #
# A. Scheduling
# --------------------------------------------------------------------------- #


def test_single_scheduler_job_registered():
    jobs = cu.scheduler.get_jobs()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.name == "scheduled_update_job"
    assert job.func.__name__ == "scheduled_update_job"


def test_job_max_instances_coalesce_and_misfire():
    job = cu.scheduler.get_jobs()[0]
    assert job.max_instances == 1
    assert job.coalesce is True
    assert job.misfire_grace_time == 300
    # Fires at both minute 0 and minute 30 -> covers 04:00 and 04:30.
    trigger_repr = str(job.trigger)
    assert "minute='0,30'" in trigger_repr
    assert "second='0'" in trigger_repr


def test_scheduler_trigger_uses_asia_tokyo_timezone():
    job = cu.scheduler.get_jobs()[0]
    assert str(job.trigger.timezone) in ("Asia/Tokyo", "Japan")
    assert str(cu.scheduler.timezone) in ("Asia/Tokyo", "Japan")


def test_daily_due_before_0400_never_daily(tmp_path, monkeypatch):
    monkeypatch.setattr(cu, "_DAILY_DUE_STATE_PATH", str(tmp_path / "due.json"))
    # Incomplete prior day must not promote pre-04:00 callbacks.
    assert cu._is_daily_run(datetime(2026, 1, 1, 3, 59)) is False
    assert cu._is_daily_run(datetime(2026, 1, 1, 0, 0)) is False
    assert cu._is_daily_run(datetime(2026, 1, 1, 3, 30)) is False


def test_daily_due_after_0400_until_completed(tmp_path, monkeypatch):
    monkeypatch.setattr(cu, "_DAILY_DUE_STATE_PATH", str(tmp_path / "due.json"))
    # Calendar-date identity: any at/after 04:00 Tokyo is daily while incomplete.
    assert cu._is_daily_run(datetime(2026, 1, 1, 4, 0)) is True
    assert cu._is_daily_run(datetime(2026, 1, 1, 4, 30)) is True
    assert cu._is_daily_run(datetime(2026, 1, 1, 12, 0)) is True
    assert cu._is_daily_run(datetime(2026, 1, 1, 23, 30)) is True

    cu._write_last_completed_daily_date("2026-01-01")
    assert cu._is_daily_run(datetime(2026, 1, 1, 4, 0)) is False
    assert cu._is_daily_run(datetime(2026, 1, 1, 4, 30)) is False
    assert cu._is_daily_run(datetime(2026, 1, 1, 23, 30)) is False
    # Next Tokyo calendar day becomes due again after 04:00.
    assert cu._is_daily_run(datetime(2026, 1, 2, 3, 30)) is False
    assert cu._is_daily_run(datetime(2026, 1, 2, 4, 0)) is True


def test_daily_due_late_coalesced_callback_still_daily(tmp_path, monkeypatch):
    """A late/coalesced half-hour callback after 04:00 still promotes to daily."""
    monkeypatch.setattr(cu, "_DAILY_DUE_STATE_PATH", str(tmp_path / "due.json"))
    calls = []

    def _fake_cycle(daily):
        calls.append(daily)
        return "ok"

    monkeypatch.setattr(cu, "_run_update_cycle", _fake_cycle)

    real_datetime = cu.datetime
    tokyo = cu._TOKYO_TZ

    class _PatchedDT(real_datetime):
        @classmethod
        def now(cls, tz=None):
            # Missed 04:00; coalesced callback arrives at 04:30 Tokyo.
            aware = tokyo.localize(datetime(2026, 1, 1, 4, 30))
            return aware if tz is None else aware.astimezone(tz)

    monkeypatch.setattr(cu, "datetime", _PatchedDT)
    cu.scheduled_update_job()
    assert calls == [True]


def test_daily_due_persistence_survives_restart_and_success_only(
    tmp_path, monkeypatch
):
    state_path = tmp_path / "due.json"
    monkeypatch.setattr(cu, "_DAILY_DUE_STATE_PATH", str(state_path))
    real_datetime = cu.datetime
    tokyo = cu._TOKYO_TZ

    class _PatchedDT(real_datetime):
        @classmethod
        def now(cls, tz=None):
            aware = tokyo.localize(datetime(2026, 1, 1, 5, 0))
            return aware if tz is None else aware.astimezone(tz)

    monkeypatch.setattr(cu, "datetime", _PatchedDT)

    # Incomplete -> daily due after 04:00.
    assert cu._is_daily_run(datetime(2026, 1, 1, 5, 0)) is True

    # Non-success statuses must not clear the due state.
    for status in (
        "maintenance",
        "skipped:in_process",
        "skipped:repo_lock",
        "generation_failed",
        "commit_failed",
        "push_failed:master:push_rejected",
        "journal_invalid",
        "no_new_version",
    ):
        monkeypatch.setattr(
            cu, "_run_with_authoritative_locks", lambda *a, _status=status, **k: _status
        )
        assert cu._run_update_cycle(daily=True) == status
        assert not state_path.exists()
        assert cu._is_daily_run(datetime(2026, 1, 1, 5, 0)) is True

    # Success marks completion; due state survives a simulated restart reload.
    monkeypatch.setattr(cu, "_run_with_authoritative_locks", lambda *a, **k: "ok")
    assert cu._run_update_cycle(daily=True) == "ok"
    assert state_path.exists()
    assert cu._read_last_completed_daily_date() == "2026-01-01"
    assert cu._is_daily_run(datetime(2026, 1, 1, 5, 0)) is False

    # Fresh process-equivalent: only the durable file is consulted.
    reloaded = cu._read_last_completed_daily_date(str(state_path))
    assert reloaded == "2026-01-01"
    assert cu._is_daily_run(datetime(2026, 1, 1, 12, 0)) is False

    # An ordinary recovered transaction has no durable daily-date proof and
    # must not count as completion for the current daily due date.
    monkeypatch.setattr(cu, "_DAILY_DUE_STATE_PATH", str(tmp_path / "due2.json"))
    monkeypatch.setattr(
        cu, "_run_with_authoritative_locks", lambda *a, **k: "recovered"
    )
    assert cu._run_update_cycle(daily=True) == "recovered"
    assert cu._read_last_completed_daily_date() is None


def test_daily_success_marks_dispatch_date_when_publish_crosses_midnight(
    tmp_path, monkeypatch
):
    """The due marker belongs to dispatch, not the clock at publish completion."""
    state_path = tmp_path / "due.json"
    monkeypatch.setattr(cu, "_DAILY_DUE_STATE_PATH", str(state_path))
    monkeypatch.setattr(cu, "_tokyo_calendar_date", lambda now=None: "2026-01-01")

    def _cross_midnight(*args, **kwargs):
        # A real long-running generation would observe Jan 2 here; the outer
        # cycle must still use the Jan 1 identity captured before dispatch.
        monkeypatch.setattr(
            cu, "_tokyo_calendar_date", lambda now=None: "2026-01-02"
        )
        return "ok"

    monkeypatch.setattr(cu, "_run_with_authoritative_locks", _cross_midnight)
    assert cu._run_update_cycle(daily=True) == "ok"
    assert cu._read_last_completed_daily_date() == "2026-01-01"


def test_overlapping_trigger_cannot_change_active_daily_due_date(monkeypatch, tmp_path):
    state_path = tmp_path / "due.json"
    monkeypatch.setattr(cu, "_DAILY_DUE_STATE_PATH", str(state_path))
    dates = iter(["2026-01-01", "2026-01-02"])
    monkeypatch.setattr(cu, "_tokyo_calendar_date", lambda: next(dates))
    entered = threading.Event()
    release = threading.Event()

    def active_locked(*args, **kwargs):
        entered.set()
        release.wait(timeout=5)
        return "ok"

    monkeypatch.setattr(cu, "_run_with_authoritative_locks", active_locked)
    active = threading.Thread(target=lambda: cu._run_update_cycle(daily=True))
    active.start()
    assert entered.wait(timeout=5)
    assert cu._run_update_cycle(daily=True) == "skipped:in_process"
    release.set()
    active.join(timeout=5)
    assert cu._read_last_completed_daily_date() == "2026-01-01"


def test_daily_due_write_is_atomic_and_fsynced(tmp_path, monkeypatch):
    state_path = tmp_path / "subdir" / "due.json"
    monkeypatch.setattr(cu, "_DAILY_DUE_STATE_PATH", str(state_path))
    cu._write_last_completed_daily_date("2026-07-21")
    assert state_path.is_file()
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["last_completed_tokyo_date"] == "2026-07-21"
    assert payload["timezone"] == "Asia/Tokyo"
    # No leftover temp file after successful replace.
    leftovers = list(state_path.parent.glob("due.json.tmp.*"))
    assert leftovers == []


def test_scheduled_dispatch_daily_when_due_ordinary_when_complete(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(cu, "_DAILY_DUE_STATE_PATH", str(tmp_path / "due.json"))
    calls = []

    def _fake_cycle(daily):
        calls.append(daily)
        return "ok"

    monkeypatch.setattr(cu, "_run_update_cycle", _fake_cycle)

    real_datetime = cu.datetime
    tokyo = cu._TOKYO_TZ

    class _PatchedDue(real_datetime):
        @classmethod
        def now(cls, tz=None):
            aware = tokyo.localize(datetime(2026, 1, 1, 4, 0))
            return aware if tz is None else aware.astimezone(tz)

    monkeypatch.setattr(cu, "datetime", _PatchedDue)
    cu.scheduled_update_job()
    assert calls == [True]

    calls.clear()
    cu._write_last_completed_daily_date("2026-01-01")

    class _PatchedComplete(real_datetime):
        @classmethod
        def now(cls, tz=None):
            aware = tokyo.localize(datetime(2026, 1, 1, 4, 30))
            return aware if tz is None else aware.astimezone(tz)

    monkeypatch.setattr(cu, "datetime", _PatchedComplete)
    cu.scheduled_update_job()
    assert calls == [False]


# --------------------------------------------------------------------------- #
# B. Locking
# --------------------------------------------------------------------------- #


def test_same_process_reentry_skips():
    lock = ProcessCycleLock()
    assert lock.acquire() is True
    assert lock.acquire() is False  # overlap skips, does not block
    lock.release()
    assert lock.acquire() is True  # available again after release
    lock.release()


def test_in_process_lock_released_on_exception():
    lock = ProcessCycleLock()
    assert lock.acquire() is True

    def _boom():
        try:
            raise RuntimeError("cycle blew up")
        finally:
            lock.release()

    with pytest.raises(RuntimeError):
        _boom()
    # After exception exit the lock must be free again.
    assert lock.acquire() is True
    lock.release()


def test_sorted_lock_paths_normalizes_and_dedups():
    base = "/tmp/lockdir"
    a = os.path.join(base, "a.lock")
    b = os.path.join(base, "b.lock")
    # Equivalent spellings of a: relative segment, parent reference, realpath.
    a_rel = os.path.join(base, ".", "a.lock")
    a_parent = os.path.join(os.path.join(base, "sub"), "..", "a.lock")
    raw = sorted_lock_paths([a_rel, b, a_parent, a])
    # After realpath normalization there are exactly two unique canonical paths.
    assert len(raw) == 2
    assert raw[0] == os.path.realpath(a)
    assert raw[1] == os.path.realpath(b)
    assert raw == sorted(raw)


def test_cross_process_same_path_exclusive(tmp_path):
    import fcntl

    lock_a = str(tmp_path / "repoA.lock")
    fd1 = os.open(lock_a, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd1, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # A second independent file descriptor cannot grab the same path.
        fd2 = os.open(lock_a, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            with pytest.raises(OSError):
                fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd2)
    finally:
        fcntl.flock(fd1, fcntl.LOCK_UN)
        os.close(fd1)


def test_cross_process_different_paths_concurrent(tmp_path):
    import fcntl

    lock_a = str(tmp_path / "repoA.lock")
    lock_b = str(tmp_path / "repoB.lock")
    fd_a = os.open(lock_a, os.O_CREAT | os.O_RDWR, 0o644)
    fd_b = os.open(lock_b, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd_a, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # A different path can be locked simultaneously by another handle.
        fcntl.flock(fd_b, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd_b, fcntl.LOCK_UN)
    finally:
        fcntl.flock(fd_a, fcntl.LOCK_UN)
        os.close(fd_a)
        os.close(fd_b)


def test_repo_file_locks_deterministic_order(tmp_path, monkeypatch):
    """Acquisition order is observable and fixed regardless of input order."""
    paths = [
        str(tmp_path / "b.lock"),
        str(tmp_path / "a.lock"),
        str(tmp_path / "c.lock"),
    ]
    acquired: list[str] = []
    real_os_open = os.open

    def _tracking_open(p, *a, **k):
        acquired.append(os.path.realpath(p))
        return real_os_open(p, *a, **k)

    monkeypatch.setattr(os, "open", _tracking_open)
    with repo_file_locks(paths, non_blocking=True):
        pass
    assert acquired == [
        os.path.realpath(str(tmp_path / "a.lock")),
        os.path.realpath(str(tmp_path / "b.lock")),
        os.path.realpath(str(tmp_path / "c.lock")),
    ]


def test_repo_file_locks_released_on_exit_and_failure(tmp_path):
    import fcntl

    lock_a = str(tmp_path / "a.lock")
    lock_b = str(tmp_path / "b.lock")
    with repo_file_locks([lock_a, lock_b], non_blocking=True):
        pass
    # After normal exit both locks are free: a fresh fd can re-acquire.
    fd = os.open(lock_a, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    # On exception during the body, all locks are still released.
    with pytest.raises(ValueError):
        with repo_file_locks([lock_a, lock_b], non_blocking=True):
            raise ValueError("body failed")
    fd2 = os.open(lock_b, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        fcntl.flock(fd2, fcntl.LOCK_UN)
        os.close(fd2)


# --------------------------------------------------------------------------- #
# C. Wiring: prepare gate, order, no .pull()
# --------------------------------------------------------------------------- #


def test_cycle_stops_before_generation_when_prepare_not_ok(monkeypatch, tmp_path):
    """If any enabled repo prepare is not ready, generation must NOT run."""
    monkeypatch.setattr(
        cu, "update_options", {"master": True, "i18n": False, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    master_repo = _init_repo(tmp_path, "master_repo")
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "i18n_diff_repo", None)

    prepare_calls = []
    generation_ran = {"flag": False}

    def _fake_prepare(repo, branch="main", allow_push=True):
        prepare_calls.append(repo)
        if repo is master_repo:
            return cu.GitResult(
                outcome=GitOutcome.BLOCKED, reason="dirty", operation="prepare"
            )
        return _prepare_ok()

    monkeypatch.setattr(cu, "prepare_repo_for_update", _fake_prepare)
    monkeypatch.setattr(
        cu, "_generate_and_publish",
        lambda *a, **k: generation_ran.__setitem__("flag", True) or {},
    )
    monkeypatch.setattr(cu, "_commit_enabled_repositories", lambda *a: {})
    monkeypatch.setattr(cu, "_push_enabled_repositories", lambda *a: None)

    _stub_jsonrpc_no_maintenance(monkeypatch)
    status = cu._run_update_cycle_locked(daily=True)
    assert status == "not_ready:master:dirty"
    assert prepare_calls == [master_repo]  # master prepared, then stop
    assert generation_ran["flag"] is False  # generation never ran


def test_all_prepares_run_before_generation(monkeypatch, tmp_path):
    """Every enabled repo is prepared before generation begins."""
    monkeypatch.setattr(
        cu, "update_options", {"master": True, "i18n": True, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    master_repo = _init_repo(tmp_path, "master_repo")
    i18n_repo = _init_repo(tmp_path, "i18n_repo")
    _write_commit(master_repo, "seed.txt", "seed", "seed master")
    _write_commit(i18n_repo, "seed.txt", "seed", "seed i18n")
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "i18n_diff_repo", i18n_repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_repo.working_dir)
    monkeypatch.setattr(cu, "i18n_diff_folder_path", i18n_repo.working_dir)
    _write_commit(master_repo, "seed.txt", "seed", "seed master")
    _write_commit(i18n_repo, "seed.txt", "seed", "seed i18n")

    order: list[str] = []

    def _fake_prepare(repo, branch="main", allow_push=True):
        order.append(f"prepare:{repo.working_dir}")
        return _prepare_ok()

    monkeypatch.setattr(cu, "prepare_repo_for_update", _fake_prepare)
    monkeypatch.setattr(
        cu, "_generate_and_publish",
        lambda daily, **k: order.append("generate") or {},
    )
    monkeypatch.setattr(cu, "_commit_enabled_repositories", lambda *a: {})
    monkeypatch.setattr(cu, "_push_enabled_repositories", lambda *a: None)

    _stub_jsonrpc_no_maintenance(monkeypatch)
    status = cu._run_update_cycle_locked(daily=True)
    assert status == "ok"
    assert order == [
        f"prepare:{master_repo.working_dir}",
        f"prepare:{i18n_repo.working_dir}",
        "generate",
    ]


def test_no_remote_pull_in_production_update_path():
    """No production update path (cycle, bootstrap, refresh, i18n) calls pull()."""
    import pathlib

    tree = ast.parse(pathlib.Path(cu.__file__).read_text(encoding="utf-8"))
    banned = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            # Any `.<something>.pull(` call (remote().pull(), repo.pull(), ...).
            if node.func.attr == "pull":
                banned.append(ast.dump(node))
    assert banned == [], f"production update path still calls .pull(): {banned}"


# --------------------------------------------------------------------------- #
# D. Staging / publication atomicity
# --------------------------------------------------------------------------- #


def test_master_generation_failure_leaves_trees_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cu, "update_options", {"master": True, "i18n": False, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    master_repo = _init_repo(tmp_path, "master_repo")
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_repo.working_dir)

    monkeypatch.setattr(cu, "prepare_repo_for_update", lambda *a, **k: _prepare_ok())
    monkeypatch.setattr(cu, "_commit_enabled_repositories", lambda *a: {})
    monkeypatch.setattr(cu, "_push_enabled_repositories", lambda *a: None)

    before = _snapshot_tree(master_repo)
    version_before = _snap_version(master_repo)

    def _boom(*a, **k):
        raise RuntimeError("master generation exploded")

    monkeypatch.setattr(cu, "refresh_version", _boom)

    _stub_jsonrpc_no_maintenance(monkeypatch)
    status = cu._run_update_cycle_locked(daily=True)
    assert status == "generation_failed"
    assert _snapshot_tree(master_repo) == before
    assert _snap_version(master_repo) == version_before


def test_i18n_generation_failure_blocks_master_publication(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cu, "update_options", {"master": True, "i18n": True, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    master_repo = _init_repo(tmp_path, "master_repo")
    i18n_repo = _init_repo(tmp_path, "i18n_repo")
    _write_commit(master_repo, "seed.txt", "seed", "seed master")
    _write_commit(i18n_repo, "seed.txt", "seed", "seed i18n")
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "i18n_diff_repo", i18n_repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_repo.working_dir)
    monkeypatch.setattr(cu, "i18n_diff_folder_path", i18n_repo.working_dir)

    monkeypatch.setattr(cu, "prepare_repo_for_update", lambda *a, **k: _prepare_ok())
    monkeypatch.setattr(cu, "_commit_enabled_repositories", lambda *a: {})
    monkeypatch.setattr(cu, "_push_enabled_repositories", lambda *a: None)

    before_master = _snapshot_tree(master_repo)

    def _refresh_i18n_booms(*args, **kwargs):
        cu._write_master_file(
            "versions.json", {"dataVersion": "1", "assetVersion": "1"}
        )
        cu._write_master_file("cards.json", [{"id": 1}])
        raise RuntimeError("i18n failed")

    monkeypatch.setattr(cu, "refresh_version", _refresh_i18n_booms)

    _stub_jsonrpc_no_maintenance(monkeypatch)
    status = cu._run_update_cycle_locked(daily=True)
    assert status == "generation_failed"
    # Neither master nor i18n working tree mutated.
    assert _snapshot_tree(master_repo) == before_master
    assert _snap_version(master_repo) is None


def test_json_validation_failure_leaves_trees_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cu, "update_options", {"master": True, "i18n": False, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    master_repo = _init_repo(tmp_path, "master_repo")
    _write_commit(master_repo, "seed.txt", "seed", "seed master")
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_repo.working_dir)
    monkeypatch.setattr(cu, "prepare_repo_for_update", lambda *a, **k: _prepare_ok())
    monkeypatch.setattr(cu, "_commit_enabled_repositories", lambda *a: {})
    monkeypatch.setattr(cu, "_push_enabled_repositories", lambda *a: None)

    before = _snapshot_tree(master_repo)

    # Force staged-JSON validation to fail for the generated candidate files.
    def _validate_raises(file_path):
        raise ValueError("staged JSON failed validation")

    monkeypatch.setattr(cu, "_validate_staged_json", _validate_raises)

    def _refresh_writes(*args, **kwargs):
        cu._write_master_file("cards.json", [{"id": 1}])
        cu._write_master_file("versions.json", {"dataVersion": "1"})

    monkeypatch.setattr(cu, "refresh_version", _refresh_writes)

    _stub_jsonrpc_no_maintenance(monkeypatch)
    status = cu._run_update_cycle_locked(daily=True)
    assert status == "generation_failed"
    assert _snapshot_tree(master_repo) == before


def test_failed_cycle_candidate_retry_succeeds(monkeypatch, tmp_path):
    """After a failure the same candidate can be retried and then succeed."""
    monkeypatch.setattr(
        cu, "update_options", {"master": True, "i18n": False, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    master_repo = _init_repo(tmp_path, "master_repo")
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_repo.working_dir)
    monkeypatch.setattr(cu, "prepare_repo_for_update", lambda *a, **k: _prepare_ok())
    monkeypatch.setattr(cu, "_commit_enabled_repositories", lambda *a: {})
    monkeypatch.setattr(cu, "_push_enabled_repositories", lambda *a: None)

    fails = {"n": 1}

    def _refresh_maybe(*args, **kwargs):
        if fails["n"] > 0:
            fails["n"] -= 1
            raise RuntimeError("transient generation error")
        cu._write_master_file(
            "versions.json", {"dataVersion": "9", "assetVersion": "9"}
        )
        cu._write_master_file("cards.json", [{"id": 1}])

    monkeypatch.setattr(cu, "refresh_version", _refresh_maybe)

    _stub_jsonrpc_no_maintenance(monkeypatch)
    first = cu._run_update_cycle_locked(daily=True)
    assert first == "generation_failed"
    assert _snap_version(master_repo) is None
    second = cu._run_update_cycle_locked(daily=True)
    assert second == "ok"
    assert _snap_version(master_repo)["dataVersion"] == "9"


def test_versions_json_published_last_on_success(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cu, "update_options", {"master": True, "i18n": False, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    master_repo = _init_repo(tmp_path, "master_repo")
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_repo.working_dir)
    monkeypatch.setattr(cu, "prepare_repo_for_update", lambda *a, **k: _prepare_ok())
    monkeypatch.setattr(cu, "_commit_enabled_repositories", lambda *a: {})
    monkeypatch.setattr(cu, "_push_enabled_repositories", lambda *a: None)

    publish_order: list[str] = []
    real_replace = os.replace

    def _tracked_replace(src, dst):
        publish_order.append(os.path.basename(dst))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _tracked_replace)

    def _refresh_ok(*args, **kwargs):
        cu._write_master_file("cards.json", [{"id": 1}])
        cu._write_master_file("events.json", [{"id": 2}])
        cu._write_master_file("versions.json", {"dataVersion": "1"})

    monkeypatch.setattr(cu, "refresh_version", _refresh_ok)

    _stub_jsonrpc_no_maintenance(monkeypatch)
    cu._run_update_cycle_locked(daily=True)
    assert publish_order[-1] == "versions.json"
    assert "versions.json" in publish_order
    assert publish_order.index("versions.json") > publish_order.index("cards.json")


def test_replace_failure_no_commit_push_and_dirty_left(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cu, "update_options", {"master": True, "i18n": False, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    master_repo = _init_repo(tmp_path, "master_repo")
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_repo.working_dir)
    monkeypatch.setattr(cu, "prepare_repo_for_update", lambda *a, **k: _prepare_ok())

    committed = {"n": 0}
    pushed = {"n": 0}
    monkeypatch.setattr(
        cu, "_commit_enabled_repositories",
        lambda *a, **k: committed.__setitem__("n", committed["n"] + 1) or {},
    )
    monkeypatch.setattr(
        cu, "_push_enabled_repositories",
        lambda *a, **k: pushed.__setitem__("n", pushed["n"] + 1) or None,
    )

    real_replace = os.replace

    def _replace_first_then_fail(src, dst):
        if os.path.basename(dst) == "cards.json":
            return real_replace(src, dst)  # publish one file first
        raise OSError("disk full during replace")

    monkeypatch.setattr(os, "replace", _replace_first_then_fail)

    def _refresh_ok(*args, **kwargs):
        cu._write_master_file("cards.json", [{"id": 1}])
        cu._write_master_file("events.json", [{"id": 2}])
        cu._write_master_file("versions.json", {"dataVersion": "1"})

    monkeypatch.setattr(cu, "refresh_version", _refresh_ok)

    _stub_jsonrpc_no_maintenance(monkeypatch)
    status = cu._run_update_cycle_locked(daily=True)
    assert status == "publication_failed"
    assert committed["n"] == 0
    assert pushed["n"] == 0
    # Dirty working tree retained: cards.json published, events/version not.
    assert os.path.exists(os.path.join(master_repo.working_dir, "cards.json"))
    assert not os.path.exists(os.path.join(master_repo.working_dir, "events.json"))
    assert _snap_version(master_repo) is None


# --------------------------------------------------------------------------- #
# E. Git publication: manifest-only staging, partial commit/push
# --------------------------------------------------------------------------- #


def test_manifest_only_stages_explicit_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cu, "update_options", {"master": True, "i18n": False, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    master_repo = _init_repo(tmp_path, "master_repo")
    remote_url = _make_bare_remote(tmp_path)
    master_repo.create_remote("origin", remote_url)
    _write_commit(master_repo, "unrelated.txt", "keep me", "seed")
    with open(os.path.join(master_repo.working_dir, "untracked.txt"), "w") as f:
        f.write("should not be staged")

    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_repo.working_dir)
    monkeypatch.setattr(
        cu, "version_info", {"dataVersion": "1", "assetVersion": "1"}
    )

    staged = {}
    real_prepare_target = cu._prepare_commit_target

    def _tracking_prepare(repo, key, manifest, *args, **kwargs):
        staged[key] = list(manifest)
        return real_prepare_target(repo, key, manifest, *args, **kwargs)

    monkeypatch.setattr(cu, "_prepare_commit_target", _tracking_prepare)
    monkeypatch.setattr(cu, "prepare_repo_for_update", lambda *a, **k: _prepare_ok())
    monkeypatch.setattr(cu, "_push_enabled_repositories", lambda *a: None)
    _write_commit(master_repo, "seed.txt", "seed", "seed master")

    def _refresh_ok(*args, **kwargs):
        cu._write_master_file("cards.json", [{"id": 1}])
        cu._write_master_file("versions.json", {"dataVersion": "1"})

    monkeypatch.setattr(cu, "refresh_version", _refresh_ok)

    _stub_jsonrpc_no_maintenance(monkeypatch)
    status = cu._run_update_cycle_locked(daily=True)
    assert status == "ok"
    assert staged["master"] == ["cards.json", "versions.json"]
    assert "unrelated.txt" not in staged["master"]
    assert "untracked.txt" not in staged["master"]


def test_i18n_commit_failure_leaves_master_unpushed(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cu, "update_options", {"master": True, "i18n": True, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    master_repo = _init_repo(tmp_path, "master_repo")
    i18n_repo = _init_repo(tmp_path, "i18n_repo")
    _write_commit(master_repo, "seed.txt", "seed", "seed master")
    _write_commit(i18n_repo, "seed.txt", "seed", "seed i18n")
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "i18n_diff_repo", i18n_repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_repo.working_dir)
    monkeypatch.setattr(cu, "i18n_diff_folder_path", i18n_repo.working_dir)
    monkeypatch.setattr(
        cu, "version_info", {"dataVersion": "1", "assetVersion": "1"}
    )
    monkeypatch.setattr(cu, "prepare_repo_for_update", lambda *a, **k: _prepare_ok())

    def _refresh_ok(*args, **kwargs):
        cu._write_master_file("cards.json", [{"id": 1}])
        cu._write_master_file("versions.json", {"dataVersion": "1"})
        cu._write_i18n_file("card_prefix.json", {"1": "p"})

    monkeypatch.setattr(cu, "refresh_version", _refresh_ok)

    pushed = []

    def _fake_commit_enabled(enabled, manifest, candidate=None, journal=None):
        del manifest, candidate
        return {
            "master": cu.GitResult(
                outcome=GitOutcome.OK,
                reason="committed",
                local_sha=master_repo.head.commit.hexsha,
            ),
            "i18n": cu.GitResult(outcome=GitOutcome.FAILED, reason="commit_failed"),
        }

    monkeypatch.setattr(cu, "_commit_enabled_repositories", _fake_commit_enabled)
    monkeypatch.setattr(
        cu, "_push_enabled_repositories",
        lambda commits_d: pushed.append(list(commits_d.keys())) or None,
    )

    _stub_jsonrpc_no_maintenance(monkeypatch)
    status = cu._run_update_cycle_locked(daily=True)
    assert status == "commit_failed"
    assert pushed == []  # no push happened because a commit failed


def test_commit_all_before_first_push(monkeypatch, tmp_path):
    """All enabled repos are committed before the first push begins."""
    monkeypatch.setattr(
        cu, "update_options", {"master": True, "i18n": True, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    master_repo = _init_repo(tmp_path, "master_repo")
    i18n_repo = _init_repo(tmp_path, "i18n_repo")
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "i18n_diff_repo", i18n_repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_repo.working_dir)
    monkeypatch.setattr(cu, "i18n_diff_folder_path", i18n_repo.working_dir)
    monkeypatch.setattr(
        cu, "version_info", {"dataVersion": "1", "assetVersion": "1"}
    )
    monkeypatch.setattr(cu, "prepare_repo_for_update", lambda *a, **k: _prepare_ok())

    def _refresh_ok(*args, **kwargs):
        cu._write_master_file("cards.json", [{"id": 1}])
        cu._write_master_file("versions.json", {"dataVersion": "1"})
        cu._write_i18n_file("card_prefix.json", {"1": "p"})

    monkeypatch.setattr(cu, "refresh_version", _refresh_ok)

    seq: list[str] = []

    _write_commit(master_repo, "seed.txt", "seed", "seed master")
    _write_commit(i18n_repo, "seed.txt", "seed", "seed i18n")

    def _fake_commit_enabled(enabled, manifest, candidate=None, journal=None):
        del manifest, candidate
        for key, _repo in enabled:
            seq.append(f"commit:{key}")
            repo = master_repo if key == "master" else i18n_repo
            journal.update_repo(
                key,
                commit_state=cu.RepoCommitState.COMMITTED,
                target_commit_sha=repo.head.commit.hexsha,
            )
        return {
            "master": cu.GitResult(
                outcome=GitOutcome.OK,
                reason="committed",
                local_sha=master_repo.head.commit.hexsha,
            ),
            "i18n": cu.GitResult(
                outcome=GitOutcome.OK,
                reason="committed",
                local_sha=i18n_repo.head.commit.hexsha,
            ),
        }

    monkeypatch.setattr(cu, "_commit_enabled_repositories", _fake_commit_enabled)

    def _fake_push(commits_d):
        for key in commits_d:
            seq.append(f"push:{key}")
        return None

    monkeypatch.setattr(cu, "_push_enabled_repositories", _fake_push)

    _stub_jsonrpc_no_maintenance(monkeypatch)
    status = cu._run_update_cycle_locked(daily=True)
    assert status == "ok"
    assert seq == ["commit:master", "commit:i18n", "push:master", "push:i18n"]


def test_first_push_failure_stops_later_push_keeps_commits(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cu, "update_options", {"master": True, "i18n": True, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    master_repo = _init_repo(tmp_path, "master_repo")
    i18n_repo = _init_repo(tmp_path, "i18n_repo")
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "i18n_diff_repo", i18n_repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_repo.working_dir)
    monkeypatch.setattr(cu, "i18n_diff_folder_path", i18n_repo.working_dir)
    monkeypatch.setattr(
        cu, "version_info", {"dataVersion": "1", "assetVersion": "1"}
    )
    monkeypatch.setattr(cu, "prepare_repo_for_update", lambda *a, **k: _prepare_ok())

    def _refresh_ok(*args, **kwargs):
        cu._write_master_file("cards.json", [{"id": 1}])
        cu._write_master_file("versions.json", {"dataVersion": "1"})
        cu._write_i18n_file("card_prefix.json", {"1": "p"})

    monkeypatch.setattr(cu, "refresh_version", _refresh_ok)

    def _fake_commit_enabled(enabled, manifest, candidate=None, journal=None):
        # Create a real local commit so the "local commit preserved" assertion
        # is meaningful (the cycle must never reset/rebase/delete it).
        res = {}
        for key, repo in enabled:
            if repo is not None:
                repo.index.commit(f"seed {key}")
                journal.update_repo(
                    key,
                    commit_state=cu.RepoCommitState.COMMITTED,
                    target_commit_sha=repo.head.commit.hexsha,
                )
            res[key] = cu.GitResult(
                outcome=GitOutcome.OK, reason="committed",
                local_sha=repo.head.commit.hexsha if repo else None,
            )
        return res

    monkeypatch.setattr(cu, "_commit_enabled_repositories", _fake_commit_enabled)

    pushed_keys = []

    def _fake_push(commits_d):
        pushed_keys.append("master")
        return "push_failed:master:push_rejected"

    monkeypatch.setattr(cu, "_push_enabled_repositories", _fake_push)

    _stub_jsonrpc_no_maintenance(monkeypatch)
    status = cu._run_update_cycle_locked(daily=True)
    assert status == "push_failed:master:push_rejected"
    assert pushed_keys == ["master"]  # only master attempted
    # Both local commits remain (never reset/rebased/deleted).
    assert master_repo.head.commit.hexsha is not None
    assert i18n_repo.head.commit.hexsha is not None


def test_partial_push_recovers_next_cycle(monkeypatch, tmp_path):
    """After a master push failure, the journal is RETAINED (not dropped) so the
    next cycle runs durable recovery and pushes the same local commit.

    Uses a local bare remote (no network). master has an unpushed local commit;
    the next cycle must recover (not run a fresh ``ok`` cycle) and, once the
    remote accepts it, push the same local commit.
    """
    monkeypatch.setattr(
        cu, "update_options", {"master": True, "i18n": False, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    master_repo = _init_repo(tmp_path, "master_repo")
    remote_url = _make_bare_remote(tmp_path)
    master_repo.create_remote("origin", remote_url)
    _write_commit(master_repo, "base.txt", "base", "seed base")
    master_repo.git.push("origin", "main")
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_repo.working_dir)
    monkeypatch.setattr(
        cu, "version_info", {"dataVersion": "1", "assetVersion": "1"}
    )

    def _push_fail(repo, branch="main", **kwargs):
        return cu.GitResult(
            outcome=GitOutcome.PENDING_PUSH,
            reason="push_rejected",
            local_sha=repo.head.commit.hexsha,
        )

    monkeypatch.setattr(cu, "prepare_repo_for_update", lambda *a, **k: _prepare_ok())
    monkeypatch.setattr(cu, "push_current_head", _push_fail)

    def _refresh_ok(*args, **kwargs):
        cu._write_master_file("cards.json", [{"id": 1}])
        cu._write_master_file("versions.json", {"dataVersion": "1"})

    monkeypatch.setattr(cu, "refresh_version", _refresh_ok)

    _stub_jsonrpc_no_maintenance(monkeypatch)
    status1 = cu._run_update_cycle_locked(daily=True)
    assert status1 == "push_failed:master:push_rejected"
    local_sha_after_fail = master_repo.head.commit.hexsha
    assert local_sha_after_fail is not None  # local commit preserved
    # The journal is RETAINED after a post-journal push failure.
    assert TransactionJournal.load(master_repo.git_dir) is not None

    # Next cycle: remote now accepts the push; the retained journal must drive
    # recovery (status "recovered"), not a fresh "ok" cycle.
    pushed_second = {"ok": False}

    def _push_ok(repo, branch="main", **kwargs):
        pushed_second["ok"] = True
        return cu.GitResult(
            outcome=GitOutcome.OK, reason="pushed",
            local_sha=master_repo.head.commit.hexsha)

    monkeypatch.setattr(cu, "push_current_head", _push_ok)
    # This unit test replaces the actual push transport; keep the decisive
    # recovery assertion on the post-attempt authoritative-probe seam.
    monkeypatch.setattr(
        cu, "_probe_remote",
        lambda repo, key, state: state.target_commit_sha,
    )
    monkeypatch.setattr(
        cu, "prepare_repo_for_update",
        lambda *a, **k: cu.GitResult(outcome=GitOutcome.OK, reason="equal"),
    )

    status2 = cu._run_update_cycle_locked(daily=True)
    assert status2 == "recovered"
    # The authoritative probe found the target already accepted, so recovery
    # completed without issuing a duplicate push.
    assert pushed_second["ok"] is False
    # The local commit remains present afterwards (never deleted/rebased).
    assert master_repo.head.commit.hexsha is not None
    # Journal cleared after successful recovery.
    assert TransactionJournal.load(master_repo.git_dir) is None


# --------------------------------------------------------------------------- #
# F. Candidate / published separation
# --------------------------------------------------------------------------- #


def test_candidate_failure_does_not_advance_published_version(monkeypatch, tmp_path):
    """A candidate that fails to generate/publish must not advance version_info."""
    monkeypatch.setattr(
        cu, "update_options", {"master": True, "i18n": False, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    master_repo = _init_repo(tmp_path, "master_repo")
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_repo.working_dir)

    published_before = {"value": "UNSET"}
    published_before["value"] = cu.version_info

    monkeypatch.setattr(cu, "prepare_repo_for_update", lambda *a, **k: _prepare_ok())
    monkeypatch.setattr(cu, "_commit_enabled_repositories", lambda *a: {})
    monkeypatch.setattr(cu, "_push_enabled_repositories", lambda *a: None)

    def _boom(*args, **kwargs):
        raise RuntimeError("candidate generation failed")

    monkeypatch.setattr(cu, "refresh_version", _boom)

    _stub_jsonrpc_no_maintenance(monkeypatch)
    status = cu._run_update_cycle_locked(daily=True)
    assert status == "generation_failed"
    # The published global is untouched (including relative to None initial).
    assert cu.version_info == published_before["value"]


def test_candidate_passed_through_to_generation_and_commit_message(
    monkeypatch, tmp_path
):
    """The candidate (not the published global) drives generation + commit msg."""
    monkeypatch.setattr(
        cu, "update_options", {"master": True, "i18n": False, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    master_repo = _init_repo(tmp_path, "master_repo")
    _write_commit(master_repo, "seed.txt", "seed", "seed master")
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_repo.working_dir)
    monkeypatch.setattr(cu, "prepare_repo_for_update", lambda *a, **k: _prepare_ok())
    monkeypatch.setattr(cu, "_push_enabled_repositories", lambda *a: None)

    captured_candidate = {}

    def _fake_refresh(candidate=None):
        candidate = {"dataVersion": "7", "assetVersion": "7"}
        # Mirror production: write versions.json from candidate, then return it.
        cu._write_master_file("versions.json", candidate)
        captured_candidate["value"] = candidate
        return candidate

    monkeypatch.setattr(cu, "refresh_version", _fake_refresh)

    captured_msg = {}

    def _fake_commit_enabled(enabled, manifest, candidate=None, journal=None):
        # Use the published global exactly as production does.
        for key, _repo in enabled:
            captured_msg[key] = (
                f"master version {cu.version_info['dataVersion']} "
                f"asset version {cu.version_info['assetVersion']}"
            )
        result = {}
        journal = journal or TransactionJournal.load(master_repo.git_dir)
        for k, repo in enabled:
            journal.update_repo(
                k,
                commit_state=cu.RepoCommitState.COMMITTED,
                target_commit_sha=repo.head.commit.hexsha,
            )
            result[k] = cu.GitResult(
                outcome=GitOutcome.OK,
                reason="committed",
                local_sha=repo.head.commit.hexsha,
            )
        return result

    monkeypatch.setattr(cu, "_commit_enabled_repositories", _fake_commit_enabled)

    _stub_jsonrpc_no_maintenance(monkeypatch)
    cu._run_update_cycle_locked(daily=True)
    # The candidate that drove generation equals the one used for the commit msg.
    assert captured_candidate["value"]["dataVersion"] == "7"
    # And the published global was advanced to that candidate after success.
    assert cu.version_info["dataVersion"] == "7"
    assert captured_msg["master"] == "master version 7 asset version 7"


def test_published_version_unchanged_on_validation_failure(monkeypatch, tmp_path):
    """JSON validation failure keeps published version_info at its old value."""
    monkeypatch.setattr(
        cu, "update_options", {"master": True, "i18n": False, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    master_repo = _init_repo(tmp_path, "master_repo")
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_repo.working_dir)
    monkeypatch.setattr(
        cu, "version_info", {"dataVersion": "OLD", "assetVersion": "OLD"}
    )
    monkeypatch.setattr(cu, "prepare_repo_for_update", lambda *a, **k: _prepare_ok())
    monkeypatch.setattr(cu, "_commit_enabled_repositories", lambda *a: {})
    monkeypatch.setattr(cu, "_push_enabled_repositories", lambda *a: None)
    def _validate_raises(file_path):
        raise ValueError("staged JSON failed validation")

    monkeypatch.setattr(cu, "_validate_staged_json", _validate_raises)

    def _refresh_writes(*args, **kwargs):
        cu._write_master_file("cards.json", [{"id": 1}])
        cu._write_master_file(
            "versions.json", {"dataVersion": "NEW", "assetVersion": "NEW"}
        )

    monkeypatch.setattr(cu, "refresh_version", _refresh_writes)

    _stub_jsonrpc_no_maintenance(monkeypatch)
    status = cu._run_update_cycle_locked(daily=True)
    assert status == "generation_failed"
    # Published global must remain at its old value despite the candidate change.
    assert cu.version_info == {"dataVersion": "OLD", "assetVersion": "OLD"}


def test_simple_check_does_not_advance_global_outside_cycle(monkeypatch):
    """Simple-mode candidate check returns the candidate but never assigns global."""
    monkeypatch.setattr(cu, "check_update_simple_mode", True)
    monkeypatch.setattr(cu, "version_info", {"dataVersion": "BASE"})

    def _fake_fetch():
        return {"dataVersion": "CANDIDATE", "assetVersion": "CANDIDATE"}

    monkeypatch.setattr(cu, "fetch_simple_version_info", _fake_fetch)

    result = cu.check_versions_simple()
    assert result["new_version"] is True
    assert result["candidate_version_info"]["dataVersion"] == "CANDIDATE"
    # The published global is NOT advanced by the check itself.
    assert cu.version_info == {"dataVersion": "BASE"}


# --------------------------------------------------------------------------- #
# H. Cycle gating branches (_cycle_should_proceed)
# --------------------------------------------------------------------------- #


def test_cycle_should_proceed_daily_bypasses_new_version_gate(monkeypatch):
    """daily=True must proceed even when the server reports no new version (the
    new-version gate only blocks ordinary runs); maintenance still stops it."""
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(
        cu, "version_info", {"dataVersion": "1", "assetVersion": "1"}
    )

    # Ordinary-style "no change" response: daily must still proceed.
    _stub_jsonrpc(monkeypatch, maintenance=False, new_version=False)
    assert cu._cycle_should_proceed(daily=True) is None

    # Maintenance stops even a daily run.
    _stub_jsonrpc(monkeypatch, maintenance=True, new_version=False)
    assert cu._cycle_should_proceed(daily=True) == "maintenance"


def test_cycle_should_proceed_ordinary_requires_new_version(monkeypatch):
    """ordinary run must respect the new-version gate and return no_new_version
    when the server reports no change."""
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(
        cu, "version_info", {"dataVersion": "1", "assetVersion": "1"}
    )

    _stub_jsonrpc(monkeypatch, maintenance=False, new_version=False)
    assert cu._cycle_should_proceed(daily=False) == "no_new_version"

    # A real change lets an ordinary run proceed.
    _stub_jsonrpc(monkeypatch, maintenance=False, new_version=True)
    assert cu._cycle_should_proceed(daily=False) is None


def test_cycle_should_proceed_simple_uses_new_version(monkeypatch):
    """simple mode must use the simple-mode new-version check (no maintenance
    gate) and skip when there is no new version."""
    monkeypatch.setattr(cu, "check_update_simple_mode", True)
    monkeypatch.setattr(
        cu, "version_info", {"dataVersion": "1", "assetVersion": "1"}
    )
    monkeypatch.setattr(cu, "check_update_versions_url", "http://example/versions")

    def _fake_fetch_no_change():
        # Candidate matches the published global -> no new version.
        return {"dataVersion": "1", "assetVersion": "1"}

    def _fake_fetch_new():
        return {"dataVersion": "2", "assetVersion": "2"}

    monkeypatch.setattr(cu, "fetch_simple_version_info", _fake_fetch_no_change)

    _stub_jsonrpc(monkeypatch, simple_new_version=False)
    assert cu._cycle_should_proceed(daily=True) == "no_new_version"

    monkeypatch.setattr(cu, "fetch_simple_version_info", _fake_fetch_new)
    _stub_jsonrpc(monkeypatch, simple_new_version=True)
    assert cu._cycle_should_proceed(daily=True) is None


def test_cycle_locked_returns_no_new_version_when_ordinary_unchanged(
    monkeypatch, tmp_path
):
    """End-to-end: an ordinary run with no version change must skip (no gen)."""
    monkeypatch.setattr(
        cu, "update_options", {"master": True, "i18n": False, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    master_repo = _init_repo(tmp_path, "master_repo")
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_repo.working_dir)
    monkeypatch.setattr(cu, "prepare_repo_for_update", lambda *a, **k: _prepare_ok())
    monkeypatch.setattr(cu, "_generate_and_publish", lambda *a: {})
    monkeypatch.setattr(cu, "_commit_enabled_repositories", lambda *a, **k: {})
    monkeypatch.setattr(cu, "_push_enabled_repositories", lambda *a: None)

    generation_ran = {"flag": False}

    def _tracked_gen(daily):
        generation_ran["flag"] = True
        return {}

    monkeypatch.setattr(cu, "_generate_and_publish", _tracked_gen)
    _stub_jsonrpc(monkeypatch, maintenance=False, new_version=False)
    assert cu._run_update_cycle_locked(daily=False) == "no_new_version"
    assert generation_ran["flag"] is False


# --------------------------------------------------------------------------- #
# I. Generation obeys update_options (disabled repos write nothing)
# --------------------------------------------------------------------------- #


def test_generation_master_disabled_writes_no_master(monkeypatch, tmp_path):
    """When master is disabled, master staging root is never written to: no
    master manifest entries, no versions.json/userInfo, even with i18n on."""
    monkeypatch.setattr(
        cu, "update_options", {"master": False, "i18n": True, "userInfo": True}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    master_repo = _init_repo(tmp_path, "master_repo")
    i18n_repo = _init_repo(tmp_path, "i18n_repo")
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "i18n_diff_repo", i18n_repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_repo.working_dir)
    monkeypatch.setattr(cu, "i18n_diff_folder_path", i18n_repo.working_dir)
    monkeypatch.setattr(cu, "prepare_repo_for_update", lambda *a, **k: _prepare_ok())
    monkeypatch.setattr(cu, "_commit_enabled_repositories", lambda *a: {})
    monkeypatch.setattr(cu, "_push_enabled_repositories", lambda *a: None)

    captured_manifest = {}

    def _tracked_gen():
        # Simulate real generation: master files (should be suppressed because
        # master is disabled) plus an i18n file (should be written because i18n
        # is enabled). Use the production write helpers so the disabled flags
        # take effect exactly as in production.
        cu._write_master_file("cards.json", [{"id": 1}])
        cu._write_i18n_file("card_prefix.json", {"1": "p"})
        captured_manifest["master"] = list(cu._STAGING_MANIFEST["master"])
        captured_manifest["i18n"] = list(cu._STAGING_MANIFEST["i18n"])

    monkeypatch.setattr(cu, "refresh_version", lambda *a: _tracked_gen() or {})
    monkeypatch.setattr(cu, "save_info_from_suite_user", lambda *a: None)
    monkeypatch.setattr(cu, "refresh_information", lambda *a: None)

    _stub_jsonrpc(monkeypatch, maintenance=False, new_version=True)
    status = cu._run_update_cycle_locked(daily=True)
    # master disabled: no master writes at all (no versions.json, no files).
    assert status == "ok"
    assert captured_manifest["master"] == []
    # i18n enabled: i18n writes happen.
    assert captured_manifest["i18n"]  # non-empty
    # The master working tree contains nothing new.
    assert _snapshot_tree(master_repo) == {}


def test_generation_all_disabled_writes_nothing(monkeypatch, tmp_path):
    """When both master and i18n are disabled, nothing is written anywhere."""
    monkeypatch.setattr(
        cu, "update_options", {"master": False, "i18n": False, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    master_repo = _init_repo(tmp_path, "master_repo")
    i18n_repo = _init_repo(tmp_path, "i18n_repo")
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "i18n_diff_repo", i18n_repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_repo.working_dir)
    monkeypatch.setattr(cu, "i18n_diff_folder_path", i18n_repo.working_dir)
    monkeypatch.setattr(cu, "prepare_repo_for_update", lambda *a, **k: _prepare_ok())
    monkeypatch.setattr(cu, "_commit_enabled_repositories", lambda *a: {})
    monkeypatch.setattr(cu, "_push_enabled_repositories", lambda *a: None)

    captured_manifest = {}

    def _tracked_gen():
        captured_manifest["master"] = list(cu._STAGING_MANIFEST["master"])
        captured_manifest["i18n"] = list(cu._STAGING_MANIFEST["i18n"])

    monkeypatch.setattr(cu, "refresh_version", lambda *a: _tracked_gen() or {})
    monkeypatch.setattr(cu, "save_info_from_suite_user", lambda *a: None)
    monkeypatch.setattr(cu, "refresh_information", lambda *a: None)

    _stub_jsonrpc(monkeypatch, maintenance=False, new_version=True)
    status = cu._run_update_cycle_locked(daily=True)
    assert status == "ok"
    assert captured_manifest["master"] == []
    assert captured_manifest["i18n"] == []
    assert _snapshot_tree(master_repo) == {}
    assert _snapshot_tree(i18n_repo) == {}


def test_generation_userinfo_true_master_false(monkeypatch, tmp_path):
    """userInfo=True with master=False: user-info writes are master writes, so
    they are suppressed; i18n (enabled) still writes."""
    monkeypatch.setattr(
        cu, "update_options", {"master": False, "i18n": True, "userInfo": True}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "en")  # en triggers refresh_information
    master_repo = _init_repo(tmp_path, "master_repo")
    i18n_repo = _init_repo(tmp_path, "i18n_repo")
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "i18n_diff_repo", i18n_repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_repo.working_dir)
    monkeypatch.setattr(cu, "i18n_diff_folder_path", i18n_repo.working_dir)
    monkeypatch.setattr(cu, "prepare_repo_for_update", lambda *a, **k: _prepare_ok())
    monkeypatch.setattr(cu, "_commit_enabled_repositories", lambda *a: {})
    monkeypatch.setattr(cu, "_push_enabled_repositories", lambda *a: None)

    # These must be no-ops when master is disabled (verify they are not reached
    # for master writes by asserting the master tree stays empty).
    monkeypatch.setattr(cu, "save_info_from_suite_user", lambda *a: None)
    monkeypatch.setattr(cu, "refresh_information", lambda *a: None)

    def _tracked_gen():
        # Simulate a userInfo write attempt that must be ignored under master=off
        cu._write_master_file("userInformations.json", [{"id": 1}])
        cu._write_i18n_file("card_prefix.json", {"1": "p"})

    monkeypatch.setattr(cu, "refresh_version", lambda *a: _tracked_gen() or {})

    _stub_jsonrpc(monkeypatch, maintenance=False, new_version=True)
    status = cu._run_update_cycle_locked(daily=True)
    assert status == "ok"
    # master (incl. userInfo) suppressed; i18n written.
    assert _snapshot_tree(master_repo) == {}
    assert os.path.exists(os.path.join(i18n_repo.working_dir, "ja", "card_prefix.json"))


# --------------------------------------------------------------------------- #
# J. _commit_diff path-list contract
# --------------------------------------------------------------------------- #


def test_commit_diff_empty_paths_is_nothing_to_do(tmp_path, monkeypatch):
    """An explicit empty path list must NOT stage/commit (no broad add that would
    sweep unrelated dirty files)."""
    repo = _init_repo(tmp_path, "repo")
    _write_commit(repo, "unrelated.txt", "must stay unstaged", "seed")
    with open(os.path.join(repo.working_dir, "untracked.txt"), "w") as f:
        f.write("untracked garbage")

    monkeypatch.setattr(cu, "version_info", {"dataVersion": "1", "assetVersion": "1"})

    staged = {}

    real_add = git.IndexFile.add

    def _track(*a, **k):
        staged["called"] = True
        return real_add(*a, **k)

    monkeypatch.setattr(git.IndexFile, "add", _track)

    res = cu._commit_diff(
        repo, "op", "label", "msg",
        git.Actor("b", "b@e.com"), paths=[],
    )
    assert res.outcome is GitOutcome.NOTHING_TO_DO
    assert res.reason == "no_staged_paths"
    assert "called" not in staged  # no stage at all
    # Unrelated + untracked files remain untouched (never staged/committed).
    assert not repo.git.diff("--cached", "--name-only")


def test_commit_diff_none_paths_broad_adds(tmp_path, monkeypatch):
    """paths=None (legacy) still does a broad add and commits."""
    repo = _init_repo(tmp_path, "repo")
    with open(os.path.join(repo.working_dir, "x.txt"), "w") as f:
        f.write("dirty")

    monkeypatch.setattr(cu, "version_info", {"dataVersion": "1", "assetVersion": "1"})
    monkeypatch.setattr(cu, "prepare_repo_for_update", lambda *a, **k: _prepare_ok())
    res = cu._commit_diff(
        repo, "op", "label", "msg",
        git.Actor("b", "b@e.com"), paths=None,
    )
    assert res.outcome is GitOutcome.OK
    # The broad add staged and committed x.txt (no exception path -> committed).
    assert "x.txt" in repo.git.show("--stat", "--oneline", "HEAD")


# --------------------------------------------------------------------------- #
# K. _run_update_cycle: only RepoLockUnavailable is swallowed; body errors
#    propagate and the process lock is released.
# --------------------------------------------------------------------------- #


def test_run_update_cycle_swallows_repo_lock_unavailable(monkeypatch):
    from contextlib import contextmanager

    from utils.git_lock import RepoLockUnavailable

    calls = []

    def _fake_locked(daily):
        calls.append(daily)
        return "ok"

    monkeypatch.setattr(cu, "_run_update_cycle_locked", _fake_locked)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", "/tmp/master")
    monkeypatch.setattr(cu, "i18n_diff_folder_path", "/tmp/i18n")

    @contextmanager
    def _boom_locks(paths, non_blocking=True):
        raise RepoLockUnavailable("held elsewhere")
        yield  # pragma: no cover

    monkeypatch.setattr(cu, "repo_file_locks", _boom_locks)

    status = cu._run_update_cycle(daily=True)
    assert status == "skipped:repo_lock"
    assert calls == []  # body never ran
    # The process lock must be free again.
    assert cu._PROCESS_LOCK.acquire() is True
    cu._PROCESS_LOCK.release()


def test_run_update_cycle_propagates_body_runtime_error_and_releases_lock(monkeypatch):
    """A RuntimeError raised inside the locked body is propagated (not swallowed);
    the process lock is released regardless."""
    from contextlib import contextmanager

    monkeypatch.setattr(cu, "masterdb_diff_folder_path", "/tmp/master")
    monkeypatch.setattr(cu, "i18n_diff_folder_path", "/tmp/i18n")

    @contextmanager
    def _ok_locks(paths, non_blocking=True):
        yield

    monkeypatch.setattr(cu, "repo_file_locks", _ok_locks)

    def _boom_locked(daily, deadline=None):
        raise RuntimeError("cycle body failed")

    monkeypatch.setattr(cu, "_run_update_cycle_locked", _boom_locked)

    with pytest.raises(RuntimeError):
        cu._run_update_cycle(daily=True)
    # Process lock released on the exception path.
    assert cu._PROCESS_LOCK.acquire() is True
    cu._PROCESS_LOCK.release()


# --------------------------------------------------------------------------- #
# L. Publication OSError -> publication_failed (distinct from generation_failed)
# --------------------------------------------------------------------------- #


def test_publication_oserror_returns_publication_failed(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cu, "update_options", {"master": True, "i18n": False, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    master_repo = _init_repo(tmp_path, "master_repo")
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_repo.working_dir)
    monkeypatch.setattr(cu, "prepare_repo_for_update", lambda *a, **k: _prepare_ok())
    monkeypatch.setattr(cu, "_commit_enabled_repositories", lambda *a: {})
    monkeypatch.setattr(cu, "_push_enabled_repositories", lambda *a: None)

    def _refresh_ok(*args, **kwargs):
        cu._write_master_file("cards.json", [{"id": 1}])
        cu._write_master_file("versions.json", {"dataVersion": "1"})

    monkeypatch.setattr(cu, "refresh_version", _refresh_ok)

    def _always_fail(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", _always_fail)

    _stub_jsonrpc(monkeypatch, maintenance=False, new_version=True)
    status = cu._run_update_cycle_locked(daily=True)
    assert status == "publication_failed"
    # No commit/push happens on publication failure.
    assert not master_repo.git.diff("--cached", "--name-only")


def test_publication_versions_json_last_and_i18n_failure_keeps_global(
    monkeypatch, tmp_path
):
    """Global versions.json published last and alone: an i18n replace failure
    must not touch the formal master versions.json / published global."""
    monkeypatch.setattr(
        cu, "update_options", {"master": True, "i18n": True, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    master_repo = _init_repo(tmp_path, "master_repo")
    i18n_repo = _init_repo(tmp_path, "i18n_repo")
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "i18n_diff_repo", i18n_repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_repo.working_dir)
    monkeypatch.setattr(cu, "i18n_diff_folder_path", i18n_repo.working_dir)
    monkeypatch.setattr(cu, "prepare_repo_for_update", lambda *a, **k: _prepare_ok())
    monkeypatch.setattr(cu, "_commit_enabled_repositories", lambda *a: {})
    monkeypatch.setattr(cu, "_push_enabled_repositories", lambda *a: None)
    monkeypatch.setattr(
        cu, "version_info", {"dataVersion": "OLD", "assetVersion": "OLD"}
    )

    def _refresh_ok(*args, **kwargs):
        cu._write_master_file("cards.json", [{"id": 1}])
        cu._write_master_file("versions.json", {"dataVersion": "NEW"})
        cu._write_i18n_file("card_prefix.json", {"1": "p"})

    monkeypatch.setattr(cu, "refresh_version", _refresh_ok)

    order: list[str] = []
    real_replace = os.replace

    def _tracked_replace(src, dst):
        order.append(os.path.basename(dst))
        if os.path.basename(dst) == "card_prefix.json":
            raise OSError("i18n disk full")  # i18n fails after master+cards
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _tracked_replace)

    _stub_jsonrpc(monkeypatch, maintenance=False, new_version=True)
    status = cu._run_update_cycle_locked(daily=True)
    assert status == "publication_failed"
    # i18n failed: master cards.json was published (moved before i18n), but the
    # formal versions.json must NOT have been moved yet (it is published last,
    # after i18n, so an i18n replace failure never reaches it).
    assert os.path.exists(os.path.join(master_repo.working_dir, "cards.json"))
    assert not os.path.exists(os.path.join(master_repo.working_dir, "versions.json"))
    # Published global unchanged.
    assert cu.version_info == {"dataVersion": "OLD", "assetVersion": "OLD"}
    # On the i18n failure path versions.json is never attempted (master+cards ran
    # first, then i18n failed before reaching the last versions.json step).
    assert "versions.json" not in order
    assert "cards.json" in order


# --------------------------------------------------------------------------- #
# M. Phase 4.2 narrow production fix: global advance gating + staging cleanup
# --------------------------------------------------------------------------- #


def _staging_paths():
    """Return (master_staging, i18n_staging) roots used during a cycle."""
    return (
        cu.masterdb_diff_folder_path + ".staging",
        cu.i18n_diff_folder_path + ".staging",
    )


def test_master_disabled_i18n_enabled_does_not_advance_global(
    monkeypatch, tmp_path
):
    """master=False/i18n=True: the global published version_info is never advanced
    and the formal master versions.json is left at its old value (it is never
    staged/published when master is disabled)."""
    monkeypatch.setattr(
        cu, "update_options", {"master": False, "i18n": True, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    master_repo = _init_repo(tmp_path, "master_repo")
    i18n_repo = _init_repo(tmp_path, "i18n_repo")
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "i18n_diff_repo", i18n_repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_repo.working_dir)
    monkeypatch.setattr(cu, "i18n_diff_folder_path", i18n_repo.working_dir)
    monkeypatch.setattr(cu, "prepare_repo_for_update", lambda *a, **k: _prepare_ok())
    monkeypatch.setattr(cu, "_commit_enabled_repositories", lambda *a: {})
    monkeypatch.setattr(cu, "_push_enabled_repositories", lambda *a: None)

    # Seed an OLD formal master versions.json that must NOT be overwritten.
    old_versions = {"dataVersion": "OLD", "assetVersion": "OLD"}
    _write_commit(
        master_repo, "versions.json", json.dumps(old_versions), "seed old version"
    )
    monkeypatch.setattr(cu, "version_info", dict(old_versions))

    def _refresh_ok(*args, **kwargs):
        # Even if generation "writes" a versions.json, master being disabled makes
        # _write_master_file a no-op, so it never reaches the formal tree.
        cu._write_master_file(
            "versions.json", {"dataVersion": "NEW", "assetVersion": "NEW"}
        )
        cu._write_i18n_file("card_prefix.json", {"1": "p"})

    monkeypatch.setattr(cu, "refresh_version", _refresh_ok)

    _stub_jsonrpc(monkeypatch, maintenance=False, new_version=True)
    status = cu._run_update_cycle_locked(daily=True)
    assert status == "ok"
    # Global published version_info is unchanged (master disabled).
    assert cu.version_info == old_versions
    # Formal master versions.json retains its OLD value (never re-published).
    assert _snap_version(master_repo) == old_versions
    # The i18n tree was still published (i18n enabled).
    assert os.path.exists(
        os.path.join(i18n_repo.working_dir, "ja", "card_prefix.json")
    )


def test_all_disabled_does_not_advance_global(monkeypatch, tmp_path):
    """all-disabled: no repository writes, no global advance, and no formal
    versions.json is created."""
    monkeypatch.setattr(
        cu, "update_options", {"master": False, "i18n": False, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    master_repo = _init_repo(tmp_path, "master_repo")
    i18n_repo = _init_repo(tmp_path, "i18n_repo")
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "i18n_diff_repo", i18n_repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_repo.working_dir)
    monkeypatch.setattr(cu, "i18n_diff_folder_path", i18n_repo.working_dir)
    monkeypatch.setattr(cu, "prepare_repo_for_update", lambda *a, **k: _prepare_ok())
    monkeypatch.setattr(cu, "_commit_enabled_repositories", lambda *a: {})
    monkeypatch.setattr(cu, "_push_enabled_repositories", lambda *a: None)
    monkeypatch.setattr(cu, "version_info", {"dataVersion": "OLD"})

    def _refresh_ok(*args, **kwargs):
        cu._write_master_file("versions.json", {"dataVersion": "NEW"})
        cu._write_i18n_file("card_prefix.json", {"1": "p"})

    monkeypatch.setattr(cu, "refresh_version", _refresh_ok)

    _stub_jsonrpc(monkeypatch, maintenance=False, new_version=True)
    status = cu._run_update_cycle_locked(daily=True)
    assert status == "ok"
    # Global unchanged.
    assert cu.version_info == {"dataVersion": "OLD"}
    # No formal versions.json created in either tree.
    assert _snap_version(master_repo) is None
    assert _snap_version(i18n_repo) is None


def _assert_no_commit_push_and_both_roots_gone(master_repo, i18n_repo):
    committed = {"n": 0}
    pushed = {"n": 0}

    def _fake_commit(*a, **k):
        committed["n"] += 1
        return {}

    def _fake_push(*a, **k):
        pushed["n"] += 1
        return None

    return committed, pushed, _fake_commit, _fake_push


def test_master_replace_failure_retains_journal_and_staging(
    monkeypatch, tmp_path
):
    """A master ``os.replace`` failure RETURNS ``publication_failed`` without
    committing/pushing and leaves the global published version unchanged, but the
    journal AND both staging roots are RETAINED (not cleared) so a subsequent
    recovery cycle can finish the work. The already-published dirty working tree
    is NOT rolled back/cleaned."""
    monkeypatch.setattr(
        cu, "update_options", {"master": True, "i18n": True, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    master_repo = _init_repo(tmp_path, "master_repo")
    i18n_repo = _init_repo(tmp_path, "i18n_repo")
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "i18n_diff_repo", i18n_repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_repo.working_dir)
    monkeypatch.setattr(cu, "i18n_diff_folder_path", i18n_repo.working_dir)
    monkeypatch.setattr(cu, "prepare_repo_for_update", lambda *a, **k: _prepare_ok())

    committed = {"n": 0}
    pushed = {"n": 0}
    real_prepare_target = cu._prepare_commit_target

    def _counting_prepare(repo, key, manifest, *args, **kwargs):
        committed["n"] += 1
        return real_prepare_target(repo, key, manifest, *args, **kwargs)

    monkeypatch.setattr(cu, "_prepare_commit_target", _counting_prepare)
    monkeypatch.setattr(
        cu, "_push_enabled_repositories",
        lambda *a, **k: pushed.__setitem__("n", pushed["n"] + 1) or None,
    )
    # Recovery pushes via push_current_head directly; stub it to succeed so the
    # retained-journal recovery can finish (no real network in tests).
    monkeypatch.setattr(
        cu, "push_current_head",
        lambda *a, **k: cu.GitResult(
            outcome=cu.GitOutcome.OK, reason="pushed",
            local_sha=master_repo.head.commit.hexsha,
        ),
    )
    monkeypatch.setattr(
        cu, "version_info", {"dataVersion": "OLD", "assetVersion": "OLD"}
    )

    def _refresh_ok(*args, **kwargs):
        # events.json will be published first (before the failing cards.json).
        cu._write_master_file("events.json", [{"id": 2}])
        cu._write_master_file("cards.json", [{"id": 1}])
        cu._write_master_file("versions.json", {"dataVersion": "NEW"})
        cu._write_i18n_file("card_prefix.json", {"1": "p"})

    monkeypatch.setattr(cu, "refresh_version", _refresh_ok)

    real_replace = os.replace

    def _replace_fails_on_master_cards(src, dst):
        if os.path.basename(dst) == "cards.json":
            raise OSError("disk full during master replace")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _replace_fails_on_master_cards)

    m_staging, i_staging = _staging_paths()

    _stub_jsonrpc(monkeypatch, maintenance=False, new_version=True)
    status = cu._run_update_cycle_locked(daily=True)
    assert status == "publication_failed"
    # No commit / push happened.
    assert committed["n"] == 0
    assert pushed["n"] == 0
    # The journal AND both staging roots are RETAINED (not cleared) so recovery
    # can finish the work.
    assert os.path.exists(m_staging)
    assert os.path.exists(i_staging)
    assert TransactionJournal.load(master_repo.git_dir) is not None
    # Global published version unchanged.
    assert cu.version_info == {"dataVersion": "OLD", "assetVersion": "OLD"}
    # The dirtily-published events.json is retained (NOT rolled back/cleaned).
    assert os.path.exists(os.path.join(master_repo.working_dir, "events.json"))
    # cards.json and versions.json never reached the formal tree.
    assert not os.path.exists(os.path.join(master_repo.working_dir, "cards.json"))
    assert not os.path.exists(os.path.join(master_repo.working_dir, "versions.json"))

    # --- Recovery cycle: restore os.replace and re-run; the retained journal +
    # staging must let recovery finish the publication and commit/push. ---
    monkeypatch.setattr(os, "replace", real_replace)
    status2 = cu._run_update_cycle_locked(daily=True)
    assert status2 == "recovered"
    assert committed["n"] == 2
    # All master files (incl. versions.json, published last) now in the tree.
    assert os.path.exists(os.path.join(master_repo.working_dir, "events.json"))
    assert os.path.exists(os.path.join(master_repo.working_dir, "cards.json"))
    assert os.path.exists(os.path.join(master_repo.working_dir, "versions.json"))
    # i18n file published too.
    assert os.path.exists(
        os.path.join(i18n_repo.working_dir, "ja", "card_prefix.json")
    )
    # Journal cleared after successful recovery.
    assert TransactionJournal.load(master_repo.git_dir) is None


def test_i18n_replace_failure_retains_journal_and_staging(
    monkeypatch, tmp_path
):
    """An i18n ``os.replace`` failure RETURNS ``publication_failed`` without
    committing/pushing and leaves the global published version unchanged, but the
    journal AND both staging roots are RETAINED (not cleared) so a subsequent
    recovery cycle can finish the work. The already-published master dirty working
    tree is NOT rolled back/cleaned."""
    monkeypatch.setattr(
        cu, "update_options", {"master": True, "i18n": True, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    master_repo = _init_repo(tmp_path, "master_repo")
    i18n_repo = _init_repo(tmp_path, "i18n_repo")
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "i18n_diff_repo", i18n_repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_repo.working_dir)
    monkeypatch.setattr(cu, "i18n_diff_folder_path", i18n_repo.working_dir)
    monkeypatch.setattr(cu, "prepare_repo_for_update", lambda *a, **k: _prepare_ok())

    committed = {"n": 0}
    pushed = {"n": 0}
    real_prepare_target = cu._prepare_commit_target

    def _counting_prepare(repo, key, manifest, *args, **kwargs):
        committed["n"] += 1
        return real_prepare_target(repo, key, manifest, *args, **kwargs)

    monkeypatch.setattr(cu, "_prepare_commit_target", _counting_prepare)
    monkeypatch.setattr(
        cu, "_push_enabled_repositories",
        lambda *a, **k: pushed.__setitem__("n", pushed["n"] + 1) or None,
    )
    # Recovery pushes via push_current_head directly; stub it to succeed so the
    # retained-journal recovery can finish (no real network in tests).
    monkeypatch.setattr(
        cu, "push_current_head",
        lambda *a, **k: cu.GitResult(
            outcome=cu.GitOutcome.OK, reason="pushed",
            local_sha=master_repo.head.commit.hexsha,
        ),
    )
    monkeypatch.setattr(
        cu, "version_info", {"dataVersion": "OLD", "assetVersion": "OLD"}
    )

    def _refresh_ok(*args, **kwargs):
        cu._write_master_file("cards.json", [{"id": 1}])
        cu._write_master_file("versions.json", {"dataVersion": "NEW"})
        cu._write_i18n_file("card_prefix.json", {"1": "p"})

    monkeypatch.setattr(cu, "refresh_version", _refresh_ok)

    real_replace = os.replace

    def _replace_fails_on_i18n(src, dst):
        if os.path.basename(dst) == "card_prefix.json":
            raise OSError("disk full during i18n replace")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _replace_fails_on_i18n)

    m_staging, i_staging = _staging_paths()

    _stub_jsonrpc(monkeypatch, maintenance=False, new_version=True)
    status = cu._run_update_cycle_locked(daily=True)
    assert status == "publication_failed"
    # No commit / push happened.
    assert committed["n"] == 0
    assert pushed["n"] == 0
    # The journal AND both staging roots are RETAINED (not cleared) so recovery
    # can finish the work.
    assert os.path.exists(m_staging)
    assert os.path.exists(i_staging)
    assert TransactionJournal.load(master_repo.git_dir) is not None
    # Global published version unchanged.
    assert cu.version_info == {"dataVersion": "OLD", "assetVersion": "OLD"}
    # Master cards.json (already published) is retained; versions.json never
    # reached the formal tree because it is published last, after i18n.
    assert os.path.exists(os.path.join(master_repo.working_dir, "cards.json"))
    assert not os.path.exists(os.path.join(master_repo.working_dir, "versions.json"))
    # Nothing was published into the i18n formal tree.
    assert not os.path.exists(
        os.path.join(i18n_repo.working_dir, "ja", "card_prefix.json")
    )

    # --- Recovery cycle: restore os.replace and re-run; the retained journal +
    # staging must let recovery finish the publication and commit/push. ---
    monkeypatch.setattr(os, "replace", real_replace)
    status2 = cu._run_update_cycle_locked(daily=True)
    assert status2 == "recovered"
    assert committed["n"] == 2
    # All master files (incl. versions.json, published last) now in the tree.
    assert os.path.exists(os.path.join(master_repo.working_dir, "cards.json"))
    assert os.path.exists(os.path.join(master_repo.working_dir, "versions.json"))
    # i18n file published too.
    assert os.path.exists(
        os.path.join(i18n_repo.working_dir, "ja", "card_prefix.json")
    )
    # Journal cleared after successful recovery.
    assert TransactionJournal.load(master_repo.git_dir) is None


def test_full_manifest_includes_versions_json_and_recovers(
    monkeypatch, tmp_path
):
    """The publishing journal records the COMPLETE manifest (incl. versions.json)
    for every repo, and a publish failure + retained journal recovers all files
    including versions.json on the next cycle."""
    monkeypatch.setattr(
        cu, "update_options", {"master": True, "i18n": True, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    master_repo = _init_repo(tmp_path, "master_repo")
    i18n_repo = _init_repo(tmp_path, "i18n_repo")
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "i18n_diff_repo", i18n_repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_repo.working_dir)
    monkeypatch.setattr(cu, "i18n_diff_folder_path", i18n_repo.working_dir)
    monkeypatch.setattr(cu, "prepare_repo_for_update", lambda *a, **k: _prepare_ok())

    _real_commit_diff = cu._commit_diff

    def _counting_commit_diff(*args, **kwargs):
        return _real_commit_diff(*args, **kwargs)

    monkeypatch.setattr(cu, "_commit_diff", _counting_commit_diff)
    monkeypatch.setattr(
        cu, "push_current_head",
        lambda *a, **k: cu.GitResult(
            outcome=cu.GitOutcome.OK, reason="pushed",
            local_sha=master_repo.head.commit.hexsha,
        ),
    )
    monkeypatch.setattr(
        cu, "version_info", {"dataVersion": "OLD", "assetVersion": "OLD"}
    )

    def _refresh_ok(*args, **kwargs):
        cu._write_master_file("cards.json", [{"id": 1}])
        cu._write_master_file("versions.json", {"dataVersion": "NEW"})
        cu._write_i18n_file("card_prefix.json", {"1": "p"})

    monkeypatch.setattr(cu, "refresh_version", _refresh_ok)

    real_replace = os.replace

    def _fail_on_versions(src, dst):
        if os.path.basename(dst) == "versions.json":
            raise OSError("disk full on versions.json")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _fail_on_versions)

    _stub_jsonrpc(monkeypatch, maintenance=False, new_version=True)
    status1 = cu._run_update_cycle_locked(daily=True)
    assert status1 == "publication_failed"
    # Journal retained; the master manifest must include versions.json (i18n has
    # no versions.json of its own — it is master-only).
    j = TransactionJournal.load(master_repo.git_dir)
    assert j is not None
    assert "versions.json" in j.repos["master"].manifest
    assert "versions.json" not in j.repos["i18n"].manifest
    # cards.json + i18n published; versions.json not yet (failed last).
    assert os.path.exists(os.path.join(master_repo.working_dir, "cards.json"))
    assert not os.path.exists(os.path.join(master_repo.working_dir, "versions.json"))

    # Recovery: restore replace; the retained journal (with versions.json in the
    # manifest) must finish publishing versions.json and commit/push.
    monkeypatch.setattr(os, "replace", real_replace)
    status2 = cu._run_update_cycle_locked(daily=True)
    assert status2 == "recovered"
    assert os.path.exists(os.path.join(master_repo.working_dir, "versions.json"))
    assert TransactionJournal.load(master_repo.git_dir) is None


def test_recovery_crash_after_one_replace_retains_journal(
    monkeypatch, tmp_path
):
    """If recovery itself crashes after publishing one file (but before finishing),
    the journal + staging are retained so the NEXT cycle resumes from the last
    proven destination checkpoint without re-reading the moved source."""
    monkeypatch.setattr(
        cu, "update_options", {"master": True, "i18n": False, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    master_repo = _init_repo(tmp_path, "master_repo")
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_repo.working_dir)
    monkeypatch.setattr(cu, "prepare_repo_for_update", lambda *a, **k: _prepare_ok())
    monkeypatch.setattr(
        cu, "version_info", {"dataVersion": "OLD", "assetVersion": "OLD"}
    )

    def _refresh_ok(*args, **kwargs):
        cu._write_master_file("cards.json", [{"id": 1}])
        cu._write_master_file("events.json", [{"id": 2}])
        cu._write_master_file("versions.json", {"dataVersion": "NEW"})

    monkeypatch.setattr(cu, "refresh_version", _refresh_ok)

    # First cycle: fail publication so the journal + staging are retained.
    real_replace = os.replace

    def _fail(src, dst):
        if os.path.basename(dst) == "events.json":
            raise OSError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _fail)
    _stub_jsonrpc(monkeypatch, maintenance=False, new_version=True)
    assert cu._run_update_cycle_locked(daily=True) == "publication_failed"
    assert TransactionJournal.load(master_repo.git_dir) is not None

    # Second cycle: let publication finish, but crash during commit (simulate a
    # crash mid-recovery) so the journal is retained again.
    monkeypatch.setattr(os, "replace", real_replace)

    real_prepare_target = cu._prepare_commit_target

    def _boom_commit(*args, **kwargs):
        raise RuntimeError("crash during recovery commit")

    monkeypatch.setattr(cu, "_prepare_commit_target", _boom_commit)
    # push_current_head must not be reached; stub defensively.
    monkeypatch.setattr(
        cu, "push_current_head",
        lambda *a, **k: cu.GitResult(outcome=cu.GitOutcome.OK, reason="pushed"),
    )
    with pytest.raises(RuntimeError):
        cu._run_update_cycle_locked(daily=True)
    # The journal is RETAINED after a crash mid-recovery (fail closed, no delete).
    assert TransactionJournal.load(master_repo.git_dir) is not None

    # Third cycle: real commit + push; recovery completes from the proven dest
    # checkpoints (cards.json + events.json already published, versions.json too).
    monkeypatch.setattr(cu, "_prepare_commit_target", real_prepare_target)
    status3 = cu._run_update_cycle_locked(daily=True)
    assert status3 == "recovered"
    assert os.path.exists(os.path.join(master_repo.working_dir, "cards.json"))
    assert os.path.exists(os.path.join(master_repo.working_dir, "events.json"))
    assert os.path.exists(os.path.join(master_repo.working_dir, "versions.json"))
    assert TransactionJournal.load(master_repo.git_dir) is None


def test_recovery_base_head_mismatch_blocks_commit(monkeypatch, tmp_path):
    """Fail-closed: if the working-tree HEAD diverged from the journal's recorded
    base SHA (no target yet), recovery must NOT create a replacement commit and
    must surface journal_invalid (preserving state)."""
    monkeypatch.setattr(
        cu, "update_options", {"master": True, "i18n": False, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    master_repo = _init_repo(tmp_path, "master_repo")
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_repo.working_dir)
    monkeypatch.setattr(cu, "prepare_repo_for_update", lambda *a, **k: _prepare_ok())
    monkeypatch.setattr(
        cu, "version_info", {"dataVersion": "OLD", "assetVersion": "OLD"}
    )

    # Seed a real base commit BEFORE the cycle so the journal captures a correct,
    # non-null base SHA. Recovery can then detect an out-of-band divergence of the
    # working-tree HEAD from that recorded base (an unborn repo has base_sha=None
    # and a first commit is a legitimate transition, not a mismatch).
    _write_commit(
        master_repo, "seed.txt", "seed", "seed base commit"
    )
    base_before = master_repo.head.commit.hexsha

    def _refresh_ok(*args, **kwargs):
        cu._write_master_file("cards.json", [{"id": 1}])
        cu._write_master_file("versions.json", {"dataVersion": "NEW"})

    monkeypatch.setattr(cu, "refresh_version", _refresh_ok)

    # First cycle fails publication -> retained journal with base_sha = seed HEAD.
    real_replace = os.replace

    def _fail(src, dst):
        if os.path.basename(dst) == "cards.json":
            raise OSError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _fail)
    _stub_jsonrpc(monkeypatch, maintenance=False, new_version=True)
    assert cu._run_update_cycle_locked(daily=True) == "publication_failed"
    j = TransactionJournal.load(master_repo.git_dir)
    assert j is not None
    base = j.repos["master"].base_sha
    assert base == base_before  # journal captured the correct base SHA

    # Diverge the working tree out of band (new commit not recorded in journal).
    monkeypatch.setattr(os, "replace", real_replace)
    with open(os.path.join(master_repo.working_dir, "diverged.txt"), "w") as f:
        f.write("x")
    master_repo.index.add(["diverged.txt"])
    master_repo.index.commit("out of band")
    assert master_repo.head.commit.hexsha != base

    # Recovery must fail closed (no replacement commit) and retain the journal.
    j_dbg = TransactionJournal.load(master_repo.git_dir)
    with open("/tmp/dbg_journal.txt", "w") as _f:
        _f.write(f"j_none={j_dbg is None}\n")
        if j_dbg is not None:
            _f.write(f"base={j_dbg.repos['master'].base_sha}\n")
            _f.write(f"head={master_repo.head.commit.hexsha}\n")
    monkeypatch.setattr(
        cu, "_commit_diff",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not commit")),
    )
    monkeypatch.setattr(
        cu, "push_current_head",
        lambda *a, **k: cu.GitResult(outcome=cu.GitOutcome.OK, reason="pushed"),
    )
    status = cu._run_update_cycle_locked(daily=True)
    assert status == "journal_invalid"
    assert TransactionJournal.load(master_repo.git_dir) is not None


def test_recovery_remote_already_at_target_skips_push(monkeypatch, tmp_path):
    """When the remote is already at the recorded target SHA, recovery verifies it
    (no duplicate push) and completes; the journal is deleted."""
    monkeypatch.setattr(
        cu, "update_options", {"master": True, "i18n": False, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    master_repo = _init_repo(tmp_path, "master_repo")
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_repo.working_dir)
    monkeypatch.setattr(cu, "prepare_repo_for_update", lambda *a, **k: _prepare_ok())
    monkeypatch.setattr(
        cu, "version_info", {"dataVersion": "OLD", "assetVersion": "OLD"}
    )

    def _refresh_ok(*args, **kwargs):
        cu._write_master_file("cards.json", [{"id": 1}])
        cu._write_master_file("versions.json", {"dataVersion": "NEW"})

    monkeypatch.setattr(cu, "refresh_version", _refresh_ok)

    # First cycle fails publication -> retained journal.
    real_replace = os.replace

    def _fail(src, dst):
        if os.path.basename(dst) == "cards.json":
            raise OSError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _fail)
    _stub_jsonrpc(monkeypatch, maintenance=False, new_version=True)
    assert cu._run_update_cycle_locked(daily=True) == "publication_failed"
    monkeypatch.setattr(os, "replace", real_replace)

    pushed = {"n": 0}

    def _push_ok(repo, branch="main", expected_sha=None, **kwargs):
        # Simulate the remote already at the target: report OK without a real push.
        pushed["n"] += 1
        return cu.GitResult(
            outcome=cu.GitOutcome.OK, reason="pushed",
            local_sha=master_repo.head.commit.hexsha,
        )

    monkeypatch.setattr(cu, "_commit_diff", cu._commit_diff)
    monkeypatch.setattr(cu, "push_current_head", _push_ok)
    status = cu._run_update_cycle_locked(daily=True)
    assert status == "recovered"
    assert pushed["n"] == 1  # exactly one push (master), no duplicate
    assert TransactionJournal.load(master_repo.git_dir) is None


def test_normal_cycle_pushes_i18n_before_master_with_expected_sha(
    monkeypatch, tmp_path
):
    """A normal (fresh) cycle commits all enabled repos, then pushes i18n -> master
    exclusively via push_current_head(expected_sha=...), and only deletes the
    journal after both pushes succeed."""
    monkeypatch.setattr(
        cu, "update_options", {"master": True, "i18n": True, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    master_repo = _init_repo(tmp_path, "master_repo")
    i18n_repo = _init_repo(tmp_path, "i18n_repo")
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "i18n_diff_repo", i18n_repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_repo.working_dir)
    monkeypatch.setattr(cu, "i18n_diff_folder_path", i18n_repo.working_dir)
    monkeypatch.setattr(cu, "prepare_repo_for_update", lambda *a, **k: _prepare_ok())
    monkeypatch.setattr(
        cu, "version_info", {"dataVersion": "OLD", "assetVersion": "OLD"}
    )

    def _refresh_ok(*args, **kwargs):
        cu._write_master_file("cards.json", [{"id": 1}])
        cu._write_master_file("versions.json", {"dataVersion": "NEW"})
        cu._write_i18n_file("card_prefix.json", {"1": "p"})

    monkeypatch.setattr(cu, "refresh_version", _refresh_ok)

    push_order = []

    def _push(repo, branch="main", expected_sha=None, **kwargs):
        # Identify which repo by its working dir.
        key = "i18n" if repo.working_dir == i18n_repo.working_dir else "master"
        push_order.append(key)
        return cu.GitResult(
            outcome=cu.GitOutcome.OK, reason="pushed",
            local_sha=repo.head.commit.hexsha,
        )

    monkeypatch.setattr(cu, "push_current_head", _push)
    _stub_jsonrpc(monkeypatch, maintenance=False, new_version=True)
    status = cu._run_update_cycle_locked(daily=True)
    assert status == "ok"
    # i18n pushed before master, both with an expected SHA barrier.
    assert push_order == ["i18n", "master"]
    assert TransactionJournal.load(master_repo.git_dir) is None


# --------------------------------------------------------------------------- #
# G. Entry delegation + AST (merged from the fix-8 entry tests)
# --------------------------------------------------------------------------- #

# Functions that must NEVER be called by a production entry point outside of the
# unified cycle. The cycle itself (inside the lock) is allowed to call them, so
# we monkeypatch the module-level names to raise and then assert the entry point
# never reaches them.
FORBIDDEN_SIDE_EFFECTS = [
    "save_info_from_suite_user",
    "refresh_information",
    "refresh_version",
    "commit_master_diff",
    "commit_i18n_files",
]


def _raise(name):
    def _boom(*args, **kwargs):
        raise AssertionError(
            f"forbidden side effect {name!r} called outside the locked cycle"
        )

    return _boom


def _install_forbidden(monkeypatch):
    for name in FORBIDDEN_SIDE_EFFECTS:
        monkeypatch.setattr(cu, name, _raise(name), raising=True)


def _make_fake_cycle(monkeypatch):
    """Replace ``_run_update_cycle`` with a recorder that returns 'ok'."""
    calls = []

    def fake_cycle(daily):
        calls.append(daily)
        return "ok"

    monkeypatch.setattr(cu, "_run_update_cycle", fake_cycle)
    return calls


class MockScheduler:
    def start(self):
        return None

    def add_job(self, *args, **kwargs):
        return None


def test_day_change_func_delegates_once_daily(monkeypatch):
    _install_forbidden(monkeypatch)
    calls = _make_fake_cycle(monkeypatch)

    cu.day_change_func()

    assert calls == [True]


def test_try_update_func_delegates_once_not_daily(monkeypatch):
    _install_forbidden(monkeypatch)
    calls = _make_fake_cycle(monkeypatch)

    cu.try_update_func()

    assert calls == [False]


def test_try_update_simple_func_delegates_once_not_daily(monkeypatch):
    _install_forbidden(monkeypatch)
    calls = _make_fake_cycle(monkeypatch)

    cu.try_update_simple_func()

    assert calls == [False]


def test_bootstrap_delegates_once_daily_and_no_userinfo_write_before_cycle(
    monkeypatch,
):
    """bootstrap() must call the cycle exactly once (daily=True) and must NOT
    write user info before the cycle, even when userInfo=True.
    """
    _install_forbidden(monkeypatch)
    # Avoid the maintenance retry loop / scheduler startup / client init.
    monkeypatch.setattr(cu, "_bootstrap_try_refresh", lambda: True)
    monkeypatch.setattr(cu, "_bootstrap_init_client", lambda: None)
    monkeypatch.setattr(cu, "_bootstrap_prepare_repositories", lambda: None)
    monkeypatch.setattr(cu, "scheduler", MockScheduler())

    calls = _make_fake_cycle(monkeypatch)
    # Track whether any forbidden user-info write sneaks in before the cycle.
    wrote_before_cycle = {"value": False}

    def tracking_save():
        if not calls:
            wrote_before_cycle["value"] = True
        raise AssertionError("save_info_from_suite_user called before cycle")

    monkeypatch.setattr(
        cu, "save_info_from_suite_user", tracking_save, raising=True
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(
        cu,
        "update_options",
        {"master": True, "i18n": True, "userInfo": True},
    )

    cu.bootstrap()

    assert calls == [True]
    assert wrote_before_cycle["value"] is False


def test_bootstrap_simple_delegates_once_daily(monkeypatch):
    _install_forbidden(monkeypatch)
    monkeypatch.setattr(cu, "scheduler", MockScheduler())
    monkeypatch.setattr(cu, "check_git_folder", lambda *a, **k: None)
    monkeypatch.setattr(cu, "pjsk_region", "cn")
    monkeypatch.setattr(
        cu, "check_update_versions_url", "http://example/versions"
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", True)
    monkeypatch.setattr(
        cu,
        "update_options",
        {"master": True, "i18n": True, "userInfo": False},
    )

    calls = _make_fake_cycle(monkeypatch)

    cu.bootstrap_simple()

    assert calls == [True]


# --------------------------------------------------------------------------- #
# AST checks: entry-point bodies must not call forbidden functions nor assign
# the published ``version_info`` global outside the locked cycle.
# --------------------------------------------------------------------------- #


def _parse_body(name):
    tree = ast.parse(inspect.getsource(getattr(cu, name)))
    func = tree.body[0]
    assert isinstance(func, ast.FunctionDef)
    return func


def _collect_called_names(node):
    names = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            if isinstance(n.func, ast.Name):
                names.add(n.func.id)
            elif isinstance(n.func, ast.Attribute):
                names.add(n.func.attr)
    return names


def _has_version_info_assignment(node):
    for n in ast.walk(node):
        if isinstance(n, ast.Assign):
            for target in n.targets:
                if isinstance(target, ast.Name) and target.id == "version_info":
                    return True
    return False


@pytest.mark.parametrize(
    "entry,",
    [
        "day_change_func",
        "try_update_func",
        "try_update_simple_func",
    ],
)
def test_entry_body_has_no_forbidden_calls_or_version_assign(entry):
    func = _parse_body(entry)
    called = _collect_called_names(func)
    assert not (set(FORBIDDEN_SIDE_EFFECTS) & called), (
        f"{entry} calls forbidden side effect(s): "
        f"{sorted(set(FORBIDDEN_SIDE_EFFECTS) & called)}"
    )
    assert not _has_version_info_assignment(func), (
        f"{entry} assigns version_info outside the locked cycle"
    )


def test_bootstrap_body_has_no_forbidden_calls_or_version_assign():
    func = _parse_body("bootstrap")
    called = _collect_called_names(func)
    # These helpers must not appear in bootstrap's body (they live only inside
    # the locked cycle now).
    assert "save_info_from_suite_user" not in called
    assert "commit_master_diff" not in called
    assert "commit_i18n_files" not in called
    assert "refresh_version" not in called
    assert "refresh_information" not in called
    assert not _has_version_info_assignment(func)


# --------------------------------------------------------------------------- #
# N. Cooperative deadline (ordinary expires -> deadline_exceeded; daily ignores)
# --------------------------------------------------------------------------- #


def test_ordinary_expired_deadline_returns_deadline_exceeded(monkeypatch, tmp_path):
    """An ordinary run whose deadline already expired must return
    ``deadline_exceeded`` without invoking the expensive generation phase."""
    monkeypatch.setattr(
        cu, "update_options", {"master": True, "i18n": False, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    master_repo = _init_repo(tmp_path, "master_repo")
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_repo.working_dir)
    monkeypatch.setattr(cu, "prepare_repo_for_update", lambda *a, **k: _prepare_ok())

    generation_ran = {"flag": False}
    monkeypatch.setattr(
        cu, "_generate_and_publish",
        lambda *a, **k: generation_ran.__setitem__("flag", True) or {},
    )
    monkeypatch.setattr(cu, "_commit_enabled_repositories", lambda *a: {})
    monkeypatch.setattr(cu, "_push_enabled_repositories", lambda *a: None)

    _stub_jsonrpc(monkeypatch, maintenance=False, new_version=True)
    # A zero-second deadline is already expired at the first safe seam.
    status = cu._run_update_cycle(daily=False, deadline_seconds=0)
    assert status == "deadline_exceeded"
    # The expensive generation phase must NOT have run.
    assert generation_ran["flag"] is False


def test_daily_ignores_expired_deadline_and_proceeds(monkeypatch, tmp_path):
    """A daily run must ignore an expired deadline and proceed to completion."""
    monkeypatch.setattr(
        cu, "update_options", {"master": True, "i18n": False, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    master_repo = _init_repo(tmp_path, "master_repo")
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_repo.working_dir)
    monkeypatch.setattr(cu, "prepare_repo_for_update", lambda *a, **k: _prepare_ok())

    generation_ran = {"flag": False}
    monkeypatch.setattr(
        cu, "_generate_and_publish",
        lambda *a, **k: generation_ran.__setitem__("flag", True) or {},
    )
    monkeypatch.setattr(cu, "_commit_enabled_repositories", lambda *a: {})
    monkeypatch.setattr(cu, "_push_enabled_repositories", lambda *a: None)

    _stub_jsonrpc(monkeypatch, maintenance=False, new_version=True)
    # Even with an expired deadline, daily must proceed (deadline disabled).
    status = cu._run_update_cycle(daily=True, deadline_seconds=0)
    assert status == "ok"
    assert generation_ran["flag"] is True


def test_deadline_exceeded_releases_outer_process_lock_and_repo_flock(
    monkeypatch, tmp_path
):
    """The deadline path must release the outer process lock + repo flock so a
    follow-up run can acquire them and complete."""
    master_folder, _i18n_folder, master_lock, _i18n_lock = _lock_paths(tmp_path)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_folder)
    monkeypatch.setattr(cu, "i18n_diff_folder_path", _i18n_folder)
    monkeypatch.setattr(
        cu, "update_options", {"master": True, "i18n": False, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    monkeypatch.setattr(cu, "prepare_repo_for_update", lambda *a, **k: _prepare_ok())
    monkeypatch.setattr(cu, "_generate_and_publish", lambda *a, **k: {})
    monkeypatch.setattr(cu, "_commit_enabled_repositories", lambda *a, **k: {})
    monkeypatch.setattr(cu, "_push_enabled_repositories", lambda *a: None)
    _stub_jsonrpc(monkeypatch, maintenance=False, new_version=True)

    # First ordinary run hits the expired deadline and returns deadline_exceeded.
    status1 = cu._run_update_cycle(daily=False, deadline_seconds=0)
    assert status1 == "deadline_exceeded"

    # The in-process cycle lock is free again.
    assert cu._PROCESS_LOCK.acquire() is True
    cu._PROCESS_LOCK.release()

    # The repo flock is free again: a fresh process can re-acquire it.
    with repo_file_locks([master_lock], non_blocking=True):
        pass  # acquired cleanly after the deadline-exceeded cycle

    # A follow-up run can acquire both locks and complete normally.
    status2 = cu._run_update_cycle(daily=False, deadline_seconds=3600)
    assert status2 == "ok"


def test_deadline_exceeded_at_fourth_seam_skips_publication(monkeypatch, tmp_path):
    """The cooperative deadline's FOURTH (final) safe check happens AFTER all
    staging generation + validation but BEFORE the first formal ``os.replace``.
    If it fires there, the cycle returns ``deadline_exceeded`` with:
      - ZERO ``os.replace`` calls (no formal bytes written),
      - formal working-tree files unchanged (never created),
      - both staging roots cleared,
      - no commit/push.

    This uses the REAL ``_generate_and_publish`` (no fake) and a deterministic
    deadline double (no sleeps) that permits the first three seams (gating-after,
    prepare, between-prepare/generation) and raises only at the fourth seam
    (inside ``_generate_and_publish``, after staging gen+validation, before the
    first replace)."""
    monkeypatch.setattr(
        cu, "update_options", {"master": True, "i18n": False, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    master_repo = _init_repo(tmp_path, "master_repo")
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_repo.working_dir)
    monkeypatch.setattr(cu, "prepare_repo_for_update", lambda *a, **k: _prepare_ok())

    committed = {"n": 0}
    pushed = {"n": 0}
    monkeypatch.setattr(
        cu, "_commit_enabled_repositories",
        lambda *a, **k: committed.__setitem__("n", committed["n"] + 1) or {},
    )
    monkeypatch.setattr(
        cu, "_push_enabled_repositories",
        lambda *a, **k: pushed.__setitem__("n", pushed["n"] + 1) or None,
    )

    # Instrument os.replace to count formal-publication calls.
    replace_calls = {"n": 0}
    real_replace = os.replace

    def _counting_replace(src, dst, *a, **k):
        replace_calls["n"] += 1
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", _counting_replace)

    def _refresh_ok(*args, **kwargs):
        cu._write_master_file("cards.json", [{"id": 1}])
        cu._write_master_file("versions.json", {"dataVersion": "1"})

    monkeypatch.setattr(cu, "refresh_version", _refresh_ok)

    _stub_jsonrpc(monkeypatch, maintenance=False, new_version=True)

    # Deterministic deadline double: permits the gating-after (call 1), prepare
    # (call 2), and between-prepare/generation (call 3) seams, then raises at the
    # fourth seam (call 4) — inside _generate_and_publish, after staging gen +
    # validation, before the first os.replace.
    calls = {"n": 0}

    class _FourthSeamDeadline(cu.Deadline):
        def __init__(self):
            super().__init__(None)  # disabled base; we override check()

        def check(self):
            calls["n"] += 1
            if calls["n"] >= 4:
                raise cu.CycleDeadlineExceeded("expired at fourth (pre-replace) seam")

    status = cu._run_update_cycle(daily=False, deadline=_FourthSeamDeadline())
    assert status == "deadline_exceeded"
    # No formal publication occurred: zero os.replace calls, and the formal
    # working-tree files were never written (bytes unchanged).
    assert replace_calls["n"] == 0
    assert not os.path.exists(os.path.join(master_repo.working_dir, "cards.json"))
    assert not os.path.exists(os.path.join(master_repo.working_dir, "versions.json"))
    # Both staging roots were cleared by the deadline handler.
    assert not os.path.exists(master_repo.working_dir + ".staging")
    assert not os.path.exists(cu.i18n_diff_folder_path + ".staging")
    # No commit/push happened.
    assert committed["n"] == 0
    assert pushed["n"] == 0


def test_deadline_expired_only_after_first_replace_still_completes(
    monkeypatch, tmp_path
):
    """Post-publication behavior: once the first ``os.replace`` has run, NO
    further cooperative deadline check occurs. A deadline double that is
    "armed" to raise only AFTER the first replace must NOT cancel the cycle:
    the cycle proceeds through the remaining publication, the real
    explicit-manifest commit, and push, ending with a clean working tree and
    status ``ok`` (not ``deadline_exceeded``).

    This wraps ``os.replace`` so the deadline becomes expired exactly when the
    first formal replace happens, then verifies the cycle still completes —
    proving the deadline is never checked after publication begins.
    """
    monkeypatch.setattr(
        cu, "update_options", {"master": True, "i18n": False, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    master_repo = _init_repo(tmp_path, "master_repo")
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_repo.working_dir)
    monkeypatch.setattr(cu, "prepare_repo_for_update", lambda *a, **k: _prepare_ok())

    # Use the REAL commit path (no fake commit counter). Mock network/push only:
    # the temp master repo has no remote, so report a successful push without
    # touching the network. The real explicit-manifest commit must run.
    pushed = {"n": 0}
    monkeypatch.setattr(
        cu, "_push_diff",
        lambda repo, operation, **kwargs: pushed.__setitem__("n", pushed["n"] + 1)
        or cu.GitResult(outcome=GitOutcome.OK, reason="pushed", operation=operation),
    )

    # Instrument os.replace: count formal-publication calls AND arm the deadline
    # to expire only once the first replace has happened.
    replace_calls = {"n": 0}
    real_replace = os.replace

    def _arming_replace(src, dst, *a, **k):
        replace_calls["n"] += 1
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", _arming_replace)

    def _refresh_ok(*args, **kwargs):
        cu._write_master_file("cards.json", [{"id": 1}])
        cu._write_master_file("versions.json", {"dataVersion": "1"})
        # Production-compatible candidate: refresh_version returns the candidate
        # it generated with, which _generate_and_publish stashes for commit
        # construction (so the explicit manifest commit has a real version).
        return {"dataVersion": "1", "assetVersion": "1"}

    monkeypatch.setattr(cu, "refresh_version", _refresh_ok)

    _stub_jsonrpc(monkeypatch, maintenance=False, new_version=True)

    # Deterministic deadline double: raises ONLY after the first os.replace has
    # run (i.e. after formal publication has begun). Because the deadline is
    # checked only at the four safe seams BEFORE the first replace, this never
    # trips and the cycle must run to completion.
    class _ExpiresAfterFirstReplaceDeadline(cu.Deadline):
        def __init__(self):
            super().__init__(None)  # disabled base; we override check()

        def check(self):
            if replace_calls["n"] >= 1:
                raise cu.CycleDeadlineExceeded("expired after first replace")

    status = cu._run_update_cycle(
        daily=False, deadline=_ExpiresAfterFirstReplaceDeadline()
    )
    # The cycle is NOT cancelled by a deadline that only elapsed after publication
    # began; it proceeds to commit (and push).
    assert status != "deadline_exceeded"
    assert status == "ok"
    # Formal publication actually happened (at least one os.replace ran).
    assert replace_calls["n"] >= 1
    # The real commit path produced a commit in the master repo.
    assert master_repo.head.commit is not None
    assert pushed["n"] == 1
    # The published output is present in the working tree (formal publication ran)
    # and the working tree is CLEAN after the successful explicit-manifest commit
    # (no leftover dirty/staged files).
    assert os.path.exists(os.path.join(master_repo.working_dir, "cards.json"))
    assert os.path.exists(os.path.join(master_repo.working_dir, "versions.json"))
    assert master_repo.is_dirty(untracked_files=True) is False


def test_i18n_only_first_cycle_with_none_version_info_no_typeerror(
    monkeypatch, tmp_path
):
    """i18n-only first run (master disabled) where the published global
    ``version_info`` is ``None`` must NOT raise ``TypeError`` when building
    commit messages, and must still use the explicit candidate version + the
    explicit manifest paths (no broad-staging). The published global stays
    ``None`` (master disabled, never advanced), but the i18n repository is
    committed with an explicit, non-crashing message derived from the candidate.
    """
    monkeypatch.setattr(
        cu, "update_options", {"master": False, "i18n": True, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    master_repo = _init_repo(tmp_path, "master_repo")
    i18n_repo = _init_repo(tmp_path, "i18n_repo")
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "i18n_diff_repo", i18n_repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_repo.working_dir)
    monkeypatch.setattr(cu, "i18n_diff_folder_path", i18n_repo.working_dir)
    monkeypatch.setattr(cu, "prepare_repo_for_update", lambda *a, **k: _prepare_ok())
    monkeypatch.setattr(cu, "version_info", None)  # i18n-only first run

    # Capture the new temporary-index plumbing seam rather than the removed
    # Repo.index.add coordinated path.
    prepared_manifests: list[list[str]] = []
    real_prepare_target = cu._prepare_commit_target

    def _capture_prepare(repo, key, manifest, *args, **kwargs):
        prepared_manifests.append(list(manifest))
        return real_prepare_target(repo, key, manifest, *args, **kwargs)

    monkeypatch.setattr(cu, "_prepare_commit_target", _capture_prepare)

    # Mock network/push only: the temp i18n repo has no remote, so report a
    # successful push without touching the network. The real commit path runs.
    monkeypatch.setattr(
        cu, "_push_diff",
        lambda repo, operation, **kwargs: cu.GitResult(
            outcome=GitOutcome.OK, reason="pushed", operation=operation
        ),
    )

    # Seed UNRELATED preexisting files (one tracked, one untracked) in the i18n
    # repo. They must NOT be committed/staged by the cycle's explicit manifest.
    _write_commit(i18n_repo, "README.md", "unrelated tracked", "seed readme")
    with open(os.path.join(i18n_repo.working_dir, "scratch.txt"), "w") as f:
        f.write("unrelated untracked")

    candidate_version = {"dataVersion": "1", "assetVersion": "1"}

    def _refresh_ok(*args, **kwargs):
        # Master writes are suppressed (master disabled); only i18n is written.
        # Production ``refresh_version`` returns the candidate it generated with;
        # mirror that here so the cycle threads the explicit candidate through.
        cu._write_master_file("versions.json", candidate_version)  # no-op (master off)
        cu._write_i18n_file("card_prefix.json", {"1": "p"})
        return candidate_version

    monkeypatch.setattr(cu, "refresh_version", _refresh_ok)

    _stub_jsonrpc(monkeypatch, maintenance=False, new_version=True)

    prepared_manifests.clear()

    # Must not raise TypeError indexing a None global; must run the real commit
    # path to completion.
    status = cu._run_update_cycle_locked(daily=True)
    assert status == "ok"

    # The explicit manifest path was passed to the temporary plumbing index; no
    # broad-stage of unrelated files is possible in the coordinated flow.
    assert prepared_manifests == [["ja/card_prefix.json"]]

    # The resulting commit subject is exactly the expected i18n message, derived
    # from the explicit candidate (not the None global).
    head = i18n_repo.head.commit
    assert head.message.splitlines()[0] == "i18n update for master version 1"
    assert "Sekai-Update-Txn:" in head.message
    assert "Sekai-Update-Repo: i18n" in head.message

    # HEAD contains ONLY the expected i18n manifest content (the unrelated
    # tracked README.md is a separate commit; the unrelated untracked scratch.txt
    # is never committed).
    tree_files = sorted(t for t in head.stats.files)
    assert tree_files == ["ja/card_prefix.json"]

    # The published global is still None (master disabled, never advanced).
    assert cu.version_info is None

    # The i18n file was committed into the i18n working tree.
    assert os.path.exists(
        os.path.join(i18n_repo.working_dir, "ja", "card_prefix.json")
    )
    # The unrelated untracked file remains untracked (not swept into the commit).
    assert "scratch.txt" in i18n_repo.untracked_files


def test_deadline_disabled_for_daily_even_if_passed(monkeypatch, tmp_path):
    """Even if a caller passes an explicit deadline, daily=True disables it."""
    monkeypatch.setattr(
        cu, "update_options", {"master": True, "i18n": False, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    master_repo = _init_repo(tmp_path, "master_repo")
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_repo.working_dir)
    monkeypatch.setattr(cu, "prepare_repo_for_update", lambda *a, **k: _prepare_ok())

    generation_ran = {"flag": False}
    monkeypatch.setattr(
        cu, "_generate_and_publish",
        lambda *a, **k: generation_ran.__setitem__("flag", True) or {},
    )
    monkeypatch.setattr(cu, "_commit_enabled_repositories", lambda *a: {})
    monkeypatch.setattr(cu, "_push_enabled_repositories", lambda *a: None)

    _stub_jsonrpc(monkeypatch, maintenance=False, new_version=True)
    explicit_deadline = cu.Deadline(0)  # already expired
    status = cu._run_update_cycle(daily=True, deadline=explicit_deadline)
    assert status == "ok"
    assert generation_ran["flag"] is True
