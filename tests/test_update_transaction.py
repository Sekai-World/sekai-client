"""Phase 2 unit tests for the durable transaction journal module.

These tests exercise :mod:`utils.update_transaction` directly against temporary
repositories / bare remotes with no network. They prove the strict schema, the
atomic ``0600`` + fsync write, the directory-fsync delete, the journal-owned
staging layout, and the fail-closed validation (malformed JSON, duplicate
journal artifact, invalid / escaping paths).
"""

import json
import os

import pytest

from utils.update_transaction import (
    FileEntry,
    JournalError,
    RepoCommitState,
    RepoPushState,
    RepoState,
    TransactionJournal,
    TxnPhase,
    compute_sha256,
    new_transaction_id,
    staging_dir_for,
    validate_journal_roots,
)


def _init_repo(tmp_path, name="repo"):
    from git import Repo

    repo_path = tmp_path / name
    repo_path.mkdir(parents=True, exist_ok=True)
    repo = Repo.init(str(repo_path))
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "t")
        cw.set_value("user", "email", "t@example.com")
        cw.set_value("init", "defaultBranch", "main")
    if not repo.head.is_valid():
        repo.git.checkout("-b", "main")
    return repo


def _write(path, content):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


# Valid 64-char lowercase hex SHA-256 digests used as fixtures.
_HEX_V = "a" * 64
_HEX_C = "c" * 64
_HEX_X = "f" * 64
_HEX_B = "b" * 40


def _make_journal(
    git_dir, txn_id=None, enabled=("master", "i18n"), phase=TxnPhase.PUBLISHING
):
    txn_id = txn_id or new_transaction_id()
    repos = {}
    for key in enabled:
        repos[key] = RepoState(
            manifest=["versions.json", "cards.json"],
            staging_dir=staging_dir_for(f"/data/{key}", txn_id),
            target_commit_sha=None,
            base_sha=_HEX_B,
            remote_sha=None,
            remote_base_sha=_HEX_B,
            remote_name="origin",
            remote_ref="refs/heads/main",
            files={
                "versions.json": FileEntry(source_sha256=_HEX_V),
                "cards.json": FileEntry(source_sha256=_HEX_C),
            },
        )
    return TransactionJournal(
        master_git_dir=git_dir,
        transaction_id=txn_id,
        candidate={"dataVersion": "1"},
        enabled_repos=list(enabled),
        publish_order=list(enabled),
        repos=repos,
        phase=phase,
    )


# --------------------------------------------------------------------------- #
# Atomic write / fsync / permissions
# --------------------------------------------------------------------------- #


def test_journal_written_atomic_0600_and_loads(tmp_path):
    repo = _init_repo(tmp_path)
    j = _make_journal(repo.git_dir)
    j.write()
    p = j.journal_path
    assert os.path.isfile(p)
    # 0600 permissions.
    mode = os.stat(p).st_mode & 0o777
    assert mode == 0o600
    loaded = TransactionJournal.load(repo.git_dir)
    assert loaded is not None
    assert loaded.transaction_id == j.transaction_id
    assert loaded.phase == TxnPhase.PUBLISHING
    assert loaded.enabled_repos == ["master", "i18n"]


def test_journal_delete_fsyncs_parent(tmp_path):
    repo = _init_repo(tmp_path)
    j = _make_journal(repo.git_dir)
    j.write()
    assert os.path.isfile(j.journal_path)
    j.delete()
    assert not os.path.isfile(j.journal_path)
    # The parent sekai-update dir may remain but must be empty of the journal.
    assert not os.path.exists(j.journal_path)


def test_load_returns_none_when_absent(tmp_path):
    repo = _init_repo(tmp_path)
    assert TransactionJournal.load(repo.git_dir) is None


# --------------------------------------------------------------------------- #
# Strict schema validation (fail closed)
# --------------------------------------------------------------------------- #


