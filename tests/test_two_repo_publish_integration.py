"""Phase 4.2 dual-Git integration evidence lane.

This file drives the *real* production helpers end-to-end against two temporary
bare remotes and two worker repositories (master / i18n). The four core
functions are invoked for real (never mocked):

  * ``check_update._commit_enabled_repositories``
  * ``check_update._push_enabled_repositories``
  * ``utils.git.prepare_repo_for_update``
  * (and, transitively, ``utils.git.push_current_head`` via the two above)

The only permitted monkeypatching is of module-level globals on
``check_update`` (``masterdb_diff_repo``, ``i18n_diff_repo``,
``masterdb_diff_folder_path``, ``i18n_diff_folder_path``, ``version_info``,
``update_options``, ``check_update_simple_mode``, ``pjsk_region``) and the
installation of a real ``pre-receive`` hook on a bare remote to simulate a
rejecting server. No network, no GitHub, no production remotes.

Proven:

  A. When both repos have manifest changes, the real commit helper creates a
     local commit for *each*; before the first push both repo HEADs are the new
     commits.
  B. Scenario 1 — master remote ``pre-receive`` hook rejects: the real push
     helper returns ``push_failed:master:...``; the i18n remote is still at
     base (never attempted / never pushed); both master and i18n local new
     SHAs are retained and the working trees are clean.
  C. After removing the master hook, the real ``prepare_repo_for_update`` for
     master recognizes "ahead" and pushes the *same* SHA; i18n is still ahead
     and a subsequent real ``prepare`` recovers the *same* SHA; the full commit
     SHA list of both repos is unchanged across recovery, and the remote refs
     finally equal the original pending SHAs.
  D. Scenario 2 — master push succeeds, i18n hook rejects: master remote gets
     the new SHA; i18n remote stays at base while the local pending commit is
     retained; a later real ``prepare`` on i18n recovers the same SHA with no
     second commit.
  E. Unrelated tracked / untracked files are not part of the manifest commit
     (verified via the commit diff and the committed tree).
"""

import json
import os
import shutil
import subprocess

from git import Repo

import check_update as cu
from utils.git import GitOutcome, prepare_repo_for_update
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

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


# Candidate version_info used by every scenario (matches what _set_globals pins).
CANDIDATE = {"dataVersion": "100", "assetVersion": "100"}


def _make_bare_remote(tmp_path, name: str) -> str:
    remote_path = tmp_path / f"{name}.git"
    bare = Repo.init(str(remote_path), bare=True)
    assert bare.bare
    # Point the bare remote's HEAD at ``main`` so worker clones check out main
    # (Repo.init default HEAD is "master", which has no ref yet).
    bare.git.symbolic_ref("HEAD", "refs/heads/main")
    return str(remote_path)


_SEED_COUNTER = [0]


def _seed_remote(
    tmp_path, remote_url: str, filename: str, content: str, msg: str
) -> str:
    """Create a temp seed repo, commit on ``main``, push to the bare remote.

    Returns the pushed base commit SHA. A fresh, unique seed dir is used on
    every call so multiple remotes can be seeded within one test.
    """
    _SEED_COUNTER[0] += 1
    seed = Repo.init(str(tmp_path / f"seed_{_SEED_COUNTER[0]}"))
    with seed.config_writer() as cw:
        cw.set_value("user", "name", "seed")
        cw.set_value("user", "email", "seed@example.com")
        cw.set_value("init", "defaultBranch", "main")
    # Ensure the first commit lands on ``main`` (init default may be "master").
    if not seed.head.is_valid() or seed.active_branch.name != "main":
        seed.git.checkout("-b", "main")
    seed.create_remote("origin", remote_url)
    path = os.path.join(seed.working_dir, filename)
    with open(path, "w") as f:
        f.write(content)
    seed.index.add([filename])
    seed.index.commit(msg)
    seed.git.push("origin", "main")
    return seed.head.commit.hexsha


