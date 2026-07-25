"""Focused tests for Phase 4.1 Git primitives (second review round).

All tests use only local temporary repositories / bare remotes; no GitHub and
no real push to production. Covers:

- ``push_current_head``: explicit ``HEAD:refs/heads/main`` refspec; pre-push SHA
  capture; ``expected_sha`` guard (remote unchanged); ``require_remote_branch``
  refusing to auto-create ``main``; per-ref error flags (REJECTED etc.) ->
  stable ``PENDING_PUSH`` / ``push_rejected``; ``PushInfoList.raise_if_error``
  backstop; empty push result; post-fetch SHA verification; unborn HEAD.
- ``prepare_repo_for_update``: prune fetch; ``missing_remote_branch`` when the
  remote ``main`` is deleted / stale tracking ref; true shared-base behind; ff
  failure on operation ``fast_forward``; fetch failure on operation ``fetch``;
  equal / ahead (pushes same SHA) / diverged; local-unborn + remote exists /
  both missing; structural BLOCKED; clean-ahead recovery via a temporary
  pre-receive hook that proves the *same* SHA is pushed and the full commit-SHA
  list is unchanged.
- ``check_update`` shared commit helper: credential-safe pending warning
  (operation/reason/local SHA, no URL/detail); original Actors preserved;
  repo-missing / version-info-missing -> FAILED.
"""

import os
from unittest.mock import Mock

import pytest
from git import Repo

from utils.git import (
    GitOutcome,
    check_git_folder,
    prepare_repo_for_update,
    push_current_head,
)
from utils.git_lock import RepoLockUnavailable, repo_file_locks

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _make_bare_remote(tmp_path) -> str:
    remote_path = tmp_path / "remote.git"
    bare = Repo.init(str(remote_path), bare=True)
    assert bare.bare
    return str(remote_path)


def _init_repo(tmp_path, name: str, branch: str = "main") -> Repo:
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


def _write_commit(repo: Repo, filename: str, content: str, msg: str) -> str:
    path = os.path.join(repo.working_dir, filename)
    with open(path, "w") as f:
        f.write(content)
    repo.index.add([filename])
    repo.index.commit(msg)
    return repo.head.commit.hexsha


def _add_origin(repo: Repo, remote_url: str) -> None:
    repo.create_remote("origin", remote_url)


def _seed_remote(
    tmp_path, remote_url: str, filename: str, content: str, msg: str
) -> str:
    """Create a commit in a temp clone and push it to the bare remote.

    Returns the pushed commit SHA.
    """
    seed = _init_repo(tmp_path, "seed")
    _add_origin(seed, remote_url)
    sha = _write_commit(seed, filename, content, msg)
    seed.git.push("origin", "main")
    return sha


def _install_pre_receive_hook(bare_repo: Repo, body: str) -> str:
    """Install a pre-receive hook in the bare remote; return its path."""
    hook_path = os.path.join(bare_repo.git_dir, "hooks", "pre-receive")
    os.makedirs(os.path.dirname(hook_path), exist_ok=True)
    with open(hook_path, "w") as f:
        f.write("#!/bin/sh\n" + body + "\n")
    os.chmod(hook_path, 0o755)
    return hook_path


def test_check_git_folder_opens_existing_repo_under_repo_lock(tmp_path):
    repo_path = tmp_path / "existing"
    repo = _init_repo(tmp_path, "existing")

    opened = check_git_folder(str(repo_path), "https://example.invalid")

    assert opened.working_dir == repo.working_dir
    # The non-blocking lock is released after the normal open.
    with repo_file_locks([str(repo_path) + ".lock"]):
        pass


def test_check_git_folder_clone_contention_skips_without_clone(tmp_path, monkeypatch):
    folder = str(tmp_path / "missing")
    clone = Mock()
    monkeypatch.setattr("utils.git.Repo.clone_from", clone)

    with repo_file_locks([folder + ".lock"]):
        with pytest.raises(RepoLockUnavailable):
            check_git_folder(folder, "https://example.invalid")

    clone.assert_not_called()