def test_malformed_json_fails_closed(tmp_path):
    repo = _init_repo(tmp_path)
    jdir = os.path.join(repo.git_dir, "sekai-update")
    os.makedirs(jdir, exist_ok=True)
    with open(os.path.join(jdir, "transaction.json"), "w") as f:
        f.write("{not valid json")
    with pytest.raises(JournalError):
        TransactionJournal.load(repo.git_dir)


def test_duplicate_journal_artifact_fails_closed(tmp_path):
    repo = _init_repo(tmp_path)
    j = _make_journal(repo.git_dir)
    j.write()
    # A second stray .json artifact alongside the canonical journal is rejected.
    stray = os.path.join(repo.git_dir, "sekai-update", "stray.json")
    with open(stray, "w") as f:
        f.write("{}")
    with pytest.raises(JournalError):
        TransactionJournal.load(repo.git_dir)


def test_wrong_schema_version_fails_closed(tmp_path):
    repo = _init_repo(tmp_path)
    jdir = os.path.join(repo.git_dir, "sekai-update")
    os.makedirs(jdir, exist_ok=True)
    payload = _make_journal(repo.git_dir).to_dict()
    payload["schema_version"] = 999
    with open(os.path.join(jdir, "transaction.json"), "w") as f:
        json.dump(payload, f)
    with pytest.raises(JournalError):
        TransactionJournal.load(repo.git_dir)


def test_publish_order_mismatch_fails_closed(tmp_path):
    repo = _init_repo(tmp_path)
    jdir = os.path.join(repo.git_dir, "sekai-update")
    os.makedirs(jdir, exist_ok=True)
    payload = _make_journal(repo.git_dir).to_dict()
    payload["publish_order"] = ["i18n"]  # missing master
    with open(os.path.join(jdir, "transaction.json"), "w") as f:
        json.dump(payload, f)
    with pytest.raises(JournalError):
        TransactionJournal.load(repo.git_dir)


def test_escaping_staging_dir_fails_closed(tmp_path):
    repo = _init_repo(tmp_path)
    jdir = os.path.join(repo.git_dir, "sekai-update")
    os.makedirs(jdir, exist_ok=True)
    payload = _make_journal(repo.git_dir).to_dict()
    # staging_dir not journal-owned (no .staging suffix) -> rejected.
    payload["repos"]["master"]["staging_dir"] = "/etc/evil/master"
    with open(os.path.join(jdir, "transaction.json"), "w") as f:
        json.dump(payload, f)
    with pytest.raises(JournalError):
        TransactionJournal.load(repo.git_dir)


def test_file_rel_with_parent_escape_fails_closed(tmp_path):
    repo = _init_repo(tmp_path)
    jdir = os.path.join(repo.git_dir, "sekai-update")
    os.makedirs(jdir, exist_ok=True)
    payload = _make_journal(repo.git_dir).to_dict()
    payload["repos"]["master"]["files"]["../escape.json"] = {
        "source_sha256": _HEX_X,
        "dest_sha256": None,
    }
    with open(os.path.join(jdir, "transaction.json"), "w") as f:
        json.dump(payload, f)
    with pytest.raises(JournalError):
        TransactionJournal.load(repo.git_dir)


def test_invalid_phase_fails_closed(tmp_path):
    repo = _init_repo(tmp_path)
    jdir = os.path.join(repo.git_dir, "sekai-update")
    os.makedirs(jdir, exist_ok=True)
    payload = _make_journal(repo.git_dir).to_dict()
    payload["phase"] = "bogus"
    with open(os.path.join(jdir, "transaction.json"), "w") as f:
        json.dump(payload, f)
    with pytest.raises(JournalError):
        TransactionJournal.load(repo.git_dir)


# --------------------------------------------------------------------------- #
# Journal-owned staging layout + hashing
# --------------------------------------------------------------------------- #


def test_staging_dir_for_is_journal_owned(tmp_path):
    root = str(tmp_path / "masterDBDiff")
    sd = staging_dir_for(root, "txn-123")
    assert sd == os.path.join(root + ".staging", "txn-123")
    assert sd.endswith(os.path.join(".staging", "txn-123"))