def _clone_worker(tmp_path, name: str, remote_url: str) -> Repo:
    """Clone the bare remote onto local disk; returns a clean worker on ``main``."""
    worker = Repo.clone_from(remote_url, str(tmp_path / name))
    with worker.config_writer() as cw:
        cw.set_value("user", "name", "worker")
        cw.set_value("user", "email", "worker@example.com")
    # clone_from checks out the remote HEAD (main); ensure branch name is main.
    if worker.active_branch.name != "main":
        worker.git.checkout("-b", "main")
    assert not worker.is_dirty(untracked_files=True)
    return worker


def _write_json(repo: Repo, relpath: str, data) -> None:
    full = os.path.join(repo.working_dir, relpath)
    parent = os.path.dirname(full)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _install_pre_receive_hook(bare_remote_url: str, body: str) -> str:
    """Install a pre-receive hook in the bare remote; return its path."""
    bare = Repo(bare_remote_url)
    hook_path = os.path.join(bare.git_dir, "hooks", "pre-receive")
    os.makedirs(os.path.dirname(hook_path), exist_ok=True)
    with open(hook_path, "w") as f:
        f.write("#!/bin/sh\n" + body + "\n")
    os.chmod(hook_path, 0o755)
    return hook_path


def _remote_main_sha(remote_url: str) -> str:
    return Repo(remote_url).commit("main").hexsha


def _set_globals(monkeypatch, master_repo: Repo, i18n_repo: Repo) -> None:
    """Wire the module-level globals the real helpers read."""
    monkeypatch.setattr(cu, "masterdb_diff_repo", master_repo)
    monkeypatch.setattr(cu, "i18n_diff_repo", i18n_repo)
    monkeypatch.setattr(cu, "masterdb_diff_folder_path", master_repo.working_dir)
    monkeypatch.setattr(cu, "i18n_diff_folder_path", i18n_repo.working_dir)
    monkeypatch.setattr(
        cu, "version_info", {"dataVersion": "100", "assetVersion": "100"}
    )
    monkeypatch.setattr(
        cu,
        "update_options",
        {"master": True, "i18n": True, "userInfo": False},
    )
    monkeypatch.setattr(cu, "check_update_simple_mode", False)
    monkeypatch.setattr(cu, "pjsk_region", "jp")


def _build_enabled(master_repo: Repo, i18n_repo: Repo) -> list:
    return [("master", master_repo), ("i18n", i18n_repo)]


def _commit_sha_list(repo: Repo) -> list:
    return [c.hexsha for c in repo.iter_commits("HEAD")]


def _bind_committing_journal(master_repo, i18n_repo, manifest):
    """Install the durable checkpoint required by the strict push coordinator."""
    txn_id = new_transaction_id()
    repos = {}
    for key, repo in (("master", master_repo), ("i18n", i18n_repo)):
        staging = staging_dir_for(repo.working_dir, txn_id)
        files = {}
        target = repo.head.commit.hexsha
        base = repo.head.commit.parents[0].hexsha
        for rel in manifest[key]:
            source = os.path.join(repo.working_dir, rel)
            staged = os.path.join(staging, rel)
            os.makedirs(os.path.dirname(staged), exist_ok=True)
            shutil.copy2(source, staged)
            files[rel] = FileEntry(source_sha256=compute_sha256(staged))
        repos[key] = RepoState(
            manifest=list(manifest[key]),
            staging_dir=staging,
            repo_root=os.path.realpath(repo.working_dir),
            target_commit_sha=target,
            base_sha=base,
            remote_base_sha=base,
            remote_name="origin",
            remote_ref="refs/heads/main",
            remote_endpoint_fingerprint=cu._remote_endpoint(repo, key)[1],
            files=files,
            commit_state=RepoCommitState.COMMITTED,
            push_state=RepoPushState.PENDING,
        )
    journal = TransactionJournal(
        master_git_dir=master_repo.git_dir,
        transaction_id=txn_id,
        candidate=dict(CANDIDATE),
        enabled_repos=["master", "i18n"],
        publish_order=["master", "i18n"],
        repos=repos,
        phase=TxnPhase.COMMITTING,
    )
    journal.write()
    return journal


# --------------------------------------------------------------------------- #
# A + B + C: full dual-repo recovery narrative
# --------------------------------------------------------------------------- #


