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

from git import Repo

import check_update as cu
from utils.git import GitOutcome, prepare_repo_for_update

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

    # ----------------------------------------------------------------- PROOF B
    # Scenario 1: master remote pre-receive hook rejects every push.
    hook = _install_pre_receive_hook(
        master_remote, "echo 'rejecting master' >&2; exit 1"
    )
    push_status = cu._push_enabled_repositories(commits)
    assert push_status is not None
    assert push_status.startswith("push_failed:master:")
    assert push_status.endswith(":push_rejected")

    # i18n remote is still at base (push stops at the first failure; i18n is
    # never attempted).
    assert _remote_main_sha(i18n_remote) == i18n_base
    # Both local new SHAs are retained and the working trees are clean.
    assert master_repo.head.commit.hexsha == master_new
    assert i18n_repo.head.commit.hexsha == i18n_new
    assert not master_repo.is_dirty(untracked_files=True)
    assert not i18n_repo.is_dirty(untracked_files=True)

    # Snapshot the full commit-SHA lists before recovery.
    master_commits_before = _commit_sha_list(master_repo)
    i18n_commits_before = _commit_sha_list(i18n_repo)

    # ----------------------------------------------------------------- PROOF C
    # Remove the master hook; a real prepare must see "ahead" and push same SHA.
    os.remove(hook)
    m_recover = prepare_repo_for_update(master_repo, branch="main")
    assert m_recover.outcome is GitOutcome.OK
    assert m_recover.reason == "ahead_pushed"
    assert m_recover.local_sha == master_new
    assert m_recover.remote_sha == master_new
    assert _remote_main_sha(master_remote) == master_new

    # i18n is still ahead (never pushed in B); a real prepare recovers same SHA.
    i_recover = prepare_repo_for_update(i18n_repo, branch="main")
    assert i_recover.outcome is GitOutcome.OK
    assert i_recover.reason == "ahead_pushed"
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
    _seed_remote(
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
    master_new = master_repo.head.commit.hexsha
    i18n_new = i18n_repo.head.commit.hexsha
    assert commits["master"].outcome is GitOutcome.OK
    assert commits["i18n"].outcome is GitOutcome.OK

    # i18n remote rejects; master has no hook, so it succeeds.
    i18n_hook = _install_pre_receive_hook(
        i18n_remote, "echo 'rejecting i18n' >&2; exit 1"
    )
    push_status = cu._push_enabled_repositories(commits)
    assert push_status is not None
    assert push_status.startswith("push_failed:i18n:")
    assert push_status.endswith(":push_rejected")

    # Master remote has the new SHA; i18n remote still at base; i18n local
    # pending commit retained.
    assert _remote_main_sha(master_remote) == master_new
    assert _remote_main_sha(i18n_remote) == i18n_base
    assert i18n_repo.head.commit.hexsha == i18n_new
    assert not i18n_repo.is_dirty(untracked_files=True)

    # A later real prepare on i18n (with the hook removed, as the server would
    # eventually accept) recovers the SAME SHA with NO second commit.
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