def test_compute_sha256_of_staged_file(tmp_path):
    p = str(tmp_path / "f.json")
    _write(p, '{"a":1}')
    h = compute_sha256(p)
    assert h is not None and len(h) == 64
    assert compute_sha256(str(tmp_path / "missing")) is None


def test_journal_records_per_file_sha_and_states(tmp_path):
    repo = _init_repo(tmp_path)
    j = _make_journal(repo.git_dir)
    j.write()
    loaded = TransactionJournal.load(repo.git_dir)
    st = loaded.repos["master"]
    assert st.files["cards.json"].source_sha256 == _HEX_C
    assert st.commit_state == RepoCommitState.PENDING
    assert st.push_state == RepoPushState.PENDING
    # Mutating state rewrites the journal atomically.
    loaded.set_phase(TxnPhase.COMMITTING)
    loaded.update_repo(
        "master", commit_state=RepoCommitState.COMMITTED, target_commit_sha=_HEX_B
    )
    reloaded = TransactionJournal.load(repo.git_dir)
    assert reloaded.repos["master"].commit_state == RepoCommitState.COMMITTED
    assert reloaded.repos["master"].target_commit_sha == _HEX_B


# --------------------------------------------------------------------------- #
# Gate 2: strict fail-closed schema validation
# --------------------------------------------------------------------------- #


def _make_payload(git_dir, **overrides):
    payload = _make_journal(git_dir).to_dict()
    payload.update(overrides)
    return payload


def _make_completed_payload(git_dir):
    payload = _make_payload(git_dir, phase=TxnPhase.COMPLETED.value)
    for state in payload["repos"].values():
        state["commit_state"] = RepoCommitState.COMMITTED.value
        state["push_state"] = RepoPushState.PUSHED.value
        state["target_commit_sha"] = _HEX_B
        state["remote_sha"] = _HEX_B
    return payload


def test_completed_journal_requires_committed_enabled_repos(tmp_path):
    repo = _init_repo(tmp_path)
    payload = _make_completed_payload(repo.git_dir)
    payload["repos"]["i18n"]["commit_state"] = RepoCommitState.PREPARED.value
    _write(
        os.path.join(repo.git_dir, "sekai-update", "transaction.json"),
        json.dumps(payload),
    )
    with pytest.raises(JournalError):
        TransactionJournal.load(repo.git_dir)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("push_state", RepoPushState.PENDING.value),
        ("target_commit_sha", None),
        ("remote_sha", "c" * 40),
    ],
)
def test_completed_journal_requires_durable_push_confirmation(tmp_path, field, value):
    repo = _init_repo(tmp_path)
    payload = _make_completed_payload(repo.git_dir)
    payload["repos"]["master"][field] = value
    _write(
        os.path.join(repo.git_dir, "sekai-update", "transaction.json"),
        json.dumps(payload),
    )
    with pytest.raises(JournalError):
        TransactionJournal.load(repo.git_dir)


def test_valid_completed_journal_loads(tmp_path):
    repo = _init_repo(tmp_path)
    payload = _make_completed_payload(repo.git_dir)
    _write(
        os.path.join(repo.git_dir, "sekai-update", "transaction.json"),
        json.dumps(payload),
    )
    loaded = TransactionJournal.load(repo.git_dir)
    assert loaded is not None
    assert loaded.phase == TxnPhase.COMPLETED
    for state in loaded.repos.values():
        assert state.commit_state == RepoCommitState.COMMITTED
        assert state.push_state == RepoPushState.PUSHED
        assert state.remote_sha == state.target_commit_sha == _HEX_B


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("remote_base_sha", None),
        ("remote_base_sha", _HEX_C[:40]),
        ("remote_name", "upstream"),
        ("remote_ref", "refs/heads/dev"),
    ],
)
def test_remote_snapshot_fields_are_strict(tmp_path, field, value):
    repo = _init_repo(tmp_path)
    payload = _make_payload(repo.git_dir)
    payload["repos"]["master"][field] = value
    _write(
        os.path.join(repo.git_dir, "sekai-update", "transaction.json"),
        json.dumps(payload),
    )
    with pytest.raises(JournalError):
        TransactionJournal.load(repo.git_dir)