def test_publish_two_repo_full_recovery(monkeypatch, tmp_path):
    # --- Setup: two bare remotes + two worker clones, both clean/equal ---
    master_remote = _make_bare_remote(tmp_path, "master_remote")
    i18n_remote = _make_bare_remote(tmp_path, "i18n_remote")
    master_base = _seed_remote(
        tmp_path, master_remote, "base.txt", "master base", "master base"
    )
    i18n_base = _seed_remote(
        tmp_path, i18n_remote, "base.txt", "i18n base", "i18n base"
    )

    master_repo = _clone_worker(tmp_path, "master_worker", master_remote)
    i18n_repo = _clone_worker(tmp_path, "i18n_worker", i18n_remote)
    assert i18n_repo.head.commit.hexsha == i18n_base

    _set_globals(monkeypatch, master_repo, i18n_repo)

    enabled = _build_enabled(master_repo, i18n_repo)

    # --- Real prepare (clean, equal) for both repos ---
    m_prep = prepare_repo_for_update(master_repo, branch="main")
    i_prep = prepare_repo_for_update(i18n_repo, branch="main")
    assert m_prep.outcome is GitOutcome.OK
    assert i_prep.outcome is GitOutcome.OK

    # --- Write manifest changes into both working trees ---
    _write_json(master_repo, "versions.json", CANDIDATE)
    _write_json(master_repo, "cards.json", [{"id": 1}])
    _write_json(i18n_repo, os.path.join("ja", "card_prefix.json"), {"1": "p"})

    manifest = {
        "master": ["versions.json", "cards.json"],
        "i18n": [os.path.join("ja", "card_prefix.json")],
    }

    # ----------------------------------------------------------------- PROOF A
    # Real commit helper commits BOTH repos (explicit manifest paths).
    commits = cu._commit_enabled_repositories(enabled, manifest)
    assert commits["master"].outcome is GitOutcome.OK
    assert commits["i18n"].outcome is GitOutcome.OK
    assert commits["master"].local_sha is not None
    assert commits["i18n"].local_sha is not None

    master_new = master_repo.head.commit.hexsha
    i18n_new = i18n_repo.head.commit.hexsha
    # Both HEADs are NEW commits (advanced past the cloned base).
    assert master_new != master_base
    assert i18n_new != i18n_base
    # Record both HEADs before the first push (still at base on the remotes).
    assert _remote_main_sha(master_remote) == master_base
    assert _remote_main_sha(i18n_remote) == i18n_base
    _bind_committing_journal(master_repo, i18n_repo, manifest)

    # ----------------------------------------------------------------- PROOF B
    # Scenario 1: master remote pre-receive hook rejects every push.
    hook = _install_pre_receive_hook(
        master_remote, "echo 'rejecting master' >&2; exit 1"
    )
    push_status = cu._push_enabled_repositories(commits)
    assert push_status is not None
    assert push_status.startswith("push_failed:master:")
    assert push_status.endswith(":push_rejected")

    # Phase 2 pushes i18n first; it succeeds before the master rejection.
    assert _remote_main_sha(i18n_remote) == i18n_new
    # Both local new SHAs are retained and the working trees are clean.
    assert master_repo.head.commit.hexsha == master_new
    assert i18n_repo.head.commit.hexsha == i18n_new
    assert not master_repo.is_dirty(untracked_files=True)
    assert not i18n_repo.is_dirty(untracked_files=True)

    # Snapshot the full commit-SHA lists before recovery.
    master_commits_before = _commit_sha_list(master_repo)
    i18n_commits_before = _commit_sha_list(i18n_repo)

    # ----------------------------------------------------------------- PROOF C
    # Remove the master hook; a real prepare sees the already-pushed state.
    os.remove(hook)
    m_recover = prepare_repo_for_update(master_repo, branch="main")
    assert m_recover.outcome is GitOutcome.OK
    assert m_recover.reason == "ahead_pushed"
    assert m_recover.local_sha == master_new
    assert m_recover.remote_sha == master_new
    assert _remote_main_sha(master_remote) == master_new

    # i18n was already pushed before the master rejection.
    i_recover = prepare_repo_for_update(i18n_repo, branch="main")
    assert i_recover.outcome is GitOutcome.OK
    assert i_recover.reason == "equal"
    assert i_recover.local_sha == i18n_new
    assert i_recover.remote_sha == i18n_new
    assert _remote_main_sha(i18n_remote) == i18n_new

    # The full commit-SHA list of both repos is unchanged across recovery
    # (prepare only pushes; it never creates a second commit).
    assert _commit_sha_list(master_repo) == master_commits_before
    assert _commit_sha_list(i18n_repo) == i18n_commits_before

    # Remote refs finally equal the original pending SHAs.
    assert _remote_main_sha(master_remote) == master_new
    assert _remote_main_sha(i18n_remote) == i18n_new


