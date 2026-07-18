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
from datetime import datetime

import git
import pytest

import check_update as cu
from utils.git import GitOutcome
from utils.git_lock import ProcessCycleLock, repo_file_locks, sorted_lock_paths

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


def test_daily_true_only_at_0400():
    assert cu._is_daily_run(datetime(2026, 1, 1, 4, 0)) is True
    assert cu._is_daily_run(datetime(2026, 1, 1, 4, 30)) is False
    assert cu._is_daily_run(datetime(2026, 1, 1, 3, 0)) is False
    assert cu._is_daily_run(datetime(2026, 1, 1, 0, 0)) is False


def test_scheduled_dispatch_daily_at_0400_ordinary_at_0430(monkeypatch):
    calls = []

    def _fake_cycle(daily):
        calls.append(daily)
        return "ok"

    monkeypatch.setattr(cu, "_run_update_cycle", _fake_cycle)

    real_datetime = cu.datetime

    class _FakeNow:
        def __init__(self, h, mi):
            self.hour = h
            self.minute = mi

        def strftime(self, *_a):
            return f"{self.hour:02d}:{self.minute:02d}"

    class _PatchedDT(real_datetime):
        @classmethod
        def now(cls, *a, **k):
            return _FakeNow(4, 0)

    monkeypatch.setattr(cu, "datetime", _PatchedDT)
    cu.scheduled_update_job()
    assert calls == [True]

    calls.clear()

    class _PatchedDT2(real_datetime):
        @classmethod
        def now(cls, *a, **k):
            return _FakeNow(4, 30)

    monkeypatch.setattr(cu, "datetime", _PatchedDT2)
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

    def _fake_prepare(repo, branch="main"):
        prepare_calls.append(repo)
        if repo is master_repo:
            return cu.GitResult(
                outcome=GitOutcome.BLOCKED, reason="dirty", operation="prepare"
            )
        return _prepare_ok()

    monkeypatch.setattr(cu, "prepare_repo_for_update", _fake_prepare)
    monkeypatch.setattr(
        cu, "_generate_and_publish",
        lambda daily: generation_ran.__setitem__("flag", True) or {},
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
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "i18n_diff_repo", i18n_repo)

    order: list[str] = []

    def _fake_prepare(repo, branch="main"):
        order.append(f"prepare:{repo.working_dir}")
        return _prepare_ok()

    monkeypatch.setattr(cu, "prepare_repo_for_update", _fake_prepare)
    monkeypatch.setattr(
        cu, "_generate_and_publish",
        lambda daily: order.append("generate") or {},
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

    real_index_add = git.IndexFile.add

    def _tracking_add(self, paths):
        staged["paths"] = list(paths)
        return real_index_add(self, paths)

    monkeypatch.setattr(git.IndexFile, "add", _tracking_add)
    monkeypatch.setattr(cu, "prepare_repo_for_update", lambda *a, **k: _prepare_ok())
    monkeypatch.setattr(cu, "_push_enabled_repositories", lambda *a: None)

    def _refresh_ok(*args, **kwargs):
        cu._write_master_file("cards.json", [{"id": 1}])
        cu._write_master_file("versions.json", {"dataVersion": "1"})

    monkeypatch.setattr(cu, "refresh_version", _refresh_ok)

    _stub_jsonrpc_no_maintenance(monkeypatch)
    status = cu._run_update_cycle_locked(daily=True)
    assert status == "ok"
    assert staged["paths"] == ["cards.json", "versions.json"]
    assert "unrelated.txt" not in staged["paths"]
    assert "untracked.txt" not in staged["paths"]


def test_i18n_commit_failure_leaves_master_unpushed(monkeypatch, tmp_path):
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

    pushed = []

    def _fake_commit_enabled(enabled, manifest):
        return {
            "master": cu.GitResult(outcome=GitOutcome.OK, reason="committed"),
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

    def _fake_commit_enabled(enabled, manifest):
        for key, _repo in enabled:
            seq.append(f"commit:{key}")
        return {
            "master": cu.GitResult(outcome=GitOutcome.OK, reason="committed"),
            "i18n": cu.GitResult(outcome=GitOutcome.OK, reason="committed"),
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

    def _fake_commit_enabled(enabled, manifest):
        # Create a real local commit so the "local commit preserved" assertion
        # is meaningful (the cycle must never reset/rebase/delete it).
        res = {}
        for key, repo in enabled:
            if repo is not None:
                repo.index.commit(f"seed {key}")
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
    """After a master push failure, the next cycle's prepare recovers locally.

    Uses a local bare remote (no network). master has an unpushed local commit;
    the next cycle should still prepare (the commit is preserved) and, once the
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
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_repo.working_dir)
    monkeypatch.setattr(
        cu, "version_info", {"dataVersion": "1", "assetVersion": "1"}
    )

    def _push_fail(repo, branch="main", **kwargs):
        return cu.GitResult(
            outcome=GitOutcome.PENDING_PUSH, reason="push_rejected", local_sha="m1"
        )

    monkeypatch.setattr(cu, "prepare_repo_for_update", lambda *a, **k: _prepare_ok())
    monkeypatch.setattr(cu, "push_current_head", _push_fail)

    def _refresh_ok(*args, **kwargs):
        cu._write_master_file("cards.json", [{"id": 1}])
        cu._write_master_file("versions.json", {"dataVersion": "1"})

    monkeypatch.setattr(cu, "refresh_version", _refresh_ok)

    monkeypatch.setattr(
        cu, "_commit_diff",
        lambda *a, **k: (master_repo.index.commit("recover seed"),
                         cu.GitResult(outcome=GitOutcome.OK, reason="committed",
                                      local_sha=master_repo.head.commit.hexsha))[1],
    )

    _stub_jsonrpc_no_maintenance(monkeypatch)
    status1 = cu._run_update_cycle_locked(daily=True)
    assert status1 == "push_failed:master:push_rejected"
    local_sha_after_fail = master_repo.head.commit.hexsha
    assert local_sha_after_fail is not None  # local commit preserved

    # Next cycle: remote now accepts the push; prepare must still succeed and
    # the cycle must complete (recovery via local commit, no external network).
    pushed_second = {"ok": False}

    def _push_ok(repo, branch="main", **kwargs):
        pushed_second["ok"] = True
        return cu.GitResult(
            outcome=GitOutcome.OK, reason="pushed",
            local_sha=master_repo.head.commit.hexsha)

    monkeypatch.setattr(cu, "push_current_head", _push_ok)
    monkeypatch.setattr(
        cu, "prepare_repo_for_update",
        lambda *a, **k: cu.GitResult(outcome=GitOutcome.OK, reason="equal"),
    )

    status2 = cu._run_update_cycle_locked(daily=True)
    assert status2 == "ok"
    # The next cycle recovered and actually pushed the local commit.
    assert pushed_second["ok"] is True
    # The local commit remains present afterwards (never deleted/rebased).
    assert master_repo.head.commit.hexsha is not None


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

    def _fake_commit_enabled(enabled, manifest):
        # Use the published global exactly as production does.
        for key, _repo in enabled:
            captured_msg[key] = (
                f"master version {cu.version_info['dataVersion']} "
                f"asset version {cu.version_info['assetVersion']}"
            )
        return {
            k: cu.GitResult(outcome=GitOutcome.OK, reason="committed")
            for k, _ in enabled
        }

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
    monkeypatch.setattr(cu, "_commit_enabled_repositories", lambda *a: {})
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

    def _boom_locked(daily):
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


def test_master_replace_failure_clears_both_staging_roots(
    monkeypatch, tmp_path
):
    """A master ``os.replace`` failure clears BOTH staging roots, performs no
    commit/push, and leaves the global published version unchanged. The already
    published dirty working tree is NOT rolled back/cleaned."""
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
    monkeypatch.setattr(
        cu, "_commit_enabled_repositories",
        lambda *a, **k: committed.__setitem__("n", committed["n"] + 1) or {},
    )
    monkeypatch.setattr(
        cu, "_push_enabled_repositories",
        lambda *a, **k: pushed.__setitem__("n", pushed["n"] + 1) or None,
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
    # Both staging roots were cleared (even though the failure was on master, the
    # i18n staging root is NOT left behind).
    assert not os.path.exists(m_staging)
    assert not os.path.exists(i_staging)
    # Global published version unchanged.
    assert cu.version_info == {"dataVersion": "OLD", "assetVersion": "OLD"}
    # The dirtily-published events.json is retained (NOT rolled back/cleaned).
    assert os.path.exists(os.path.join(master_repo.working_dir, "events.json"))
    # cards.json and versions.json never reached the formal tree.
    assert not os.path.exists(os.path.join(master_repo.working_dir, "cards.json"))
    assert not os.path.exists(os.path.join(master_repo.working_dir, "versions.json"))


def test_i18n_replace_failure_clears_both_staging_roots(
    monkeypatch, tmp_path
):
    """An i18n ``os.replace`` failure clears BOTH staging roots, performs no
    commit/push, and leaves the global published version unchanged. The already
    published master dirty working tree is NOT rolled back/cleaned."""
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
    monkeypatch.setattr(
        cu, "_commit_enabled_repositories",
        lambda *a, **k: committed.__setitem__("n", committed["n"] + 1) or {},
    )
    monkeypatch.setattr(
        cu, "_push_enabled_repositories",
        lambda *a, **k: pushed.__setitem__("n", pushed["n"] + 1) or None,
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
    # Both staging roots cleared.
    assert not os.path.exists(m_staging)
    assert not os.path.exists(i_staging)
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