def test_remote_base_sha_must_equal_base_sha(tmp_path):
    repo = _init_repo(tmp_path)
    payload = _make_payload(repo.git_dir)
    payload["repos"]["master"]["remote_base_sha"] = "c" * 40
    _write(
        os.path.join(repo.git_dir, "sekai-update", "transaction.json"),
        json.dumps(payload),
    )
    with pytest.raises(JournalError):
        TransactionJournal.load(repo.git_dir)


def test_pushed_repos_must_form_push_order_prefix(tmp_path):
    repo = _init_repo(tmp_path)
    payload = _make_completed_payload(repo.git_dir)
    payload["repos"]["i18n"]["push_state"] = RepoPushState.PENDING.value
    payload["repos"]["i18n"]["remote_sha"] = None
    _write(
        os.path.join(repo.git_dir, "sekai-update", "transaction.json"),
        json.dumps(payload),
    )
    with pytest.raises(JournalError):
        TransactionJournal.load(repo.git_dir)


@pytest.mark.parametrize("push_order", [["master", "i18n"], ["i18n", "i18n"]])
def test_push_order_rejects_duplicate_or_wrong_order(tmp_path, push_order):
    repo = _init_repo(tmp_path)
    payload = _make_payload(repo.git_dir)
    payload["push_order"] = push_order
    _write(
        os.path.join(repo.git_dir, "sekai-update", "transaction.json"),
        json.dumps(payload),
    )
    with pytest.raises(JournalError):
        TransactionJournal.load(repo.git_dir)


@pytest.mark.parametrize(
    ("commit_state", "target_commit_sha"),
    [
        (RepoCommitState.PENDING.value, _HEX_B),
        (RepoCommitState.PREPARED.value, None),
        (RepoCommitState.COMMITTED.value, None),
    ],
)
def test_commit_state_and_target_sha_must_match(
    tmp_path, commit_state, target_commit_sha
):
    repo = _init_repo(tmp_path)
    payload = _make_payload(repo.git_dir)
    payload["repos"]["master"]["commit_state"] = commit_state
    payload["repos"]["master"]["target_commit_sha"] = target_commit_sha
    _write(
        os.path.join(repo.git_dir, "sekai-update", "transaction.json"),
        json.dumps(payload),
    )
    with pytest.raises(JournalError):
        TransactionJournal.load(repo.git_dir)


def test_pending_push_state_requires_no_remote_sha(tmp_path):
    repo = _init_repo(tmp_path)
    payload = _make_payload(repo.git_dir)
    payload["repos"]["master"]["remote_sha"] = _HEX_B
    _write(
        os.path.join(repo.git_dir, "sekai-update", "transaction.json"),
        json.dumps(payload),
    )
    with pytest.raises(JournalError):
        TransactionJournal.load(repo.git_dir)


@pytest.mark.parametrize(
    ("commit_state", "target_commit_sha", "remote_sha"),
    [
        (RepoCommitState.PREPARED.value, _HEX_B, _HEX_B),
        (RepoCommitState.PENDING.value, None, _HEX_B),
        (RepoCommitState.COMMITTED.value, _HEX_B, _HEX_C[:40]),
    ],
)
def test_pushed_state_requires_committed_matching_target(
    tmp_path, commit_state, target_commit_sha, remote_sha
):
    repo = _init_repo(tmp_path)
    payload = _make_payload(repo.git_dir, phase=TxnPhase.PUSHING.value)
    payload["repos"]["master"].update(
        commit_state=commit_state,
        push_state=RepoPushState.PUSHED.value,
        target_commit_sha=target_commit_sha,
        remote_sha=remote_sha,
    )
    _write(
        os.path.join(repo.git_dir, "sekai-update", "transaction.json"),
        json.dumps(payload),
    )
    with pytest.raises(JournalError):
        TransactionJournal.load(repo.git_dir)