def test_check_git_folder_missing_repo_clones_while_holding_lock(tmp_path, monkeypatch):
    folder = str(tmp_path / "missing")
    cloned = Mock()

    def clone_from(*args, **kwargs):
        # The clone operation itself is covered by the target lock.
        with pytest.raises(RepoLockUnavailable):
            with repo_file_locks([folder + ".lock"]):
                pass
        return cloned

    monkeypatch.setattr("utils.git.Repo.clone_from", clone_from)
    assert check_git_folder(folder, "https://example.invalid") is cloned


def _clone_with_commit(
    tmp_path, name: str, remote_url: str, filename: str, content: str, msg: str
) -> tuple[Repo, str]:
    """Clone the bare remote, fast-forward onto main, then add one commit.

    Returns (repo, new_commit_sha). The remote ``main`` already exists, so a
    subsequent push is a true fast-forward / ahead push.
    """
    repo = _init_repo(tmp_path, name)
    _add_origin(repo, remote_url)
    repo.remote().fetch()
    repo.git.merge("--ff-only", "origin/main")
    sha = _write_commit(repo, filename, content, msg)
    return repo, sha


# --------------------------------------------------------------------------- #
# push_current_head: refspec, expected_sha, require_remote_branch, flags
# --------------------------------------------------------------------------- #


def test_push_explicit_refspec_and_verify(tmp_path):
    remote_url = _make_bare_remote(tmp_path)
    _seed_remote(tmp_path, remote_url, "base.txt", "base", "base")
    repo, sha = _clone_with_commit(
        tmp_path, "local", remote_url, "a.txt", "hello", "initial"
    )

    result = push_current_head(repo, branch="main")

    assert result.outcome is GitOutcome.OK
    assert result.local_sha == sha
    assert result.remote_sha == sha
    assert Repo(remote_url).commit("main").hexsha == sha


def test_push_expected_sha_mismatch_does_not_push(tmp_path):
    remote_url = _make_bare_remote(tmp_path)
    repo = _init_repo(tmp_path, "local")
    _add_origin(repo, remote_url)
    _seed_remote(tmp_path, remote_url, "r.txt", "remote", "remote only")
    sha = _write_commit(repo, "a.txt", "hello", "initial")

    result = push_current_head(repo, branch="main", expected_sha="deadbeef")

    assert result.outcome is GitOutcome.BLOCKED
    assert result.reason == "expected_sha_mismatch"
    assert result.local_sha == sha
    # Remote unchanged.
    assert Repo(remote_url).commit("main").hexsha != sha


def test_push_require_remote_branch_blocks_missing_main(tmp_path):
    remote_url = _make_bare_remote(tmp_path)  # empty bare, no main ref
    repo = _init_repo(tmp_path, "local")
    _add_origin(repo, remote_url)
    sha = _write_commit(repo, "a.txt", "hello", "initial")

    result = push_current_head(repo, branch="main", require_remote_branch=True)

    assert result.outcome is GitOutcome.BLOCKED
    assert result.reason == "missing_remote_branch"
    assert result.local_sha == sha
    # Never auto-created main on the remote.
    assert "main" not in [r.name for r in Repo(remote_url).refs]


def test_push_direct_stale_ref_blocks_missing_main(tmp_path):
    """Remote deletes main; local still has a stale origin/main tracking ref.

    With require_remote_branch=True, push_current_head must refresh the remote
    refs (prune) before deciding the branch exists, so it must NOT recreate the
    deleted remote main from the stale local ref. Result is BLOCKED /
    missing_remote_branch and the remote stays without main.
    """
    remote_url = _make_bare_remote(tmp_path)
    base_sha = _seed_remote(tmp_path, remote_url, "base.txt", "base", "base")

    repo = _init_repo(tmp_path, "local")
    _add_origin(repo, remote_url)
    repo.remote().fetch()  # creates a stale origin/main tracking ref
    assert repo.commit("origin/main").hexsha == base_sha
    # Upstream deletes main.
    Repo(remote_url).git.update_ref("-d", "refs/heads/main")
    # Local has its own commit so it is not unborn.
    local_sha = _write_commit(repo, "a.txt", "local", "local commit")

    result = push_current_head(repo, branch="main", require_remote_branch=True)

    assert result.outcome is GitOutcome.BLOCKED
    assert result.reason == "missing_remote_branch"
    assert result.local_sha == local_sha
    # Remote still has no main (stale local ref was not used to recreate it).
    assert "main" not in [r.name for r in Repo(remote_url).refs]
    # Local HEAD / working tree untouched.
    assert repo.head.commit.hexsha == local_sha
    assert os.path.exists(os.path.join(repo.working_dir, "a.txt"))


