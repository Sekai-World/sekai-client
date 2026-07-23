"""Phase 2 durable recovery integration tests (staging + journal).

These tests drive the REAL ``check_update`` cycle machinery (recovery runs
FIRST, before the maintenance/new-version gate and generation) against two
temporary bare remotes and two worker repositories, with no network.

Covered decisive scenarios:

  A. Recovery runs BEFORE the ordinary ``new_version=False`` gate and the
     maintenance gate: when a journal is present, neither the RPC gate nor
     generation is invoked, and recovery re-pushes the SAME target SHA.
  B. A crash during publication (journal + staging retained on disk, no clean
     abort) is recovered: the ordered source/destination hashes complete the
     replaces, the commit is created exactly once (no duplicate commit), and the
     push uses the expected-SHA workflow.
  C. A crash after the replace but before the journal advances to COMMITTING is
     recovered without a duplicate commit (HEAD already equals target SHA).
  D. The remote already has the target SHA (push accepted) but the journal was
     not advanced: recovery recognizes the remote SHA and performs NO duplicate
     push / commit.
  E. Fail-closed: a malformed journal, a staging hash mismatch, or manifest-
     external dirt makes the cycle return ``journal_invalid`` with NO generation,
     NO reset, and NO force-push.
"""

import json
import os
import subprocess

import git
import pytest

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

CANDIDATE = {"dataVersion": "100", "assetVersion": "100"}


def _make_bare_remote(tmp_path, name):
    remote_path = tmp_path / f"{name}.git"
    bare = git.Repo.init(str(remote_path), bare=True)
    bare.git.symbolic_ref("HEAD", "refs/heads/main")
    return str(remote_path)


_SEED_COUNTER = [0]


def _seed_remote(tmp_path, remote_url, filename, content, msg):
    _SEED_COUNTER[0] += 1
    seed_dir = str(tmp_path / f"seed_{_SEED_COUNTER[0]}")
    seed = git.Repo.init(seed_dir)
    with seed.config_writer() as cw:
        cw.set_value("user", "name", "s")
        cw.set_value("user", "email", "s@example.com")
        cw.set_value("init", "defaultBranch", "main")
    if not seed.head.is_valid() or seed.active_branch.name != "main":
        seed.git.checkout("-b", "main")
    seed.create_remote("origin", remote_url)
    p = os.path.join(seed.working_dir, filename)
    with open(p, "w") as f:
        f.write(content)
    seed.index.add([filename])
    seed.index.commit(msg)
    seed.git.push("origin", "main")
    return seed.head.commit.hexsha


def _clone_worker(tmp_path, name, remote_url):
    worker = git.Repo.clone_from(remote_url, str(tmp_path / name))
    with worker.config_writer() as cw:
        cw.set_value("user", "name", "w")
        cw.set_value("user", "email", "w@example.com")
    if worker.active_branch.name != "main":
        worker.git.checkout("-b", "main")
    return worker