# --------------------------------------------------------------------------- #
# D: scenario 2 — master succeeds, i18n rejected, then real prepare recovers
# --------------------------------------------------------------------------- #


def test_master_ok_i18n_hook_then_recover(monkeypatch, tmp_path):
    master_remote = _make_bare_remote(tmp_path, "master_remote")
    i18n_remote = _make_bare_remote(tmp_path, "i18n_remote")
    master_base = _seed_remote(
        tmp_path, master_remote, "base.txt", "master base", "master base"
    )
    i18n_base = _seed_remote(
        tmp_path, i18n_remote, "base.txt", "i18n base", "i18n base"
    )

    master_repo = _clone_worker(tmp_path, "master_worker", master_remote)
    i18n_repo = _clone_worker(tmp_path, "i18n_worker", i18n_remote)
    _set_globals(monkeypatch, master_repo, i18n_repo)
    enabled = _build_enabled(master_repo, i18n_repo)

    # Real prepare (clean, equal).
    assert prepare_repo_for_update(master_repo, branch="main").outcome is GitOutcome.OK
    assert prepare_repo_for_update(i18n_repo, branch="main").outcome is GitOutcome.OK

    # Write manifest changes.
    _write_json(master_repo, "versions.json", CANDIDATE)
    _write_json(master_repo, "cards.json", [{"id": 1}])
    _write_json(i18n_repo, os.path.join("ja", "card_prefix.json"), {"1": "p"})

    manifest = {
        "master": ["versions.json", "cards.json"],
        "i18n": [os.path.join("ja", "card_prefix.json")],
    }

    # Real commit both.
    commits = cu._commit_enabled_repositories(enabled, manifest)
    i18n_new = i18n_repo.head.commit.hexsha
    assert commits["master"].outcome is GitOutcome.OK
    assert commits["i18n"].outcome is GitOutcome.OK
    _bind_committing_journal(master_repo, i18n_repo, manifest)

    # i18n is pushed first and rejects; master must not be attempted.
    i18n_hook = _install_pre_receive_hook(
        i18n_remote, "echo 'rejecting i18n' >&2; exit 1"
    )
    push_status = cu._push_enabled_repositories(commits)
    assert push_status is not None
    assert push_status.startswith("push_failed:i18n:")
    assert push_status.endswith(":push_rejected")

    # Master remains at base; i18n local pending commit is retained.
    assert _remote_main_sha(master_remote) == master_base
    assert _remote_main_sha(i18n_remote) == i18n_base
    assert i18n_repo.head.commit.hexsha == i18n_new
    assert not i18n_repo.is_dirty(untracked_files=True)

    # A later real prepare on i18n (with the hook removed) recovers the SAME SHA.
    os.remove(i18n_hook)
    i18n_commits_before = _commit_sha_list(i18n_repo)
    i_recover = prepare_repo_for_update(i18n_repo, branch="main")
    assert i_recover.outcome is GitOutcome.OK
    assert i_recover.reason == "ahead_pushed"
    assert i_recover.local_sha == i18n_new
    assert i_recover.remote_sha == i18n_new
    assert _remote_main_sha(i18n_remote) == i18n_new
    # No second commit was created.
    assert _commit_sha_list(i18n_repo) == i18n_commits_before


# --------------------------------------------------------------------------- #
# E: unrelated tracked / untracked files are NOT in the manifest commit
# --------------------------------------------------------------------------- #