def test_push_per_ref_rejected_flag_is_pending(tmp_path):
    remote_url = _make_bare_remote(tmp_path)
    # Remote has its own root commit (no shared history with the local repo),
    # so the explicit push is non-fast-forward and rejected.
    _seed_remote(tmp_path, remote_url, "other.txt", "x", "remote seed")

    repo = _init_repo(tmp_path, "local")
    _add_origin(repo, remote_url)
    repo.remote().fetch()
    local_sha = _write_commit(repo, "a.txt", "hello", "local commit")

    result = push_current_head(repo, branch="main")

    assert result.outcome is GitOutcome.PENDING_PUSH
    assert result.reason == "push_rejected"
    assert result.local_sha == local_sha
    # Local HEAD and working tree intact; remote unchanged (no force push).
    assert repo.head.commit.hexsha == local_sha
    assert os.path.exists(os.path.join(repo.working_dir, "a.txt"))
    assert Repo(remote_url).commit("main").hexsha != local_sha


def test_push_empty_result_is_pending(tmp_path, monkeypatch):
    from unittest.mock import Mock

    remote_url = _make_bare_remote(tmp_path)
    repo = _init_repo(tmp_path, "local")
    _add_origin(repo, remote_url)
    _write_commit(repo, "a.txt", "hello", "initial")

    # Force an empty result list from the low-level push by stubbing the
    # Remote returned for "origin".
    remote_name = "origin"
    real_remote = repo.remote(remote_name)
    fake_remote = Mock(wraps=real_remote)
    fake_remote.push.return_value = []
    fake_remote.refs = real_remote.refs
    monkeypatch.setattr(repo, "remote", lambda name: fake_remote)

    result = push_current_head(repo, branch="main", require_remote_branch=False)
    assert result.outcome is GitOutcome.PENDING_PUSH
    assert result.reason == "empty_push_result"


def test_push_unborn_head_is_failed(tmp_path):
    remote_url = _make_bare_remote(tmp_path)
    repo = _init_repo(tmp_path, "local")  # unborn
    _add_origin(repo, remote_url)

    result = push_current_head(repo, branch="main")
    assert result.outcome is GitOutcome.FAILED
    assert result.reason == "unborn"
    assert result.local_sha is None


def test_push_verification_failure_is_pending(tmp_path, monkeypatch):
    """If the remote ref cannot be confirmed equal after a push, it is PENDING_PUSH.

    Simulated by making ``repo.commit`` raise for the remote-tracking ref only
    after a successful push. The local commit must remain intact.
    """
    remote_url = _make_bare_remote(tmp_path)
    _seed_remote(tmp_path, remote_url, "base.txt", "base", "base")
    repo, sha = _clone_with_commit(
        tmp_path, "local", remote_url, "a.txt", "hello", "initial"
    )

    real_commit = repo.commit

    def _commit_failing(ref, *a, **k):
        if str(ref).endswith("/main"):
            raise RuntimeError("cannot resolve remote-tracking ref")
        return real_commit(ref, *a, **k)

    monkeypatch.setattr(repo, "commit", _commit_failing)

    result = push_current_head(repo, branch="main")
    assert result.outcome is GitOutcome.PENDING_PUSH
    assert result.reason in ("verify_fetch_failed", "verify_sha_mismatch")
    assert repo.head.commit.hexsha == sha


def test_push_with_lease_rejects_stale_remote_without_mutation(tmp_path):
    remote_url = _make_bare_remote(tmp_path)
    base_sha = _seed_remote(tmp_path, remote_url, "base.txt", "base", "base")
    repo, local_sha = _clone_with_commit(
        tmp_path, "local", remote_url, "local.txt", "local", "local"
    )
    other, _ = _clone_with_commit(
        tmp_path, "other", remote_url, "other.txt", "other", "other"
    )
    assert (
        push_current_head(other, branch="main", old_remote_sha=base_sha).outcome
        is GitOutcome.OK
    )
    result = push_current_head(repo, branch="main", old_remote_sha=base_sha)
    assert result.outcome is GitOutcome.PENDING_PUSH
    assert Repo(remote_url).commit("main").hexsha == other.head.commit.hexsha
    assert repo.head.commit.hexsha == local_sha