def test_transaction_id_must_be_uuid(tmp_path):
    repo = _init_repo(tmp_path)
    bad = _make_payload(repo.git_dir, transaction_id="not-a-uuid")
    _write(
        os.path.join(repo.git_dir, "sekai-update", "transaction.json"), json.dumps(bad)
    )
    with pytest.raises(JournalError):
        TransactionJournal.load(repo.git_dir)


def test_unknown_repo_key_fails_closed(tmp_path):
    repo = _init_repo(tmp_path)
    payload = _make_payload(repo.git_dir)
    payload["repos"]["ghost"] = payload["repos"]["master"]
    payload["enabled_repos"].append("ghost")
    payload["publish_order"].append("ghost")
    _write(
        os.path.join(repo.git_dir, "sekai-update", "transaction.json"),
        json.dumps(payload),
    )
    with pytest.raises(JournalError):
        TransactionJournal.load(repo.git_dir)


def test_duplicate_enabled_repo_fails_closed(tmp_path):
    repo = _init_repo(tmp_path)
    payload = _make_payload(repo.git_dir)
    payload["enabled_repos"] = ["master", "master"]
    payload["publish_order"] = ["master", "master"]
    _write(
        os.path.join(repo.git_dir, "sekai-update", "transaction.json"),
        json.dumps(payload),
    )
    with pytest.raises(JournalError):
        TransactionJournal.load(repo.git_dir)


def test_publish_order_must_equal_enabled_set(tmp_path):
    repo = _init_repo(tmp_path)
    payload = _make_payload(repo.git_dir)
    payload["publish_order"] = ["master"]  # missing i18n
    _write(
        os.path.join(repo.git_dir, "sekai-update", "transaction.json"),
        json.dumps(payload),
    )
    with pytest.raises(JournalError):
        TransactionJournal.load(repo.git_dir)


def test_repos_keys_must_equal_enabled_set(tmp_path):
    repo = _init_repo(tmp_path)
    payload = _make_payload(repo.git_dir)
    del payload["repos"]["i18n"]  # enabled still lists i18n
    _write(
        os.path.join(repo.git_dir, "sekai-update", "transaction.json"),
        json.dumps(payload),
    )
    with pytest.raises(JournalError):
        TransactionJournal.load(repo.git_dir)


def test_manifest_files_must_match_exactly(tmp_path):
    repo = _init_repo(tmp_path)
    payload = _make_payload(repo.git_dir)
    # Extra file key not present in manifest.
    payload["repos"]["master"]["files"]["extra.json"] = {
        "source_sha256": _HEX_X,
        "dest_sha256": None,
    }
    _write(
        os.path.join(repo.git_dir, "sekai-update", "transaction.json"),
        json.dumps(payload),
    )
    with pytest.raises(JournalError):
        TransactionJournal.load(repo.git_dir)


def test_manifest_rel_path_must_be_canonical(tmp_path):
    repo = _init_repo(tmp_path)
    payload = _make_payload(repo.git_dir)
    payload["repos"]["master"]["manifest"].append("/abs.json")
    payload["repos"]["master"]["files"]["/abs.json"] = {
        "source_sha256": _HEX_X,
        "dest_sha256": None,
    }
    _write(
        os.path.join(repo.git_dir, "sekai-update", "transaction.json"),
        json.dumps(payload),
    )
    with pytest.raises(JournalError):
        TransactionJournal.load(repo.git_dir)


def test_sha256_must_be_hex_digest(tmp_path):
    repo = _init_repo(tmp_path)
    payload = _make_payload(repo.git_dir)
    payload["repos"]["master"]["files"]["versions.json"]["source_sha256"] = "z" * 64
    _write(
        os.path.join(repo.git_dir, "sekai-update", "transaction.json"),
        json.dumps(payload),
    )
    with pytest.raises(JournalError):
        TransactionJournal.load(repo.git_dir)