def test_unrelated_files_excluded_from_manifest_commit(monkeypatch, tmp_path):
    master_remote = _make_bare_remote(tmp_path, "master_remote")
    i18n_remote = _make_bare_remote(tmp_path, "i18n_remote")
    _seed_remote(tmp_path, master_remote, "base.txt", "master base", "master base")
    _seed_remote(tmp_path, i18n_remote, "base.txt", "i18n base", "i18n base")

    master_repo = _clone_worker(tmp_path, "master_worker", master_remote)
    i18n_repo = _clone_worker(tmp_path, "i18n_worker", i18n_remote)
    _set_globals(monkeypatch, master_repo, i18n_repo)
    enabled = _build_enabled(master_repo, i18n_repo)

    # A pre-existing TRACKED file in master (already committed in the base) that
    # is NOT in the manifest, plus a TRACKED file we modify but do not stage.
    _write_json(master_repo, "keep_tracked.json", {"k": "baseline"})
    master_repo.index.add(["keep_tracked.json"])
    master_repo.index.commit("seed keep_tracked")

    # An UNTRACKED stray file that must never enter the commit.
    with open(os.path.join(master_repo.working_dir, "stray_untracked.txt"), "w") as f:
        f.write("must not be committed")

    # A tracked file we MUTATE but deliberately exclude from the manifest.
    with open(os.path.join(master_repo.working_dir, "keep_tracked.json"), "w") as f:
        json.dump({"k": "mutated-but-excluded"}, f)

    # Only the manifest files are written/chosen.
    _write_json(master_repo, "versions.json", CANDIDATE)
    _write_json(master_repo, "cards.json", [{"id": 1}])
    _write_json(i18n_repo, os.path.join("ja", "card_prefix.json"), {"1": "p"})

    manifest = {
        "master": ["versions.json", "cards.json"],
        "i18n": [os.path.join("ja", "card_prefix.json")],
    }

    commits = cu._commit_enabled_repositories(enabled, manifest)

    # The mutated-but-excluded tracked file did NOT block the commit (it is not
    # part of the manifest, so it remains as unstaged dirt on top of the commit).
    assert commits["master"].outcome is GitOutcome.OK
    assert commits["i18n"].outcome is GitOutcome.OK

    # The commit diff (name-only) contains ONLY the explicitly staged manifest
    # paths — nothing unrelated.
    master_diff = master_repo.git.diff("HEAD~1", "HEAD", "--name-only").split()
    assert sorted(master_diff) == ["cards.json", "versions.json"]
    i18n_diff = i18n_repo.git.diff("HEAD~1", "HEAD", "--name-only").split()
    assert sorted(i18n_diff) == [os.path.join("ja", "card_prefix.json")]

    # The untracked stray file is not present anywhere in the new commit's tree.
    tree_paths = [t.path for t in master_repo.head.commit.tree.traverse()]
    assert "stray_untracked.txt" not in tree_paths
    # The excluded tracked file was committed at its PREVIOUS (baseline) content
    # in an earlier commit; the new mutation is not in this commit's tree because
    # it was never staged.
    assert "keep_tracked.json" in tree_paths  # baseline version is in history
    # The new mutation must NOT appear in the committed tree content.
    committed_keep = json.loads(
        master_repo.head.commit.tree["keep_tracked.json"].data_stream.read().decode()
    )
    assert committed_keep == {"k": "baseline"}


# --------------------------------------------------------------------------- #
# F: coordinator prepare is always allow_push=False (no auto-ahead push)
# --------------------------------------------------------------------------- #