def test_raw_probe_endpoint_preserves_credentials_and_ssh_user(tmp_path, monkeypatch):
    import check_update as cu

    repo = _init_repo(tmp_path, "endpoint")
    raw = "ssh://deploy@example.test:2222/srv/sekai.git"
    repo.create_remote("origin", raw)
    calls = []

    class _Result:
        stdout = "a" * 40 + "\trefs/heads/main\n"

    def _run(args, **kwargs):
        calls.append(args)
        return _Result()

    monkeypatch.setattr(cu.subprocess, "run", _run)
    cu._remote_snapshot(repo, "master")
    assert calls == [["git", "ls-remote", raw, "refs/heads/main"]]


# --------------------------------------------------------------------------- #
# prepare: prune, missing_remote_branch, behind, ff/fetch ops, recovery
# --------------------------------------------------------------------------- #


def test_prepare_missing_remote_branch_is_blocked(tmp_path):
    remote_url = _make_bare_remote(tmp_path)  # empty bare, no main
    repo = _init_repo(tmp_path, "local")
    _add_origin(repo, remote_url)
    sha = _write_commit(repo, "a.txt", "hello", "local commit")

    result = prepare_repo_for_update(repo, branch="main")
    assert result.outcome is GitOutcome.BLOCKED
    assert result.reason == "missing_remote_branch"
    assert result.local_sha == sha
    # prepare must never create main on the remote.
    assert "main" not in [r.name for r in Repo(remote_url).refs]


def test_prepare_stale_tracking_ref_is_blocked(tmp_path):
    """A stale/deleted remote main (tracking ref absent after prune fetch) is
    BLOCKED reason=missing_remote_branch, not auto-created."""
    remote_url = _make_bare_remote(tmp_path)
    base_sha = _seed_remote(tmp_path, remote_url, "r.txt", "remote", "remote base")

    repo = _init_repo(tmp_path, "local")
    _add_origin(repo, remote_url)
    # Fetch so origin/main exists locally.
    repo.remote().fetch()
    assert repo.commit("origin/main").hexsha == base_sha
    # Now delete main on the bare remote (simulating deletion upstream).
    Repo(remote_url).git.update_ref("-d", "refs/heads/main")
    # Local commit on top so the repo is not unborn.
    _write_commit(repo, "a.txt", "local", "local commit")

    result = prepare_repo_for_update(repo, branch="main")
    assert result.outcome is GitOutcome.BLOCKED
    assert result.reason == "missing_remote_branch"
    # Remote still has no main.
    assert "main" not in [r.name for r in Repo(remote_url).refs]


def test_prepare_true_shared_base_behind(tmp_path):
    """True shared-base behind: worker sits at base, a second clone advances
    the remote, then the worker prepares and fast-forwards to the new remote."""
    remote_url = _make_bare_remote(tmp_path)
    base_sha = _seed_remote(tmp_path, remote_url, "r.txt", "remote v1", "base")

    # Worker clone stopped at base (no local commit on top).
    worker = _init_repo(tmp_path, "worker")
    _add_origin(worker, remote_url)
    worker.remote().fetch()
    worker.git.merge("--ff-only", "origin/main")
    assert worker.head.commit.hexsha == base_sha

    # A second clone advances the remote with one more commit.
    advancer = _init_repo(tmp_path, "advancer")
    _add_origin(advancer, remote_url)
    advancer.remote().fetch()
    advancer.git.merge("--ff-only", "origin/main")
    advance_sha = _write_commit(advancer, "r2.txt", "remote v2", "remote advance")
    advancer.git.push("origin", "main")
    assert Repo(remote_url).commit("main").hexsha == advance_sha
    assert advance_sha != base_sha

    # Worker is now a true ancestor (shared base) of the remote -> ff only.
    result = prepare_repo_for_update(worker, branch="main")
    assert result.outcome is GitOutcome.OK
    assert result.reason == "fast_forwarded"
    assert worker.head.commit.hexsha == advance_sha
    assert worker.head.commit.hexsha == Repo(remote_url).commit("main").hexsha