def _write_json(repo, relpath, data):
    full = os.path.join(repo.working_dir, relpath)
    parent = os.path.dirname(full)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _set_globals(monkeypatch, master_repo, i18n_repo):
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "i18n_diff_repo", i18n_repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_repo.working_dir)
    monkeypatch.setattr(cu, "i18n_diff_folder_path", i18n_repo.working_dir)
    monkeypatch.setattr(cu, "version_info", dict(CANDIDATE))
    monkeypatch.setattr(
        cu, "update_options", {"master": True, "i18n": True, "userInfo": False}
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")


def _rpc_calls():
    calls = []

    def _request(method, params=None):
        calls.append(method)
        if method in ("check_versions", "check_versions_simple"):
            return {"maintenance": False, "new_version": False}
        return {}

    return calls


def _setup_two_repo(tmp_path, monkeypatch):
    master_remote = _make_bare_remote(tmp_path, "master_remote")
    i18n_remote = _make_bare_remote(tmp_path, "i18n_remote")
    _seed_remote(tmp_path, master_remote, "base.txt", "m", "m")
    _seed_remote(tmp_path, i18n_remote, "base.txt", "i", "i")
    master_repo = _clone_worker(tmp_path, "master_worker", master_remote)
    i18n_repo = _clone_worker(tmp_path, "i18n_worker", i18n_remote)
    _set_globals(monkeypatch, master_repo, i18n_repo)
    return master_remote, i18n_remote, master_repo, i18n_repo


def _canonical_content(key, rel):
    if rel == "versions.json":
        return CANDIDATE
    if rel == "cards.json":
        return [{"id": 1}]
    if rel.endswith("card_prefix.json"):
        return {"1": "p"}
    return {}


def _write_json_path(full, data):
    parent = os.path.dirname(full)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _seed_journal_and_staging(
    master_repo, i18n_repo, txn_id, *, published=None, phase=TxnPhase.PUBLISHING
):
    """Pre-seed a durable journal + journal-owned staging on disk (simulating a
    crash that left both intact). ``published`` maps repo-> {rel: content} that
    should already be in the formal working tree (simulating a partial replace
    that completed before the crash)."""
    published = published or {}
    master_staging = staging_dir_for(master_repo.working_dir, txn_id)
    i18n_staging = staging_dir_for(i18n_repo.working_dir, txn_id)

    def _state(key, repo, staging, manifest):
        files = {}
        for rel in manifest:
            content = _canonical_content(key, rel)
            sp = os.path.join(staging, rel)
            _write_json_path(sp, content)
            files[rel] = FileEntry(source_sha256=compute_sha256(sp))
        return RepoState(
            manifest=list(manifest),
            staging_dir=staging,
            target_commit_sha=None,
            base_sha=repo.head.commit.hexsha if repo.head.is_valid() else None,
            remote_base_sha=repo.head.commit.hexsha if repo.head.is_valid() else None,
            remote_name="origin",
            remote_ref="refs/heads/main",
            remote_sha=None,
            remote_endpoint_fingerprint=cu._remote_endpoint(repo, key)[1],
            files=files,
            commit_state=RepoCommitState.PENDING,
            push_state=RepoPushState.PENDING,
        )

    master_manifest = ["versions.json", "cards.json"]
    i18n_manifest = [os.path.join("ja", "card_prefix.json")]
    repos = {
        "master": _state("master", master_repo, master_staging, master_manifest),
        "i18n": _state("i18n", i18n_repo, i18n_staging, i18n_manifest),
    }
    for key, relmap in published.items():
        repo = master_repo if key == "master" else i18n_repo
        for rel, content in relmap.items():
            _write_json_path(os.path.join(repo.working_dir, rel), content)
            repos[key].files[rel].dest_sha256 = compute_sha256(
                os.path.join(repo.working_dir, rel)
            )
    j = TransactionJournal(
        master_git_dir=master_repo.git_dir,
        transaction_id=txn_id,
        candidate=dict(CANDIDATE),
        enabled_repos=["master", "i18n"],
        publish_order=["master", "i18n"],
        repos=repos,
        phase=phase,
    )
    j.write()
    return j


def _base_commits(repo):
    # The worker was cloned from a seeded remote (1 base commit).
    return 1


def _prepared_target(repo, base, content, message, parent=None):
    """Create a commit-tree target without moving HEAD or installing its index."""
    target_path = os.path.join(repo.working_dir, "target.json")
    with open(target_path, "w", encoding="utf-8") as stream:
        stream.write(content)
    temp_index = os.path.join(repo.git_dir, "gate3-test-index")
    env = {**os.environ, "GIT_INDEX_FILE": temp_index}
    try:
        subprocess.run(
            ["git", "read-tree", base], cwd=repo.working_dir, env=env, check=True
        )
        blob = subprocess.run(
            ["git", "hash-object", "-w", target_path],
            cwd=repo.working_dir,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(
            [
                "git",
                "update-index",
                "--add",
                "--cacheinfo",
                f"100644,{blob},target.json",
            ],
            cwd=repo.working_dir,
            env=env,
            check=True,
        )
        tree = subprocess.run(
            ["git", "write-tree"],
            cwd=repo.working_dir,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        args = ["git", "commit-tree", tree, "-p", parent or base]
        return subprocess.run(
            args,
            cwd=repo.working_dir,
            env={
                **env,
                "GIT_AUTHOR_NAME": "gate3",
                "GIT_AUTHOR_EMAIL": "gate3@example.com",
                "GIT_COMMITTER_NAME": "gate3",
                "GIT_COMMITTER_EMAIL": "gate3@example.com",
            },
            input=message,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    finally:
        if os.path.exists(temp_index):
            os.remove(temp_index)


def _prepared_target_journal(master_repo, target, txn_id, base):
    staging = staging_dir_for(master_repo.working_dir, txn_id)
    source = os.path.join(staging, "target.json")
    _write_json_path(source, {"value": "good"})
    destination = os.path.join(master_repo.working_dir, "target.json")
    _write_json_path(destination, {"value": "good"})
    journal = TransactionJournal(
        master_git_dir=master_repo.git_dir,
        transaction_id=txn_id,
        candidate=dict(CANDIDATE),
        enabled_repos=["master"],
        publish_order=["master"],
        repos={
            "master": RepoState(
                manifest=["target.json"],
                staging_dir=staging,
                repo_root=master_repo.working_dir,
                target_commit_sha=target,
                base_sha=base,
                remote_sha=None,
                remote_base_sha=base,
                remote_name="origin",
                remote_ref="refs/heads/main",
                remote_endpoint_fingerprint=cu._remote_endpoint(master_repo, "master")[
                    1
                ],
                files={"target.json": FileEntry(source_sha256=compute_sha256(source))},
                commit_state=RepoCommitState.PREPARED,
                push_state=RepoPushState.PENDING,
            )
        },
        phase=TxnPhase.COMMITTING,
    )
    journal.write()
    return journal


def _assert_prepared_target_rejected(
    master_repo, i18n_repo, monkeypatch, journal, expected_worktree=None
):
    _set_globals(monkeypatch, master_repo, i18n_repo)
    before_head = master_repo.head.commit.hexsha
    before_index = open(master_repo.index.path, "rb").read()
    before_journal = open(journal.journal_path, "rb").read()
    status = cu._run_update_cycle_locked(daily=True)
    assert status == "journal_invalid"
    assert master_repo.head.commit.hexsha == before_head
    assert open(master_repo.index.path, "rb").read() == before_index
    assert open(journal.journal_path, "rb").read() == before_journal
    if expected_worktree is not None:
        assert (
            open(
                os.path.join(master_repo.working_dir, "target.json"), encoding="utf-8"
            ).read()
            == expected_worktree
        )
    assert TransactionJournal.load(master_repo.git_dir) is not None


def test_prepared_target_wrong_parent_fails_closed(tmp_path, monkeypatch):
    _master_remote, _i18n_remote, master_repo, i18n_repo = _setup_two_repo(
        tmp_path, monkeypatch
    )
    base = master_repo.head.commit.hexsha
    _endpoint, endpoint_fingerprint = cu._remote_endpoint(master_repo, "master")
    other_tree = master_repo.git.rev_parse(f"{base}^{{tree}}")
    other = subprocess.run(
        ["git", "commit-tree", other_tree],
        cwd=master_repo.working_dir,
        input="unrelated parent\n",
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    target = _prepared_target(
        master_repo,
        base,
        '{"value": "good"}',
        "prepared\n\nSekai-Transaction-Id: ignored\nSekai-Transaction-Repo: master\n",
        parent=other,
    )
    journal = _prepared_target_journal(master_repo, target, new_transaction_id(), base)
    _assert_prepared_target_rejected(master_repo, i18n_repo, monkeypatch, journal)


def test_noop_target_still_proves_manifest_against_base(tmp_path, monkeypatch):
    _master_remote, _i18n_remote, master_repo, i18n_repo = _setup_two_repo(
        tmp_path, monkeypatch
    )
    base = master_repo.head.commit.hexsha
    txn_id = new_transaction_id()
    journal = _prepared_target_journal(master_repo, base, txn_id, base)
    _assert_prepared_target_rejected(master_repo, i18n_repo, monkeypatch, journal)


def test_completed_journal_invalid_head_is_retained(tmp_path, monkeypatch):
    _master_remote, _i18n_remote, master_repo, i18n_repo = _setup_two_repo(
        tmp_path, monkeypatch
    )
    base = master_repo.head.commit.hexsha
    txn_id = new_transaction_id()
    journal = TransactionJournal(
        master_git_dir=master_repo.git_dir,
        transaction_id=txn_id,
        candidate=dict(CANDIDATE),
        enabled_repos=["master"],
        publish_order=["master"],
        repos={
            "master": RepoState(
                manifest=[],
                staging_dir=staging_dir_for(master_repo.working_dir, txn_id),
                repo_root=master_repo.working_dir,
                target_commit_sha=base,
                base_sha=base,
                remote_sha=base,
                remote_base_sha=base,
                remote_name="origin",
                remote_ref="refs/heads/main",
                remote_endpoint_fingerprint=cu._remote_endpoint(master_repo, "master")[
                    1
                ],
                commit_state=RepoCommitState.COMMITTED,
                push_state=RepoPushState.PUSHED,
            )
        },
        phase=TxnPhase.COMPLETED,
    )
    journal.write()
    _write_json(master_repo, "diverged.json", {"bad": True})
    master_repo.index.add(["diverged.json"])
    master_repo.index.commit("diverged")
    _set_globals(monkeypatch, master_repo, i18n_repo)
    assert cu._run_update_cycle_locked(daily=True) == "journal_invalid"
    assert TransactionJournal.load(master_repo.git_dir) is not None


def test_completed_journal_prohibited_dirt_is_retained(tmp_path, monkeypatch):
    _master_remote, _i18n_remote, master_repo, i18n_repo = _setup_two_repo(
        tmp_path, monkeypatch
    )
    base = master_repo.head.commit.hexsha
    _endpoint, endpoint_fingerprint = cu._remote_endpoint(master_repo, "master")
    txn_id = new_transaction_id()
    journal = TransactionJournal(
        master_git_dir=master_repo.git_dir,
        transaction_id=txn_id,
        candidate=dict(CANDIDATE),
        enabled_repos=["master"],
        publish_order=["master"],
        repos={
            "master": RepoState(
                manifest=[],
                staging_dir=staging_dir_for(master_repo.working_dir, txn_id),
                repo_root=master_repo.working_dir,
                target_commit_sha=base,
                base_sha=base,
                remote_sha=base,
                remote_base_sha=base,
                remote_endpoint_fingerprint=endpoint_fingerprint,
                commit_state=RepoCommitState.COMMITTED,
                push_state=RepoPushState.PUSHED,
            )
        },
        phase=TxnPhase.COMPLETED,
    )
    journal.write()
    with open(os.path.join(master_repo.working_dir, "prohibited.txt"), "w") as f:
        f.write("dirt")
    _set_globals(monkeypatch, master_repo, i18n_repo)
    assert cu._run_update_cycle_locked(daily=True) == "journal_invalid"
    assert TransactionJournal.load(master_repo.git_dir) is not None


@pytest.mark.parametrize(
    "message",
    [
        "prepared\n",
        "prepared\n\nSekai-Transaction-Id: wrong\nSekai-Transaction-Repo: master\n",
        "prepared\n\nSekai-Transaction-Id: {txn}\nSekai-Transaction-Repo: i18n\n",
    ],
    ids=["missing-trailers", "wrong-transaction", "wrong-repo"],
)
def test_prepared_target_trailer_mismatch_fails_closed(tmp_path, monkeypatch, message):
    _master_remote, _i18n_remote, master_repo, i18n_repo = _setup_two_repo(
        tmp_path, monkeypatch
    )
    base = master_repo.head.commit.hexsha
    txn_id = new_transaction_id()
    target = _prepared_target(
        master_repo,
        base,
        '{"value": "good"}',
        message.format(txn=txn_id),
    )
    journal = _prepared_target_journal(master_repo, target, txn_id, base)
    _assert_prepared_target_rejected(master_repo, i18n_repo, monkeypatch, journal)


def test_prepared_target_tree_mismatch_fails_closed(tmp_path, monkeypatch):
    _master_remote, _i18n_remote, master_repo, i18n_repo = _setup_two_repo(
        tmp_path, monkeypatch
    )
    base = master_repo.head.commit.hexsha
    txn_id = new_transaction_id()
    target = _prepared_target(
        master_repo,
        base,
        '{"value": "bad"}',
        f"prepared\n\nSekai-Transaction-Id: {txn_id}\nSekai-Transaction-Repo: master\n",
    )
    journal = _prepared_target_journal(master_repo, target, txn_id, base)
    _assert_prepared_target_rejected(master_repo, i18n_repo, monkeypatch, journal)


def test_prepared_target_conflicting_duplicate_trailers_fails_closed(
    tmp_path, monkeypatch
):
    _master_remote, _i18n_remote, master_repo, i18n_repo = _setup_two_repo(
        tmp_path, monkeypatch
    )
    base = master_repo.head.commit.hexsha
    txn_id = new_transaction_id()
    message = (
        "prepared\n\n"
        f"Sekai-Update-Txn: {txn_id}\n"
        "Sekai-Update-Txn: conflicting\n"
        "Sekai-Update-Repo: master\n"
        "Sekai-Update-Repo: i18n\n"
    )
    target = _prepared_target(master_repo, base, '{"value": "good"}', message)
    journal = _prepared_target_journal(master_repo, target, txn_id, base)
    _assert_prepared_target_rejected(master_repo, i18n_repo, monkeypatch, journal)


def test_prepared_target_worktree_mismatch_fails_closed(tmp_path, monkeypatch):
    _master_remote, _i18n_remote, master_repo, i18n_repo = _setup_two_repo(
        tmp_path, monkeypatch
    )
    base = master_repo.head.commit.hexsha
    txn_id = new_transaction_id()
    target = _prepared_target(
        master_repo,
        base,
        '{"value": "good"}',
        f"prepared\n\nSekai-Transaction-Id: {txn_id}\nSekai-Transaction-Repo: master\n",
    )
    journal = _prepared_target_journal(master_repo, target, txn_id, base)
    _write_json(master_repo, "target.json", {"value": "changed"})
    _assert_prepared_target_rejected(master_repo, i18n_repo, monkeypatch, journal)


def test_prepared_target_index_mismatch_fails_closed(tmp_path, monkeypatch):
    _master_remote, _i18n_remote, master_repo, i18n_repo = _setup_two_repo(
        tmp_path, monkeypatch
    )
    base = master_repo.head.commit.hexsha
    txn_id = new_transaction_id()
    target = _prepared_target(
        master_repo,
        base,
        '{"value": "good"}',
        f"prepared\n\nSekai-Transaction-Id: {txn_id}\nSekai-Transaction-Repo: master\n",
    )
    journal = _prepared_target_journal(master_repo, target, txn_id, base)
    _write_json(master_repo, "target.json", {"value": "staged-bad"})
    master_repo.index.add(["target.json"])
    _write_json(master_repo, "target.json", {"value": "good"})
    _assert_prepared_target_rejected(master_repo, i18n_repo, monkeypatch, journal)


def test_post_cas_invalid_index_fails_closed(tmp_path, monkeypatch):
    _master_remote, _i18n_remote, master_repo, i18n_repo = _setup_two_repo(
        tmp_path, monkeypatch
    )
    base = master_repo.head.commit.hexsha
    txn_id = new_transaction_id()
    target = _prepared_target(
        master_repo,
        base,
        '{"value": "good"}',
        f"prepared\n\nSekai-Update-Txn: {txn_id}\nSekai-Update-Repo: master\n",
    )
    journal = _prepared_target_journal(master_repo, target, txn_id, base)
    master_repo.git.update_ref("refs/heads/main", target, base)
    master_repo.git.reset("--hard", target)
    _write_json(master_repo, "target.json", {"value": "third-tree"})
    master_repo.index.add(["target.json"])
    _write_json(master_repo, "target.json", {"value": "good"})
    _assert_prepared_target_rejected(master_repo, i18n_repo, monkeypatch, journal)


@pytest.mark.parametrize("mode", ["detached", "wrong-branch"])
def test_recovery_requires_attached_main_head(tmp_path, monkeypatch, mode):
    _master_remote, _i18n_remote, master_repo, i18n_repo = _setup_two_repo(
        tmp_path, monkeypatch
    )
    base = master_repo.head.commit.hexsha
    txn_id = new_transaction_id()
    target = _prepared_target(
        master_repo,
        base,
        '{"value": "good"}',
        f"prepared\n\nSekai-Update-Txn: {txn_id}\nSekai-Update-Repo: master\n",
    )
    journal = _prepared_target_journal(master_repo, target, txn_id, base)
    main_before = master_repo.git.rev_parse("refs/heads/main")
    if mode == "detached":
        master_repo.git.checkout(base)
    else:
        master_repo.git.checkout("-b", "feature")
    _assert_prepared_target_rejected(master_repo, i18n_repo, monkeypatch, journal)
    assert master_repo.git.rev_parse("refs/heads/main") == main_before


def test_bound_loader_and_temporary_index_are_repo_local(tmp_path, monkeypatch):
    _master_remote, _i18n_remote, master_repo, i18n_repo = _setup_two_repo(
        tmp_path, monkeypatch
    )
    journal = _seed_journal_and_staging(
        master_repo, i18n_repo, new_transaction_id(), phase=TxnPhase.PUBLISHING
    )
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "i18n_diff_repo", i18n_repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_repo.working_dir)
    monkeypatch.setattr(cu, "i18n_diff_folder_path", i18n_repo.working_dir)
    loaded = cu._load_bound_journal()
    assert loaded.transaction_id == journal.transaction_id
    assert os.path.dirname(cu._temporary_index_path(master_repo)) == os.path.dirname(
        master_repo.index.path
    )
    assert cu._temporary_index_path(master_repo) != master_repo.index.path


# --------------------------------------------------------------------------- #
# A. Recovery runs before the ordinary new_version=False / maintenance gate
# --------------------------------------------------------------------------- #


def test_recovery_runs_before_gate_and_repushes_same_sha(tmp_path, monkeypatch):
    master_remote, i18n_remote, master_repo, i18n_repo = _setup_two_repo(
        tmp_path, monkeypatch
    )
    rpc = _rpc_calls()
    monkeypatch.setattr(cu.jsonrpc_client, "request", rpc)

    txn_id = new_transaction_id()
    _seed_journal_and_staging(master_repo, i18n_repo, txn_id, phase=TxnPhase.COMMITTING)
    _write_json(master_repo, "versions.json", CANDIDATE)
    _write_json(master_repo, "cards.json", [{"id": 1}])
    _write_json(i18n_repo, os.path.join("ja", "card_prefix.json"), {"1": "p"})
    cu._commit_enabled_repositories(
        [("master", master_repo), ("i18n", i18n_repo)],
        {
            "master": ["versions.json", "cards.json"],
            "i18n": [os.path.join("ja", "card_prefix.json")],
        },
        CANDIDATE,
    )
    j = TransactionJournal.load(master_repo.git_dir)
    j.update_repo(
        "master",
        commit_state=RepoCommitState.COMMITTED,
        target_commit_sha=master_repo.head.commit.hexsha,
    )
    j.update_repo(
        "i18n",
        commit_state=RepoCommitState.COMMITTED,
        target_commit_sha=i18n_repo.head.commit.hexsha,
    )

    status = cu._run_update_cycle_locked(daily=False)
    assert status == "recovered"
    # The gate RPCs must NOT have been called (recovery runs first).
    assert "check_versions" not in rpc
    # The remotes now hold the exact local SHAs (same SHA re-pushed).
    assert (
        git.Repo(master_remote).commit("main").hexsha == master_repo.head.commit.hexsha
    )
    assert git.Repo(i18n_remote).commit("main").hexsha == i18n_repo.head.commit.hexsha
    # Journal deleted after successful recovery.
    assert TransactionJournal.load(master_repo.git_dir) is None


# --------------------------------------------------------------------------- #
# B. Crash during publication -> recovery completes without duplicate commit
# --------------------------------------------------------------------------- #


def test_crash_during_publication_recovered_no_duplicate_commit(tmp_path, monkeypatch):
    master_remote, i18n_remote, master_repo, i18n_repo = _setup_two_repo(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(cu.jsonrpc_client, "request", lambda m, p=None: {})

    txn_id = new_transaction_id()
    _seed_journal_and_staging(master_repo, i18n_repo, txn_id, phase=TxnPhase.PUBLISHING)

    status = cu._run_update_cycle_locked(daily=True)
    assert status == "recovered"

    assert os.path.exists(os.path.join(master_repo.working_dir, "cards.json"))
    assert os.path.exists(os.path.join(master_repo.working_dir, "versions.json"))
    assert os.path.exists(os.path.join(i18n_repo.working_dir, "ja", "card_prefix.json"))
    assert len(list(master_repo.iter_commits("HEAD"))) == 1 + _base_commits(master_repo)
    assert len(list(i18n_repo.iter_commits("HEAD"))) == 1 + _base_commits(i18n_repo)
    assert (
        git.Repo(master_remote).commit("main").hexsha == master_repo.head.commit.hexsha
    )
    assert git.Repo(i18n_remote).commit("main").hexsha == i18n_repo.head.commit.hexsha
    assert TransactionJournal.load(master_repo.git_dir) is None


# --------------------------------------------------------------------------- #
# C. Crash after replace but before journal advances to COMMITTING
# --------------------------------------------------------------------------- #


def test_crash_after_replace_before_commit_no_duplicate_commit(tmp_path, monkeypatch):
    master_remote, i18n_remote, master_repo, i18n_repo = _setup_two_repo(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(cu.jsonrpc_client, "request", lambda m, p=None: {})

    txn_id = new_transaction_id()
    _seed_journal_and_staging(
        master_repo,
        i18n_repo,
        txn_id,
        published={
            "master": {"versions.json": CANDIDATE, "cards.json": [{"id": 1}]},
            "i18n": {os.path.join("ja", "card_prefix.json"): {"1": "p"}},
        },
        phase=TxnPhase.PUBLISHING,
    )

    status = cu._run_update_cycle_locked(daily=True)
    assert status == "recovered"
    assert len(list(master_repo.iter_commits("HEAD"))) == 1 + _base_commits(master_repo)
    assert len(list(i18n_repo.iter_commits("HEAD"))) == 1 + _base_commits(i18n_repo)
    assert TransactionJournal.load(master_repo.git_dir) is None


# --------------------------------------------------------------------------- #
# D. Remote already has target SHA; journal not advanced -> no duplicate push
# --------------------------------------------------------------------------- #


def test_remote_already_has_sha_no_duplicate_push(tmp_path, monkeypatch):
    master_remote, i18n_remote, master_repo, i18n_repo = _setup_two_repo(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(cu.jsonrpc_client, "request", lambda m, p=None: {})

    txn_id = new_transaction_id()
    _seed_journal_and_staging(master_repo, i18n_repo, txn_id, phase=TxnPhase.COMMITTING)
    _write_json(master_repo, "versions.json", CANDIDATE)
    _write_json(master_repo, "cards.json", [{"id": 1}])
    _write_json(i18n_repo, os.path.join("ja", "card_prefix.json"), {"1": "p"})
    cu._commit_enabled_repositories(
        [("master", master_repo), ("i18n", i18n_repo)],
        {
            "master": ["versions.json", "cards.json"],
            "i18n": [os.path.join("ja", "card_prefix.json")],
        },
        CANDIDATE,
    )
    m_sha = master_repo.head.commit.hexsha
    i_sha = i18n_repo.head.commit.hexsha
    master_repo.git.push("origin", "main")
    i18n_repo.git.push("origin", "main")

    j = TransactionJournal.load(master_repo.git_dir)
    j.update_repo(
        "master", commit_state=RepoCommitState.COMMITTED, target_commit_sha=m_sha
    )
    j.update_repo(
        "i18n", commit_state=RepoCommitState.COMMITTED, target_commit_sha=i_sha
    )

    push_count = {"n": 0}
    real_push = cu.push_current_head

    def _counting_push(repo, branch="main", **kwargs):
        push_count["n"] += 1
        return real_push(repo, branch=branch, **kwargs)

    monkeypatch.setattr(cu, "push_current_head", _counting_push)

    status = cu._run_update_cycle_locked(daily=True)
    assert status == "recovered"
    # The authoritative remote probe proves both targets are already present;
    # recovery must not issue duplicate pushes.
    assert push_count["n"] == 0
    assert git.Repo(master_remote).commit("main").hexsha == m_sha
    assert git.Repo(i18n_remote).commit("main").hexsha == i_sha
    assert TransactionJournal.load(master_repo.git_dir) is None


def test_probe_third_sha_retains_pending_push_state(tmp_path, monkeypatch):
    _master_remote, _i18n_remote, master_repo, i18n_repo = _setup_two_repo(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(cu.jsonrpc_client, "request", lambda m, p=None: {})
    txn_id = new_transaction_id()
    _seed_journal_and_staging(master_repo, i18n_repo, txn_id, phase=TxnPhase.COMMITTING)
    _write_json(master_repo, "versions.json", CANDIDATE)
    _write_json(master_repo, "cards.json", [{"id": 1}])
    _write_json(i18n_repo, os.path.join("ja", "card_prefix.json"), {"1": "p"})
    cu._commit_enabled_repositories(
        [("master", master_repo), ("i18n", i18n_repo)],
        {
            "master": ["versions.json", "cards.json"],
            "i18n": [os.path.join("ja", "card_prefix.json")],
        },
        CANDIDATE,
    )
    journal = TransactionJournal.load(master_repo.git_dir)
    assert journal is not None
    third = "f" * 40
    monkeypatch.setattr(cu, "_probe_remote", lambda *args: third)
    push = cu._recover_push(journal)
    assert push == "remote_mismatch:i18n"
    retained = TransactionJournal.load(master_repo.git_dir)
    assert retained.repos["i18n"].push_state is RepoPushState.PENDING


def test_endpoint_rebind_and_pushurl_ambiguity_fail_closed(tmp_path, monkeypatch):
    import check_update as cu

    _remote_a, _remote_b, repo, _i18n = _setup_two_repo(tmp_path, monkeypatch)
    remote = repo.remote("origin").url
    repo.git.config("--add", "remote.origin.pushurl", remote + "-other")
    with pytest.raises(cu.RemoteSnapshotError):
        cu._remote_endpoint(repo, "master")


# --------------------------------------------------------------------------- #
# E. Fail-closed: malformed journal / hash mismatch / external dirt
# --------------------------------------------------------------------------- #


def test_malformed_journal_fails_closed_no_generation(tmp_path, monkeypatch):
    master_remote, i18n_remote, master_repo, i18n_repo = _setup_two_repo(
        tmp_path, monkeypatch
    )
    rpc = _rpc_calls()
    monkeypatch.setattr(cu.jsonrpc_client, "request", rpc)
    gen_called = {"flag": False}
    monkeypatch.setattr(
        cu, "refresh_version", lambda *a, **k: gen_called.__setitem__("flag", True)
    )

    jdir = os.path.join(master_repo.git_dir, "sekai-update")
    os.makedirs(jdir, exist_ok=True)
    with open(os.path.join(jdir, "transaction.json"), "w") as f:
        f.write("not json at all")

    status = cu._run_update_cycle_locked(daily=False)
    assert status == "journal_invalid"
    assert gen_called["flag"] is False
    assert "check_versions" not in rpc


def test_staging_hash_mismatch_fails_closed(tmp_path, monkeypatch):
    master_remote, i18n_remote, master_repo, i18n_repo = _setup_two_repo(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(cu.jsonrpc_client, "request", lambda m, p=None: {})
    gen_called = {"flag": False}
    monkeypatch.setattr(
        cu, "refresh_version", lambda *a, **k: gen_called.__setitem__("flag", True)
    )

    txn_id = new_transaction_id()
    _seed_journal_and_staging(master_repo, i18n_repo, txn_id, phase=TxnPhase.PUBLISHING)
    bad_src = os.path.join(
        staging_dir_for(master_repo.working_dir, txn_id), "cards.json"
    )
    with open(bad_src, "w") as f:
        f.write('{"id": 999}')

    status = cu._run_update_cycle_locked(daily=True)
    assert status == "journal_invalid"
    assert gen_called["flag"] is False
    assert not os.path.exists(os.path.join(master_repo.working_dir, "cards.json"))


def test_manifest_external_dirt_fails_closed(tmp_path, monkeypatch):
    master_remote, i18n_remote, master_repo, i18n_repo = _setup_two_repo(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(cu.jsonrpc_client, "request", lambda m, p=None: {})
    gen_called = {"flag": False}
    monkeypatch.setattr(
        cu, "refresh_version", lambda *a, **k: gen_called.__setitem__("flag", True)
    )
    with open(os.path.join(master_repo.working_dir, "scratch.txt"), "w") as f:
        f.write("dirt")
    jdir = os.path.join(master_repo.git_dir, "sekai-update")
    os.makedirs(jdir, exist_ok=True)
    with open(os.path.join(jdir, "transaction.json"), "w") as f:
        f.write("@@@")

    status = cu._run_update_cycle_locked(daily=True)
    assert status == "journal_invalid"
    assert gen_called["flag"] is False
    assert os.path.exists(os.path.join(master_repo.working_dir, "scratch.txt"))


def test_committing_recovery_rejects_actual_external_dirt(tmp_path, monkeypatch):
    """A valid COMMITTING journal must reject real out-of-manifest worktree dirt."""
    _master_remote, _i18n_remote, master_repo, i18n_repo = _setup_two_repo(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(cu.jsonrpc_client, "request", lambda m, p=None: {})
    gen_called = {"flag": False}
    monkeypatch.setattr(
        cu,
        "refresh_version",
        lambda *a, **k: gen_called.__setitem__("flag", True),
    )

    txn_id = new_transaction_id()
    _seed_journal_and_staging(
        master_repo,
        i18n_repo,
        txn_id,
        published={
            "master": {"versions.json": CANDIDATE, "cards.json": [{"id": 1}]},
            "i18n": {os.path.join("ja", "card_prefix.json"): {"1": "p"}},
        },
        phase=TxnPhase.COMMITTING,
    )
    with open(os.path.join(master_repo.working_dir, "external-dirt.txt"), "w") as f:
        f.write("must not be staged")

    status = cu._run_update_cycle_locked(daily=True)

    assert status == "journal_invalid"
    assert gen_called["flag"] is False
    assert os.path.exists(os.path.join(master_repo.working_dir, "external-dirt.txt"))
    assert TransactionJournal.load(master_repo.git_dir) is not None