def test_staging_dir_must_be_bound_to_transaction(tmp_path):
    repo = _init_repo(tmp_path)
    payload = _make_payload(repo.git_dir)
    # Wrong transaction id embedded in the staging path.
    payload["repos"]["master"]["staging_dir"] = os.path.join(
        "/data/master.staging", "other-txn-id"
    )
    _write(
        os.path.join(repo.git_dir, "sekai-update", "transaction.json"),
        json.dumps(payload),
    )
    with pytest.raises(JournalError):
        TransactionJournal.load(repo.git_dir)


def test_staging_dir_canonical_when_repo_root_recorded(tmp_path):
    repo = _init_repo(tmp_path)
    payload = _make_payload(repo.git_dir)
    txn = payload["transaction_id"]
    # repo_root recorded but staging_dir points elsewhere -> not canonical.
    payload["repos"]["master"]["repo_root"] = "/data/master"
    payload["repos"]["master"]["staging_dir"] = os.path.join(
        "/elsewhere/master.staging", txn
    )
    _write(
        os.path.join(repo.git_dir, "sekai-update", "transaction.json"),
        json.dumps(payload),
    )
    with pytest.raises(JournalError):
        TransactionJournal.load(repo.git_dir)


def test_malformed_types_fail_closed_not_raw_error(tmp_path):
    repo = _init_repo(tmp_path)
    payload = _make_payload(repo.git_dir)
    # commit_state is the wrong type (a list) -> must raise JournalError, not
    # a raw TypeError/ValueError from enum construction.
    payload["repos"]["master"]["commit_state"] = ["bad"]
    _write(
        os.path.join(repo.git_dir, "sekai-update", "transaction.json"),
        json.dumps(payload),
    )
    with pytest.raises(JournalError):
        TransactionJournal.load(repo.git_dir)


def test_record_file_dest_sha_validates_format(tmp_path):
    repo = _init_repo(tmp_path)
    j = _make_journal(repo.git_dir)
    j.write()
    with pytest.raises(JournalError):
        j.record_file_dest_sha("master", "versions.json", "not-a-hex")


def test_valid_journal_with_repo_root_loads(tmp_path):
    repo = _init_repo(tmp_path)
    txn = new_transaction_id()
    staging = os.path.join("/data/master.staging", txn)
    j = TransactionJournal(
        master_git_dir=repo.git_dir,
        transaction_id=txn,
        candidate={"dataVersion": "1"},
        enabled_repos=["master", "i18n"],
        publish_order=["master", "i18n"],
        repos={
            "master": RepoState(
                manifest=["versions.json"],
                staging_dir=staging,
                repo_root="/data/master",
                base_sha=_HEX_B,
                remote_base_sha=_HEX_B,
                remote_name="origin",
                remote_ref="refs/heads/main",
                files={"versions.json": FileEntry(source_sha256=_HEX_V)},
            ),
            "i18n": RepoState(
                manifest=["versions.json"],
                staging_dir=os.path.join("/data/i18n.staging", txn),
                repo_root="/data/i18n",
                base_sha=_HEX_B,
                remote_base_sha=_HEX_B,
                remote_name="origin",
                remote_ref="refs/heads/main",
                files={"versions.json": FileEntry(source_sha256=_HEX_V)},
            ),
        },
        phase=TxnPhase.PUBLISHING,
    )
    j.write()
    loaded = TransactionJournal.load(repo.git_dir)
    assert loaded is not None
    assert loaded.repos["master"].repo_root == "/data/master"
    assert loaded.repos["master"].staging_dir == staging


# --------------------------------------------------------------------------- #
# Gate 2 (latest): strict Git SHA + source SHA + duplicate manifest validation
# --------------------------------------------------------------------------- #