def test_prepare_fetch_failure_operation_is_fetch(tmp_path):
    repo = _init_repo(tmp_path, "local")
    # Origin points at a non-existent path so fetch fails.
    repo.create_remote("origin", str(tmp_path / "does-not-exist.git"))

    result = prepare_repo_for_update(repo, branch="main")
    assert result.outcome is GitOutcome.FAILED
    assert result.reason == "fetch_failed"
    assert result.operation == "fetch"


def test_prepare_ff_failure_operation_is_fast_forward(tmp_path, monkeypatch):
    """Reuse the true shared-base behind construction, then force the ff-only
    merge to raise. The outcome must be FAILED / ff_failed / fast_forward and the
    local HEAD must be unchanged."""
    remote_url = _make_bare_remote(tmp_path)
    base_sha = _seed_remote(tmp_path, remote_url, "r.txt", "remote v1", "base")

    worker = _init_repo(tmp_path, "worker")
    _add_origin(worker, remote_url)
    worker.remote().fetch()
    worker.git.merge("--ff-only", "origin/main")
    assert worker.head.commit.hexsha == base_sha

    advancer = _init_repo(tmp_path, "advancer")
    _add_origin(advancer, remote_url)
    advancer.remote().fetch()
    advancer.git.merge("--ff-only", "origin/main")
    advance_sha = _write_commit(advancer, "r2.txt", "remote v2", "remote advance")
    advancer.git.push("origin", "main")
    assert Repo(remote_url).commit("main").hexsha == advance_sha

    # Make only the ff-only merge fail; keep other git commands (fetch) real so
    # the remote-tracking ref is refreshed and the worker is detected as behind.
    real_git = worker.git

    class _GitMergeFails:
        def __getattr__(self, name):
            if name == "merge":

                def _raise(*args, **kwargs):
                    raise RuntimeError("ff blocked")

                return _raise
            return getattr(real_git, name)

    monkeypatch.setattr(worker, "git", _GitMergeFails())

    result = prepare_repo_for_update(worker, branch="main")
    assert result.outcome is GitOutcome.FAILED
    assert result.reason == "ff_failed"
    assert result.operation == "fast_forward"
    # Local HEAD unchanged.
    assert worker.head.commit.hexsha == base_sha
    assert os.path.exists(os.path.join(worker.working_dir, "r.txt"))


def test_prepare_equal_is_ok(tmp_path):
    remote_url = _make_bare_remote(tmp_path)
    _seed_remote(tmp_path, remote_url, "base.txt", "base", "base")
    repo, sha = _clone_with_commit(tmp_path, "local", remote_url, "a.txt", "hi", "init")
    push_current_head(repo, branch="main")

    result = prepare_repo_for_update(repo, branch="main")
    assert result.outcome is GitOutcome.OK
    assert result.local_sha == sha
    assert result.remote_sha == sha


def test_prepare_ahead_pushes_same_sha(tmp_path):
    remote_url = _make_bare_remote(tmp_path)
    _seed_remote(tmp_path, remote_url, "base.txt", "base", "base")
    repo, sha = _clone_with_commit(
        tmp_path, "local", remote_url, "a.txt", "hi", "local only"
    )

    result = prepare_repo_for_update(repo, branch="main")
    assert result.outcome is GitOutcome.OK
    assert result.reason == "ahead_pushed"
    assert result.local_sha == sha
    assert result.remote_sha == sha
    assert len(list(repo.iter_commits("HEAD"))) == 2  # base + local commit
    assert Repo(remote_url).commit("main").hexsha == sha


def test_prepare_diverged_is_blocked(tmp_path):
    remote_url = _make_bare_remote(tmp_path)
    _seed_remote(tmp_path, remote_url, "r.txt", "remote", "remote only")

    repo = _init_repo(tmp_path, "local")
    _add_origin(repo, remote_url)
    _write_commit(repo, "l.txt", "local", "local only")

    result = prepare_repo_for_update(repo, branch="main")
    assert result.outcome is GitOutcome.BLOCKED
    assert result.reason == "diverged"
    assert repo.head.commit.hexsha != Repo(remote_url).commit("main").hexsha