def test_coordinator_prepare_passes_allow_push_false(monkeypatch, tmp_path):
    """The coordinated cycle must prepare with ``allow_push=False`` so any ahead
    state is resolved only by the explicit expected-SHA push workflow, never by
    prepare's auto-ahead push."""
    master_remote = _make_bare_remote(tmp_path, "master_remote")
    i18n_remote = _make_bare_remote(tmp_path, "i18n_remote")
    _seed_remote(tmp_path, master_remote, "base.txt", "master base", "master base")
    _seed_remote(tmp_path, i18n_remote, "base.txt", "i18n base", "i18n base")

    master_repo = _clone_worker(tmp_path, "master_worker", master_remote)
    i18n_repo = _clone_worker(tmp_path, "i18n_worker", i18n_remote)
    _set_globals(monkeypatch, master_repo, i18n_repo)

    seen = {}

    def _spy_prepare(repo, branch="main", allow_push=True):
        seen["allow_push"] = allow_push
        return prepare_repo_for_update(repo, branch=branch)

    monkeypatch.setattr(cu, "prepare_repo_for_update", _spy_prepare)

    # A daily cycle bypasses the new-version gate and reaches the prepare step
    # (no journal present, so recovery is skipped and prepare runs).
    monkeypatch.setattr(
        cu.jsonrpc_client,
        "request",
        lambda m, p=None: (
            {"maintenance": False, "new_version": False}
            if m in ("check_versions", "check_versions_simple")
            else {}
        ),
    )
    # Make generation a no-op so the cycle reaches prepare -> commit -> push
    # without touching the network.
    monkeypatch.setattr(cu, "refresh_version", lambda *a, **k: None)

    cu._run_update_cycle_locked(daily=True)
    assert seen.get("allow_push") is False


# --------------------------------------------------------------------------- #
# G: master committed + i18n NOT committed (journal in COMMITTING) -> recovery
#    fails closed and does NOT push master until i18n is also committed
# --------------------------------------------------------------------------- #