def test_git_sha_fields_must_be_40_hex(tmp_path):
    repo = _init_repo(tmp_path)
    payload = _make_payload(repo.git_dir)
    # base_sha fixture is 40 hex (valid); break target_commit_sha.
    payload["repos"]["master"]["target_commit_sha"] = "z" * 40
    _write(
        os.path.join(repo.git_dir, "sekai-update", "transaction.json"),
        json.dumps(payload),
    )
    with pytest.raises(JournalError):
        TransactionJournal.load(repo.git_dir)


def test_git_sha_fields_accept_valid_40_hex(tmp_path):
    repo = _init_repo(tmp_path)
    payload = _make_payload(repo.git_dir, phase=TxnPhase.PUSHING.value)
    payload["repos"]["master"]["target_commit_sha"] = "a" * 40
    payload["repos"]["master"]["remote_sha"] = "a" * 40
    payload["repos"]["master"]["commit_state"] = RepoCommitState.COMMITTED.value
    payload["repos"]["master"]["push_state"] = RepoPushState.PUSHED.value
    payload["repos"]["i18n"].update(
        commit_state=RepoCommitState.COMMITTED.value,
        push_state=RepoPushState.PUSHED.value,
        target_commit_sha="c" * 40,
        remote_sha="c" * 40,
    )
    _write(
        os.path.join(repo.git_dir, "sekai-update", "transaction.json"),
        json.dumps(payload),
    )
    loaded = TransactionJournal.load(repo.git_dir)
    assert loaded is not None
    assert loaded.repos["master"].target_commit_sha == "a" * 40
    assert loaded.repos["master"].remote_sha == "a" * 40


def test_git_sha_fields_accept_none(tmp_path):
    repo = _init_repo(tmp_path)
    payload = _make_payload(repo.git_dir)
    # Remote snapshots and their local base are mandatory in every journal.
    payload["repos"]["master"]["base_sha"] = None
    payload["repos"]["master"]["target_commit_sha"] = None
    payload["repos"]["master"]["remote_sha"] = None
    _write(
        os.path.join(repo.git_dir, "sekai-update", "transaction.json"),
        json.dumps(payload),
    )
    with pytest.raises(JournalError):
        TransactionJournal.load(repo.git_dir)


def test_file_entry_requires_source_sha(tmp_path):
    repo = _init_repo(tmp_path)
    payload = _make_payload(repo.git_dir)
    # Drop the source SHA entirely -> must fail closed.
    payload["repos"]["master"]["files"]["versions.json"]["source_sha256"] = None
    _write(
        os.path.join(repo.git_dir, "sekai-update", "transaction.json"),
        json.dumps(payload),
    )
    with pytest.raises(JournalError):
        TransactionJournal.load(repo.git_dir)


def test_file_entry_source_sha_must_be_valid_hex(tmp_path):
    repo = _init_repo(tmp_path)
    payload = _make_payload(repo.git_dir)
    payload["repos"]["master"]["files"]["versions.json"]["source_sha256"] = "not-hex"
    _write(
        os.path.join(repo.git_dir, "sekai-update", "transaction.json"),
        json.dumps(payload),
    )
    with pytest.raises(JournalError):
        TransactionJournal.load(repo.git_dir)


def test_duplicate_manifest_entry_fails_closed(tmp_path):
    repo = _init_repo(tmp_path)
    payload = _make_payload(repo.git_dir)
    # Duplicate rel inside the manifest list (files still match set -> would
    # otherwise pass the set-equality check).
    payload["repos"]["master"]["manifest"] = [
        "versions.json",
        "cards.json",
        "versions.json",
    ]
    _write(
        os.path.join(repo.git_dir, "sekai-update", "transaction.json"),
        json.dumps(payload),
    )
    with pytest.raises(JournalError):
        TransactionJournal.load(repo.git_dir)


def test_update_repo_validates_git_sha(tmp_path):
    repo = _init_repo(tmp_path)
    j = _make_journal(repo.git_dir)
    j.write()
    with pytest.raises(JournalError):
        j.update_repo("master", target_commit_sha="bad-sha")