def test_prepare_local_unborn_remote_exists_ff(tmp_path):
    remote_url = _make_bare_remote(tmp_path)
    _seed_remote(tmp_path, remote_url, "r.txt", "remote", "remote only")

    repo = _init_repo(tmp_path, "local")  # unborn
    _add_origin(repo, remote_url)

    result = prepare_repo_for_update(repo, branch="main")
    assert result.outcome is GitOutcome.OK
    assert result.reason == "fast_forwarded"
    assert repo.head.commit.hexsha == Repo(remote_url).commit("main").hexsha


def test_prepare_local_and_remote_both_missing_is_blocked(tmp_path):
    remote_url = _make_bare_remote(tmp_path)  # empty bare, no main
    repo = _init_repo(tmp_path, "local")  # unborn
    _add_origin(repo, remote_url)

    result = prepare_repo_for_update(repo, branch="main")
    assert result.outcome is GitOutcome.BLOCKED
    assert result.reason == "unborn_no_remote"


def test_prepare_detached_is_blocked(tmp_path):
    remote_url = _make_bare_remote(tmp_path)
    repo = _init_repo(tmp_path, "local")
    _add_origin(repo, remote_url)
    sha = _write_commit(repo, "a.txt", "hi", "c")
    repo.git.checkout(sha)

    result = prepare_repo_for_update(repo, branch="main")
    assert result.outcome is GitOutcome.BLOCKED
    assert result.reason == "detached_head"
    assert result.local_sha == sha


def test_prepare_wrong_branch_is_blocked(tmp_path):
    repo = _init_repo(tmp_path, "local", branch="feature")
    _add_origin(repo, _make_bare_remote(tmp_path))
    result = prepare_repo_for_update(repo, branch="main")
    assert result.outcome is GitOutcome.BLOCKED
    assert result.reason == "wrong_branch"


def test_prepare_missing_origin_is_blocked(tmp_path):
    repo = _init_repo(tmp_path, "local")
    result = prepare_repo_for_update(repo, branch="main")
    assert result.outcome is GitOutcome.BLOCKED
    assert result.reason == "missing_origin"


def test_prepare_dirty_is_blocked(tmp_path):
    remote_url = _make_bare_remote(tmp_path)
    repo = _init_repo(tmp_path, "local")
    _add_origin(repo, remote_url)
    _write_commit(repo, "a.txt", "hi", "c")
    with open(os.path.join(repo.working_dir, "a.txt"), "a") as f:
        f.write("dirty")
    result = prepare_repo_for_update(repo, branch="main")
    assert result.outcome is GitOutcome.BLOCKED
    assert result.reason == "dirty"


def test_prepare_clean_ahead_recovery_same_sha(tmp_path):
    """Real shared-base recovery via a temporary pre-receive hook.

    Flow: remote has base; a worker clone; worker creates one pending commit;
    a bare pre-receive hook temporarily rejects pushes; the push returns
    PENDING_PUSH and the remote remains at base (worker HEAD/working tree
    untouched); the hook is removed; prepare detects a true ahead and pushes the
    *same* SHA; the full commit-SHA list is identical before and after.
    """
    remote_url = _make_bare_remote(tmp_path)
    base_sha = _seed_remote(tmp_path, remote_url, "base.txt", "base", "base")

    # Worker clone shares the base.
    worker = _init_repo(tmp_path, "worker")
    _add_origin(worker, remote_url)
    worker.remote().fetch()
    worker.git.merge("--ff-only", "origin/main")
    assert worker.head.commit.hexsha == base_sha

    # Worker creates one pending commit.
    pending_sha = _write_commit(worker, "w.txt", "worker", "worker pending")
    before_commits = [c.hexsha for c in worker.iter_commits("HEAD")]
    assert before_commits[0] == pending_sha

    # Install a hook that rejects every push (exit 1), simulating a transient
    # rejecting server.
    hook = _install_pre_receive_hook(
        Repo(remote_url), "echo 'temporarily rejecting' >&2; exit 1"
    )

    failed = push_current_head(worker, branch="main", require_remote_branch=True)
    assert failed.outcome is GitOutcome.PENDING_PUSH
    assert failed.reason == "push_rejected"
    assert failed.local_sha == pending_sha
    # Remote still at base; worker HEAD/working tree untouched.
    assert Repo(remote_url).commit("main").hexsha == base_sha
    assert worker.head.commit.hexsha == pending_sha
    assert os.path.exists(os.path.join(worker.working_dir, "w.txt"))

    # Remove the hook; prepare should now see a true ahead and push same SHA.
    os.remove(hook)
    result = prepare_repo_for_update(worker, branch="main")
    assert result.outcome is GitOutcome.OK
    assert result.reason == "ahead_pushed"
    assert result.local_sha == pending_sha
    assert result.remote_sha == pending_sha

    # The full commit-SHA list is unchanged (still exactly one commit).
    after_commits = [c.hexsha for c in worker.iter_commits("HEAD")]
    assert after_commits == before_commits
    assert Repo(remote_url).commit("main").hexsha == pending_sha