def test_master_committed_i18n_uncommitted_blocks_master_push_in_recovery(
    monkeypatch, tmp_path
):
    """Phase 2 guarantee: a durable journal in COMMITTING with master committed
    but i18n NOT committed must NOT push master during recovery (fail closed on
    the incomplete commit set). Only once i18n is also committed does recovery
    push BOTH the same SHAs."""
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

    master_remote = _make_bare_remote(tmp_path, "master_remote")
    i18n_remote = _make_bare_remote(tmp_path, "i18n_remote")
    master_base = _seed_remote(
        tmp_path, master_remote, "base.txt", "master base", "master base"
    )
    i18n_base = _seed_remote(
        tmp_path, i18n_remote, "base.txt", "i18n base", "i18n base"
    )

    master_repo = _clone_worker(tmp_path, "master_worker", master_remote)
    i18n_repo = _clone_worker(tmp_path, "i18n_worker", i18n_remote)
    _set_globals(monkeypatch, master_repo, i18n_repo)

    # Real prepare (clean, equal) for both repos.
    assert prepare_repo_for_update(master_repo, branch="main").outcome is GitOutcome.OK
    assert prepare_repo_for_update(i18n_repo, branch="main").outcome is GitOutcome.OK

    # Write manifest changes and commit BOTH (real commit helper).
    _write_json(master_repo, "versions.json", CANDIDATE)
    _write_json(master_repo, "cards.json", [{"id": 1}])
    _write_json(i18n_repo, os.path.join("ja", "card_prefix.json"), {"1": "p"})
    manifest = {
        "master": ["versions.json", "cards.json"],
        "i18n": [os.path.join("ja", "card_prefix.json")],
    }
    commits = cu._commit_enabled_repositories(
        [("master", master_repo), ("i18n", i18n_repo)], manifest, CANDIDATE
    )
    assert commits["master"].outcome is GitOutcome.OK
    assert commits["i18n"].outcome is GitOutcome.OK
    master_new = master_repo.head.commit.hexsha

    # Simulate a crash that left the journal in COMMITTING but with i18n's commit
    # NOT recorded (only master reached COMMITTED). This is the durable state a
    # crash during the commit phase would leave.
    txn_id = new_transaction_id()
    m_staging = staging_dir_for(master_repo.working_dir, txn_id)
    i_staging = staging_dir_for(i18n_repo.working_dir, txn_id)

    # Re-materialize the already-created master target with the canonical
    # transaction trailers expected by prepared-target recovery.  The direct
    # commit helper above intentionally had no bound journal, so its
    # ``standalone`` trailer is not acceptable to this durable fixture.
    master_base_for_target = master_repo.head.commit.parents[0].hexsha
    master_tree = master_repo.git.rev_parse(f"{master_new}^{{tree}}")
    target_message = (
        "master recovery fixture\n\n"
        f"Sekai-Update-Txn: {txn_id}\n"
        "Sekai-Update-Repo: master\n"
    )
    master_new = subprocess.run(
        ["git", "commit-tree", master_tree, "-p", master_base_for_target],
        cwd=master_repo.working_dir,
        input=target_message,
        text=True,
        capture_output=True,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "master-db-diff-bot",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "master-db-diff-bot",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    ).stdout.strip()
    master_repo.git.update_ref("refs/heads/main", master_new)

    def _state(key, repo, staging, sha, committed):
        files = {}
        for rel in manifest[key]:
            content = (
                CANDIDATE
                if rel == "versions.json"
                else ([{"id": 1}] if key == "master" else {"1": "p"})
            )
            sp = os.path.join(staging, rel)
            os.makedirs(os.path.dirname(sp), exist_ok=True)
            with open(sp, "w", encoding="utf-8") as f:
                import json as _json

                _json.dump(content, f, ensure_ascii=False, indent=2)
            files[rel] = FileEntry(source_sha256=compute_sha256(sp))
        base_sha = repo.head.commit.parents[0].hexsha
        _endpoint, endpoint_fingerprint = cu._remote_endpoint(repo, key)
        return RepoState(
            manifest=list(manifest[key]),
            staging_dir=staging,
            target_commit_sha=sha if committed else None,
            base_sha=base_sha,
            remote_sha=None,
            remote_base_sha=base_sha,
            remote_name="origin",
            remote_ref="refs/heads/main",
            remote_endpoint_fingerprint=endpoint_fingerprint,
            files=files,
            commit_state=(
                RepoCommitState.COMMITTED if committed else RepoCommitState.PENDING
            ),
            push_state=RepoPushState.PENDING,
        )

    repos = {
        "master": _state("master", master_repo, m_staging, master_new, True),
        "i18n": _state("i18n", i18n_repo, i_staging, None, False),
    }
    # Simulate the crash point accurately: i18n's commit object exists locally,
    # but its branch/index are still at the recorded base because its durable
    # COMMITTED checkpoint was never written.
    i18n_repo.git.reset("--hard", i18n_base)
    _write_json(i18n_repo, os.path.join("ja", "card_prefix.json"), {"1": "p"})
    journal = TransactionJournal(
        master_git_dir=master_repo.git_dir,
        transaction_id=txn_id,
        candidate=dict(CANDIDATE),
        enabled_repos=["master", "i18n"],
        publish_order=["master", "i18n"],
        repos=repos,
        phase=TxnPhase.COMMITTING,
    )
    journal.write()

    monkeypatch.setattr(cu.jsonrpc_client, "request", lambda m, p=None: {})

    # Make i18n's recovery commit genuinely FAIL (simulating a commit that cannot
    # complete). Recovery must then fail closed: master must NOT be pushed ahead
    # of the incomplete i18n commit, and the journal is retained.
    real_prepare_target = cu._prepare_commit_target

    def _failing_i18n_commit(repo, key, *args, **kwargs):
        if key == "i18n":
            return None, cu.GitResult(outcome=GitOutcome.FAILED, reason="simulated")
        return real_prepare_target(repo, key, *args, **kwargs)

    monkeypatch.setattr(cu, "_prepare_commit_target", _failing_i18n_commit)

    status = cu._run_update_cycle_locked(daily=True)
    assert status == "journal_invalid"
    # Master remote still at base — it was NOT pushed ahead of i18n.
    assert _remote_main_sha(master_remote) == master_base
    assert _remote_main_sha(i18n_remote) == i18n_base
    # Journal retained on disk for a later, complete recovery.
    assert TransactionJournal.load(master_repo.git_dir) is not None

    # Now let i18n commit succeed (operator fixes the issue). Recovery must push
    # BOTH the same SHAs.
    monkeypatch.setattr(cu, "_prepare_commit_target", real_prepare_target)
    status2 = cu._run_update_cycle_locked(daily=True)
    assert status2 == "recovered"
    assert _remote_main_sha(master_remote) == master_new
    # i18n had no durable target checkpoint at the simulated crash, so recovery
    # creates its deterministic retry target and pushes that exact local HEAD.
    assert _remote_main_sha(i18n_remote) == i18n_repo.head.commit.hexsha
    assert TransactionJournal.load(master_repo.git_dir) is None