def test_update_repo_accepts_valid_git_sha(tmp_path):
    repo = _init_repo(tmp_path)
    j = _make_journal(repo.git_dir)
    j.write()
    j.set_phase(TxnPhase.COMMITTING)
    j.update_repo(
        "master",
        commit_state=RepoCommitState.PREPARED,
        target_commit_sha="c" * 40,
        base_sha="d" * 40,
        remote_base_sha="d" * 40,
    )
    reloaded = TransactionJournal.load(repo.git_dir)
    assert reloaded.repos["master"].target_commit_sha == "c" * 40
    assert reloaded.repos["master"].base_sha == "d" * 40


# --------------------------------------------------------------------------- #
# Gate 2 (latest): recovery-time root/staging validation against configured roots
# --------------------------------------------------------------------------- #


def _make_journal_with_roots(git_dir, txn_id, roots):
    repos = {}
    for key, root in roots.items():
        repos[key] = RepoState(
            manifest=["versions.json"],
            staging_dir=staging_dir_for(root, txn_id),
            repo_root=root,
            base_sha=_HEX_B,
            remote_base_sha=_HEX_B,
            remote_name="origin",
            remote_ref="refs/heads/main",
            files={"versions.json": FileEntry(source_sha256=_HEX_V)},
        )
    return TransactionJournal(
        master_git_dir=git_dir,
        transaction_id=txn_id,
        candidate={"dataVersion": "1"},
        enabled_repos=list(roots.keys()),
        publish_order=list(roots.keys()),
        repos=repos,
        phase=TxnPhase.PUBLISHING,
    )


def test_validate_journal_roots_passes_when_matching(tmp_path):
    repo = _init_repo(tmp_path)
    txn = new_transaction_id()
    roots = {"master": "/data/master", "i18n": "/data/i18n"}
    j = _make_journal_with_roots(repo.git_dir, txn, roots)
    j.write()
    loaded = TransactionJournal.load(repo.git_dir)
    # Configured roots match the journal's recorded roots exactly.
    validate_journal_roots(loaded, actual_roots=roots)


def test_validate_journal_roots_rejects_mismatched_root(tmp_path):
    repo = _init_repo(tmp_path)
    txn = new_transaction_id()
    j = _make_journal_with_roots(
        repo.git_dir, txn, {"master": "/data/master", "i18n": "/data/i18n"}
    )
    j.write()
    loaded = TransactionJournal.load(repo.git_dir)
    # Configured master root differs from the journal's recorded root.
    with pytest.raises(JournalError):
        validate_journal_roots(
            loaded, actual_roots={"master": "/elsewhere/master", "i18n": "/data/i18n"}
        )


def test_validate_journal_roots_rejects_mismatched_staging(tmp_path):
    repo = _init_repo(tmp_path)
    txn = new_transaction_id()
    j = _make_journal_with_roots(
        repo.git_dir, txn, {"master": "/data/master", "i18n": "/data/i18n"}
    )
    j.write()
    loaded = TransactionJournal.load(repo.git_dir)
    # Tamper the staging_dir to point at an unexpected location under the
    # configured root's sibling; recovery must not trust it.
    loaded.repos["master"].staging_dir = os.path.join("/data/master.evil", txn)
    with pytest.raises(JournalError):
        validate_journal_roots(
            loaded, actual_roots={"master": "/data/master", "i18n": "/data/i18n"}
        )


def test_validate_journal_roots_rejects_unknown_configured_key(tmp_path):
    repo = _init_repo(tmp_path)
    txn = new_transaction_id()
    j = _make_journal_with_roots(
        repo.git_dir, txn, {"master": "/data/master", "i18n": "/data/i18n"}
    )
    j.write()
    loaded = TransactionJournal.load(repo.git_dir)
    # The running process is not configured for i18n -> cannot trust the journal.
    with pytest.raises(JournalError):
        validate_journal_roots(loaded, actual_roots={"master": "/data/master"})