# --------------------------------------------------------------------------- #
# check_update shared commit helper
# --------------------------------------------------------------------------- #


def test_commit_pending_emits_credential_safe_warning(monkeypatch, caplog):
    import logging

    import check_update as cu

    repo = Mock()
    repo.is_dirty.return_value = True
    pending = cu.GitResult(
        outcome=GitOutcome.PENDING_PUSH,
        reason="push_rejected",
        local_sha="abc123",
        detail="secret url https://x:TOKEN@github.com/y.git",
    )
    monkeypatch.setattr(cu, "push_current_head", Mock(return_value=pending))
    monkeypatch.setattr(cu, "masterdb_diff_repo", repo)
    monkeypatch.setattr(cu, "version_info", {"dataVersion": "1", "assetVersion": "1"})
    monkeypatch.setattr(cu, "check_git_folder", Mock())

    with caplog.at_level(logging.WARNING):
        result = cu.commit_master_diff()

    assert result.outcome is GitOutcome.PENDING_PUSH
    assert bool(result) is False
    # Warning must include operation/reason/local_sha but NOT the URL/detail.
    # The warning is emitted by the push step (operation "push_master_diff").
    warning = caplog.text
    assert "push pending" in warning
    assert "push_master_diff" in warning
    assert "push_rejected" in warning
    assert "abc123" in warning
    assert "TOKEN" not in warning
    assert "github.com" not in warning
    # Author Actor unchanged.
    repo.index.commit.assert_called_once()
    author = repo.index.commit.call_args.kwargs["author"]
    assert author.name == "master-db-diff-bot"


def test_commit_failed_when_repo_missing(monkeypatch):
    import check_update as cu

    monkeypatch.setattr(cu, "masterdb_diff_repo", None)
    monkeypatch.setattr(cu, "version_info", {"dataVersion": "1", "assetVersion": "1"})
    monkeypatch.setattr(cu, "push_current_head", Mock())

    result = cu.commit_master_diff()
    assert result.outcome is GitOutcome.FAILED
    assert result.reason == "repo_missing"
    cu.push_current_head.assert_not_called()


def test_commit_failed_when_version_info_missing(monkeypatch):
    import check_update as cu

    repo = Mock()
    repo.is_dirty.return_value = True
    monkeypatch.setattr(cu, "masterdb_diff_repo", repo)
    monkeypatch.setattr(cu, "version_info", None)
    monkeypatch.setattr(cu, "push_current_head", Mock())

    result = cu.commit_master_diff()
    assert result.outcome is GitOutcome.FAILED
    assert result.reason == "version_info_missing"
    repo.index.commit.assert_not_called()
    cu.push_current_head.assert_not_called()


def test_commit_i18n_uses_i18n_actor(monkeypatch):
    import check_update as cu

    repo = Mock()
    repo.is_dirty.return_value = True
    pending = cu.GitResult(outcome=GitOutcome.PENDING_PUSH, reason="x", local_sha="s")
    monkeypatch.setattr(cu, "push_current_head", Mock(return_value=pending))
    monkeypatch.setattr(cu, "i18n_diff_repo", repo)
    monkeypatch.setattr(cu, "version_info", {"dataVersion": "1", "assetVersion": "1"})
    monkeypatch.setattr(cu, "check_git_folder", Mock())

    cu.commit_i18n_files()
    author = repo.index.commit.call_args.kwargs["author"]
    assert author.name == "i18n-diff-bot"


# --------------------------------------------------------------------------- #
# Phase 2: crash during publication retains journal + staging for recovery
# --------------------------------------------------------------------------- #


def test_publication_crash_retains_journal_and_staging(tmp_path, monkeypatch):
    """A crash that interrupts publication (journal + journal-owned staging left
    on disk, no clean abort) must be recoverable: the next cycle completes the
    ordered replaces, commits exactly once (no duplicate), and pushes the same
    SHA. Mirrors the durable-recovery contract in ``check_update``."""
    import check_update as cu
    from utils.update_transaction import (
        FileEntry,
        RepoCommitState,
        RepoPushState,
        RepoState,
        TransactionJournal,
        TxnPhase,
        compute_sha256,
        new_transaction_id,
        staging_dir_for,
    )

    remote = _make_bare_remote(tmp_path)
    _seed_remote(tmp_path, remote, "base.txt", "base", "base")
    repo = _init_repo(tmp_path, "worker")
    _add_origin(repo, remote)
    repo.remote().fetch()
    repo.git.merge("--ff-only", "origin/main")

    monkeypatch.setattr(cu, "masterdb_diff_repo", repo)
    monkeypatch.setattr(cu, "i18n_diff_repo", repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", repo.working_dir)
    monkeypatch.setattr(cu, "i18n_diff_folder_path", repo.working_dir)
    monkeypatch.setattr(
        cu, "version_info", {"dataVersion": "100", "assetVersion": "100"}
    )
    monkeypatch.setattr(
        cu, "update_options", {"master": True, "i18n": True, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")
    monkeypatch.setattr(cu.jsonrpc_client, "request", lambda m, p=None: {})

    txn_id = new_transaction_id()
    staging = staging_dir_for(repo.working_dir, txn_id)
    manifest = ["versions.json", "cards.json"]
    files = {}
    for rel in manifest:
        content = (
            {"dataVersion": "100", "assetVersion": "100"}
            if rel == "versions.json"
            else [{"id": 1}]
        )
        sp = os.path.join(staging, rel)
        os.makedirs(os.path.dirname(sp), exist_ok=True)
        with open(sp, "w", encoding="utf-8") as f:
            import json as _json

            _json.dump(content, f, ensure_ascii=False, indent=2)
        files[rel] = FileEntry(source_sha256=compute_sha256(sp))

    repos = {
        "master": RepoState(
            manifest=list(manifest),
            staging_dir=staging,
            target_commit_sha=None,
            base_sha=repo.head.commit.hexsha,
            remote_sha=None,
            remote_base_sha=repo.head.commit.hexsha,
            remote_name="origin",
            remote_ref="refs/heads/main",
            remote_endpoint_fingerprint=cu._remote_endpoint(repo, "master")[1],
            files=files,
            commit_state=RepoCommitState.PENDING,
            push_state=RepoPushState.PENDING,
        )
    }
    journal = TransactionJournal(
        master_git_dir=repo.git_dir,
        transaction_id=txn_id,
        candidate={"dataVersion": "100", "assetVersion": "100"},
        enabled_repos=["master"],
        publish_order=["master"],
        repos=repos,
        phase=TxnPhase.PUBLISHING,
    )
    journal.write()

    # The journal and the journal-owned staging must both be present on disk.
    assert TransactionJournal.load(repo.git_dir) is not None
    assert os.path.isdir(staging)

    base_commits = len(list(repo.iter_commits("HEAD")))
    status = cu._run_update_cycle_locked(daily=True)
    assert status == "recovered"

    # Exactly one new commit (no duplicate), and the remote got the same SHA.
    assert len(list(repo.iter_commits("HEAD"))) == base_commits + 1
    assert Repo(remote).commit("main").hexsha == repo.head.commit.hexsha
    # After successful recovery the journal is deleted and staging cleared.
    assert TransactionJournal.load(repo.git_dir) is None
    assert not os.path.isdir(staging)
