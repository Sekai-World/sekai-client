"""Phase 2 durable dual-repository transaction journal.

This module implements the Oracle-design *minimal* durable transaction protocol
for the two-repository (master / i18n) update publish. It is intentionally small
and self-contained so it can be unit-tested against temporary repositories and
bare remotes with no network.

Design invariants (fail-closed):

* Exactly ONE journal lives at
  ``<master_repo.git_dir>/sekai-update/transaction.json``. It is never written
  into a working tree or a manifest file.
* The journal schema is strict: canonical (absolute, normalized) paths; a UUID
  transaction id; the candidate; the enabled repos; the publish order; and, per
  repo, the manifest, a SHA-256 per staged file, the base/remote SHA, the target
  commit SHA, and the commit/push state. Malformed JSON, a duplicate/second
  journal, or invalid (non-canonical / escaping) paths are rejected and the
  cycle fails closed.
* All journal writes are atomic (temp file + ``os.replace``) with ``0600``
  permissions, a file ``fsync``, and a parent-directory ``fsync``. Deletion
  ``fsync``s the parent directory.
* All access occurs while the caller already holds every required repository
  flock (this module never acquires locks itself).
* Staging is *journal-owned*: ``<repo>.staging/<txn_id>/``. Files are generated
  and validated there, ``fsync``-ed, and only then is the ``publishing`` journal
  written (atomically) *before* the first formal ``os.replace``.
* On a clean publication failure the caller clears the staging parent and drops
  the journal (an aborted attempt). A *crash* (no cleanup) leaves both on disk;
  recovery then completes the work using ordered source/destination hashes and
  never resets/reclones.
* Recovery is deterministic and idempotent: matching destination SHA means the
  replace already completed; otherwise a matching source SHA can replace; a
  missing/mismatched source blocks (fail closed) and preserves state.

The module exposes a small, explicit API used by ``check_update``:

* :class:`TransactionJournal` — load / create / update / delete / recover.
* :class:`JournalError` — raised on any fail-closed condition.
* :class:`TxnPhase` / :class:`RepoCommitState` / :class:`RepoPushState` — states.
* :func:`staging_dir_for` — journal-owned staging path for a repo + txn id.
* :func:`compute_sha256` — content hash helper.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import ujson as _json

# Schema version is intentionally incompatible.  A journal written by an older
# updater must never be interpreted as a v2 transaction.
SCHEMA_VERSION = 2

# Directory (relative to the master repo's .git dir) holding the single journal.
JOURNAL_REL_DIR = "sekai-update"
JOURNAL_FILENAME = "transaction.json"

# Staging suffix applied to a repository working directory.
STAGING_SUFFIX = ".staging"

# The only repository keys a journal may reference.
KNOWN_REPOS = ("master", "i18n")

# Strict format matchers (fail closed on any mismatch).
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# Git object/commit SHAs are exactly 40 lowercase hex characters.
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

_ROOT_KEYS = {
    "schema_version", "transaction_id", "candidate", "enabled_repos",
    "publish_order", "push_order", "phase", "repos",
}
_REPO_KEYS = {
    "manifest", "staging_dir", "repo_root", "target_commit_sha",
    "base_sha", "remote_sha", "remote_base_sha", "remote_name", "remote_ref",
    "remote_endpoint_fingerprint",
    "files", "commit_state", "push_state",
}
_FILE_KEYS = {"source_sha256", "dest_sha256"}


class JournalError(RuntimeError):
    """Raised on any fail-closed journal condition.

    The cycle must NOT generate, reset, or force-push when this is raised; it
    should surface a stable error status and leave all local state intact.
    """


class TxnPhase(Enum):
    """Top-level protocol phase recorded in the journal."""

    PREPARING = "preparing"
    PUBLISHING = "publishing"
    COMMITTING = "committing"
    PUSHING = "pushing"
    COMPLETED = "completed"


class RepoCommitState(Enum):
    PENDING = "pending"
    PREPARED = "prepared"
    COMMITTED = "committed"
    FAILED = "failed"


class RepoPushState(Enum):
    PENDING = "pending"
    PUSHED = "pushed"
    FAILED = "failed"


@dataclass
class FileEntry:
    """Per-staged-file record: source (staging) and dest (working tree) SHA-256."""

    source_sha256: str | None = None
    dest_sha256: str | None = None


@dataclass
class RepoState:
    """Per-repository state recorded in the journal."""

    manifest: list[str] = field(default_factory=list)
    staging_dir: str = ""
    repo_root: str = ""
    target_commit_sha: str | None = None
    base_sha: str | None = None
    remote_sha: str | None = None
    remote_base_sha: str | None = None
    remote_name: str = "origin"
    remote_ref: str = "refs/heads/main"
    remote_endpoint_fingerprint: str = "0" * 64
    files: dict[str, FileEntry] = field(default_factory=dict)
    commit_state: RepoCommitState = RepoCommitState.PENDING
    push_state: RepoPushState = RepoPushState.PENDING

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest": list(self.manifest),
            "staging_dir": self.staging_dir,
            "repo_root": self.repo_root,
            "target_commit_sha": self.target_commit_sha,
            "base_sha": self.base_sha,
            "remote_sha": self.remote_sha,
            "remote_base_sha": self.remote_base_sha,
            "remote_name": self.remote_name,
            "remote_ref": self.remote_ref,
            "remote_endpoint_fingerprint": self.remote_endpoint_fingerprint,
            "files": {
                rel: {
                    "source_sha256": fe.source_sha256,
                    "dest_sha256": fe.dest_sha256,
                }
                for rel, fe in self.files.items()
            },
            "commit_state": self.commit_state.value,
            "push_state": self.push_state.value,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RepoState:
        files: dict[str, FileEntry] = {}
        for rel, fe in (data.get("files") or {}).items():
            files[rel] = FileEntry(
                source_sha256=fe.get("source_sha256"),
                dest_sha256=fe.get("dest_sha256"),
            )
        try:
            commit_state = RepoCommitState(data.get("commit_state", "pending"))
            push_state = RepoPushState(data.get("push_state", "pending"))
        except ValueError as err:
            raise JournalError(f"invalid repo state enum: {err}") from err
        return cls(
            manifest=list(data.get("manifest") or []),
            staging_dir=data.get("staging_dir") or "",
            repo_root=data.get("repo_root") or "",
            target_commit_sha=data.get("target_commit_sha"),
            base_sha=data.get("base_sha"),
            remote_sha=data.get("remote_sha"),
            remote_base_sha=data.get("remote_base_sha"),
            remote_name=data.get("remote_name", "origin"),
            remote_ref=data.get("remote_ref", "refs/heads/main"),
            remote_endpoint_fingerprint=data.get(
                "remote_endpoint_fingerprint", "0" * 64
            ),
            files=files,
            commit_state=commit_state,
            push_state=push_state,
        )


def compute_sha256(file_path: str) -> str | None:
    """Return the SHA-256 hex digest of ``file_path`` or ``None`` if absent."""
    if not os.path.isfile(file_path):
        return None
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def staging_dir_for(repo_working_dir: str, txn_id: str) -> str:
    """Journal-owned staging path ``<repo>.staging/<txn_id>/``."""
    return os.path.join(repo_working_dir + STAGING_SUFFIX, txn_id)


def _fsync_file(fd: int) -> None:
    os.fsync(fd)


def _fsync_dir(directory: str) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_published_file(file_path: str, source_path: str | None = None) -> None:
    """Durably flush a published file and the directories involved in a replace."""
    with open(file_path, "rb") as stream:
        _fsync_file(stream.fileno())
    parent = os.path.dirname(file_path)
    if parent:
        _fsync_dir(parent)
    if source_path:
        source_parent = os.path.dirname(source_path)
        if source_parent and os.path.isdir(source_parent):
            _fsync_dir(source_parent)


def _atomic_write_json(target_path: str, payload: dict[str, Any]) -> None:
    """Atomically write ``payload`` as JSON to ``target_path`` with ``0600``.

    Writes to a sibling temp file, ``fsync``s the file, ``os.replace``-es it over
    the target, then ``fsync``s the parent directory. Fails closed on any error
    (the target is left untouched on a write failure).
    """
    parent = os.path.dirname(target_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_path = f"{target_path}.tmp.{uuid.uuid4().hex}"
    data = _json.dumps(payload, ensure_ascii=False, indent=2)
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data.encode("utf-8"))
        _fsync_file(fd)
    finally:
        os.close(fd)
    # Same-directory rename is atomic on POSIX and avoids ``os.replace`` so that
    # callers monitoring ``os.replace`` (e.g. publication-order tests) are not
    # perturbed by journal bookkeeping.
    os.rename(tmp_path, target_path)
    if parent:
        _fsync_dir(parent)


def _canonical(path: str) -> str:
    return os.path.realpath(path)


# -- strict schema validation (fail closed) ---------------------------------- #


def _validate_txn_id(tid: Any) -> None:
    if not isinstance(tid, str) or not tid:
        raise JournalError("transaction_id missing/invalid")
    if _UUID_RE.match(tid) is None:
        raise JournalError(f"transaction_id is not a UUID: {tid!r}")


def _validate_known_repos(keys: Any) -> None:
    if not isinstance(keys, (list, tuple, set)):
        raise JournalError("repo keys must be a collection")
    for key in keys:
        if key not in KNOWN_REPOS:
            raise JournalError(f"unknown repo key: {key!r}")


def _validate_no_duplicates(items: list[Any], label: str) -> None:
    if len(items) != len(set(items)):
        raise JournalError(f"{label} must not contain duplicates: {items!r}")


def _validate_rel_path(key: str, rel: Any) -> None:
    """A manifest/file path must be a canonical relative path: non-empty, not
    absolute, no parent (``..``) traversal, no trailing separator, and normalized
    to the OS separator set."""
    if not isinstance(rel, str) or not rel:
        raise JournalError(f"repo {key!r} has invalid file rel {rel!r}")
    if os.path.isabs(rel):
        raise JournalError(f"repo {key!r} file rel is absolute: {rel!r}")
    if rel.startswith(os.sep) or rel.startswith("/"):
        raise JournalError(f"repo {key!r} file rel is absolute: {rel!r}")
    parts = rel.split(os.sep)
    if any(p in ("", ".", "..") for p in parts):
        raise JournalError(f"repo {key!r} file rel is not canonical: {rel!r}")
    if rel != os.path.join(*parts):
        raise JournalError(f"repo {key!r} file rel is not canonical: {rel!r}")


def _validate_sha256(key: str, rel: str, kind: str, value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, str) or _SHA256_RE.match(value) is None:
        raise JournalError(
            f"repo {key!r} file {rel!r} {kind} is not a SHA-256 hex digest: "
            f"{value!r}"
        )


def _validate_git_sha(key: str, kind: str, value: Any) -> None:
    """Validate a Git SHA field (``base_sha`` / ``target_commit_sha`` /
    ``remote_sha``). ``None`` is allowed (the field is optional at certain
    phases); when present it MUST be a 40-char lowercase hex digest."""
    if value is None:
        return
    if not isinstance(value, str) or _GIT_SHA_RE.match(value) is None:
        raise JournalError(
            f"repo {key!r} {kind} is not a 40-hex Git SHA: {value!r}"
        )


def _validate_staging_dir(
    key: str, staging: Any, transaction_id: str, repo_root: str
) -> None:
    if not isinstance(staging, str) or not staging or not os.path.isabs(staging):
        raise JournalError(f"repo {key!r} missing staging_dir")
    if os.path.realpath(staging) != staging:
        raise JournalError(f"repo {key!r} staging_dir is not canonical: {staging!r}")
    # The staging dir must be journal-owned and bound to THIS transaction: it must
    # end in ``<repo>.staging/<transaction_id>``. When the repo root is recorded,
    # the staging dir must equal ``<repo_root>.staging/<transaction_id>`` exactly
    # (canonical), which proves the staging is owned by the right repo + txn.
    suffix = os.path.join(STAGING_SUFFIX, transaction_id)
    if not staging.endswith(suffix):
        raise JournalError(
            f"repo {key!r} staging_dir not bound to txn {transaction_id!r}: "
            f"{staging!r}"
        )
    if not isinstance(repo_root, str) or not repo_root or not os.path.isabs(repo_root):
        raise JournalError(f"repo {key!r} missing canonical repo_root")
    if os.path.realpath(repo_root) != repo_root:
        raise JournalError(f"repo {key!r} repo_root is not canonical: {repo_root!r}")
    expected = os.path.join(repo_root + STAGING_SUFFIX, transaction_id)
    if staging != expected:
        raise JournalError(
            f"repo {key!r} staging_dir not canonical for root "
            f"{repo_root!r}: {staging!r}"
        )


def _validate_file_entry(key: str, rel: Any, fe: Any) -> None:
    _validate_rel_path(key, rel)
    if not isinstance(fe, dict):
        raise JournalError(f"repo {key!r} file {rel!r} invalid")
    if set(fe) != _FILE_KEYS:
        raise JournalError(f"repo {key!r} file {rel!r} has unexpected keys")
    # Every file entry MUST carry a non-empty, valid source SHA-256. A missing or
    # malformed source SHA means recovery cannot prove the staged content, so the
    # journal is rejected fail-closed.
    src = fe.get("source_sha256")
    if not isinstance(src, str) or not src or _SHA256_RE.match(src) is None:
        raise JournalError(
            f"repo {key!r} file {rel!r} source_sha256 is required and must be a "
            f"SHA-256 hex digest: {src!r}"
        )
    _validate_sha256(key, rel, "dest_sha256", fe.get("dest_sha256"))


def _validate_repo_state(key: str, rd: Any, transaction_id: str) -> None:
    if not isinstance(rd, dict):
        raise JournalError(f"repo state {key!r} must be an object")
    if set(rd) != _REPO_KEYS:
        raise JournalError(f"repo {key!r} has unexpected keys")
    _validate_repo_location(key, rd, transaction_id)
    _validate_repo_files(key, rd)
    _validate_repo_git_state(key, rd)


def _validate_repo_location(key: str, rd: dict[str, Any], transaction_id: str) -> None:
    _validate_staging_dir(
        key, rd.get("staging_dir") or "", transaction_id, rd.get("repo_root") or ""
    )


def _validate_repo_files(key: str, rd: dict[str, Any]) -> None:
    manifest = rd.get("manifest")
    if not isinstance(manifest, list):
        raise JournalError(f"repo {key!r} manifest must be a list")
    files = rd.get("files")
    if not isinstance(files, dict):
        raise JournalError(f"repo {key!r} files must be an object")
    # Reject duplicate manifest entries (a duplicate rel would silently collapse
    # in a set and mask a real defect).
    _validate_no_duplicates(manifest, f"repo {key!r} manifest")
    # Exact manifest/files match: every manifest entry is a file key and the file
    # keys are exactly the manifest entries (no extra, no missing).
    for rel in manifest:
        _validate_rel_path(key, rel)
        if rel not in files:
            raise JournalError(
                f"repo {key!r} manifest entry {rel!r} missing from files"
            )
    if set(files.keys()) != set(manifest):
        raise JournalError(
            f"repo {key!r} files keys must exactly match manifest"
        )
    for rel, fe in files.items():
        _validate_file_entry(key, rel, fe)


def _validate_repo_git_state(key: str, rd: dict[str, Any]) -> None:
    # Git SHA fields must be valid 40-hex where the schema requires them.
    _validate_git_sha(key, "base_sha", rd.get("base_sha"))
    _validate_git_sha(key, "target_commit_sha", rd.get("target_commit_sha"))
    _validate_git_sha(key, "remote_sha", rd.get("remote_sha"))
    _validate_git_sha(key, "remote_base_sha", rd.get("remote_base_sha"))
    # An unborn repository has no local or remote commit.  In that one case both
    # fields are absent; otherwise the remote snapshot must exactly bind the
    # recorded local base.
    base = rd.get("base_sha")
    remote_base = rd.get("remote_base_sha")
    if (base is None) != (remote_base is None):
        raise JournalError(
            f"repo {key!r} base_sha and remote_base_sha must both be set or absent"
        )
    if base is not None and remote_base != base:
        raise JournalError(f"repo {key!r} remote base does not match base SHA")
    if rd.get("remote_name") != "origin":
        raise JournalError(f"repo {key!r} remote_name is invalid")
    if rd.get("remote_ref") != "refs/heads/main":
        raise JournalError(f"repo {key!r} remote_ref is invalid")
    fingerprint = rd.get("remote_endpoint_fingerprint")
    if not isinstance(fingerprint, str) or _SHA256_RE.match(fingerprint) is None:
        raise JournalError(f"repo {key!r} remote endpoint fingerprint is invalid")
    cs = rd.get("commit_state")
    if cs not in {s.value for s in RepoCommitState}:
        raise JournalError(f"repo {key!r} invalid commit_state {cs!r}")
    ps = rd.get("push_state")
    if ps not in {s.value for s in RepoPushState}:
        raise JournalError(f"repo {key!r} invalid push_state {ps!r}")


def _validate_root(data: Any) -> None:
    if not isinstance(data, dict):
        raise JournalError("journal root must be an object")
    if set(data) != _ROOT_KEYS:
        raise JournalError("journal has missing or unexpected root keys")
    _validate_root_identity(data)
    enabled, order = _validate_root_orders(data)
    _validate_root_repos(data, enabled)


def _validate_root_identity(data: dict[str, Any]) -> None:
    if data.get("schema_version") != SCHEMA_VERSION:
        raise JournalError(
            f"unsupported schema_version: {data.get('schema_version')!r}"
        )
    _validate_txn_id(data.get("transaction_id"))
    if not isinstance(data.get("candidate"), dict):
        raise JournalError("candidate must be an object")


def _validate_root_orders(data: dict[str, Any]) -> tuple[list[Any], list[Any]]:
    enabled = data.get("enabled_repos")
    order = data.get("publish_order")
    if not isinstance(enabled, list) or not enabled:
        raise JournalError("enabled_repos must be a non-empty list")
    if not isinstance(order, list) or not order:
        raise JournalError("publish_order must be a non-empty list")
    _validate_order_fields(enabled, order, data.get("push_order"))
    return enabled, order


def _validate_root_repos(data: dict[str, Any], enabled: list[Any]) -> None:
    if data.get("phase") not in {p.value for p in TxnPhase}:
        raise JournalError(f"invalid phase: {data.get('phase')!r}")
    repos = data.get("repos")
    if not isinstance(repos, dict):
        raise JournalError("repos must be an object")
    _validate_known_repos(list(repos.keys()))
    if set(repos.keys()) != set(enabled):
        raise JournalError("repos keys must equal enabled_repos set")

    phase = data["phase"]
    for key, rd in repos.items():
        _validate_repo_phase_state(key, rd, phase)
    _validate_push_prefix(enabled, data["push_order"], repos)


def _validate_push_prefix(
    enabled: list[Any], push_order: list[Any], repos: dict[str, Any]
) -> None:
    pushed = {
        key for key in enabled
        if repos[key]["push_state"] == RepoPushState.PUSHED.value
    }
    prefix = push_order[: len(pushed)]
    if set(prefix) != pushed:
        raise JournalError("pushed repos must form a prefix of push_order")


def _validate_repo_phase_state(key: str, rd: dict[str, Any], phase: str) -> None:
    """Reject impossible durable protocol combinations.

    FAILED is deliberately not a durable state.  An operational failure leaves
    the last proven state on disk and recovery retries from there.
    """
    commit_state = rd["commit_state"]
    push_state = rd["push_state"]
    if (
        commit_state == RepoCommitState.FAILED.value
        or push_state == RepoPushState.FAILED.value
    ):
        raise JournalError(f"repo {key!r} has non-durable FAILED state")
    target = rd["target_commit_sha"]
    if commit_state == RepoCommitState.PENDING.value and target is not None:
        raise JournalError(f"repo {key!r} pending state has a target SHA")
    if commit_state in {
        RepoCommitState.PREPARED.value,
        RepoCommitState.COMMITTED.value,
    } and target is None:
        raise JournalError(f"repo {key!r} committed state has no target SHA")
    if (
        push_state == RepoPushState.PENDING.value
        and rd["remote_sha"] is not None
    ):
        raise JournalError(f"repo {key!r} pending push state has a remote SHA")
    if (
        push_state == RepoPushState.PUSHED.value
        and commit_state != RepoCommitState.COMMITTED.value
    ):
        raise JournalError(f"repo {key!r} pushed before commit")
    if push_state == RepoPushState.PUSHED.value:
        _validate_durable_push_confirmation(key, rd)
    _validate_phase_relationships(key, commit_state, push_state, phase)


def _validate_phase_relationships(
    key: str, commit_state: str, push_state: str, phase: str
) -> None:
    required = {
        TxnPhase.PREPARING.value: (RepoCommitState.PENDING.value, None),
        TxnPhase.PUBLISHING.value: (
            RepoCommitState.PENDING.value,
            RepoPushState.PENDING.value,
        ),
        TxnPhase.COMMITTING.value: (None, RepoPushState.PENDING.value),
        TxnPhase.PUSHING.value: (RepoCommitState.COMMITTED.value, None),
        TxnPhase.COMPLETED.value: (
            RepoCommitState.COMMITTED.value,
            RepoPushState.PUSHED.value,
        ),
    }
    required_commit, required_push = required[phase]
    if required_commit is not None and commit_state != required_commit:
        raise JournalError(f"repo {key!r} has invalid commit state for {phase}")
    if required_push is not None and push_state != required_push:
        raise JournalError(f"repo {key!r} has invalid push state for {phase}")


def _validate_durable_push_confirmation(key: str, rd: dict[str, Any]) -> None:
    target = rd["target_commit_sha"]
    if target is None or rd["remote_sha"] != target:
        raise JournalError(
            f"repo {key!r} pushed state lacks durable confirmation of target SHA"
        )


def _validate_order_fields(
    enabled: Any, publish_order: Any, push_order: Any
) -> None:
    _validate_known_repos(enabled)
    _validate_known_repos(publish_order)
    _validate_no_duplicates(enabled, "enabled_repos")
    _validate_no_duplicates(publish_order, "publish_order")
    if set(publish_order) != set(enabled):
        raise JournalError("publish_order must equal enabled_repos set")
    if not isinstance(push_order, list) or not push_order:
        raise JournalError("push_order must be a non-empty list")
    _validate_known_repos(push_order)
    _validate_no_duplicates(push_order, "push_order")
    if set(push_order) != set(enabled):
        raise JournalError("push_order must equal enabled_repos set")
    expected = [key for key in ("i18n", "master") if key in enabled]
    if push_order != expected:
        raise JournalError("push_order must be i18n -> master")


class TransactionJournal:
    """The single durable transaction journal for a master+i18n publish.

    Construction does NOT touch disk. Use :meth:`create` to write a new journal
    or :meth:`load` to read + strictly validate an existing one.
    """

    def __init__(
        self,
        master_git_dir: str,
        transaction_id: str,
        candidate: dict[str, Any],
        enabled_repos: list[str],
        publish_order: list[str],
        repos: dict[str, RepoState],
        phase: TxnPhase = TxnPhase.PREPARING,
        push_order: list[str] | None = None,
    ) -> None:
        self.master_git_dir = os.path.realpath(master_git_dir)
        self.transaction_id = transaction_id
        self.candidate = candidate
        self.enabled_repos = list(enabled_repos)
        self.publish_order = list(publish_order)
        self.push_order = list(
            push_order
            if push_order is not None
            else [key for key in ("i18n", "master") if key in enabled_repos]
        )
        # Older callers constructed RepoState without repo_root.  Deriving it
        # here keeps the standalone constructor useful while serialized v2
        # journals remain strict and always contain canonical roots.
        for _key, state in repos.items():
            if not state.repo_root and state.staging_dir.endswith(
                os.path.join(STAGING_SUFFIX, transaction_id)
            ):
                suffix_len = len(os.path.join(STAGING_SUFFIX, transaction_id))
                state.repo_root = state.staging_dir[:-suffix_len]
            if state.repo_root:
                state.repo_root = os.path.realpath(state.repo_root)
                state.staging_dir = os.path.realpath(state.staging_dir)
        self.repos = repos
        self.phase = phase

    # -- location ---------------------------------------------------------- #
    @property
    def journal_dir(self) -> str:
        return os.path.join(self.master_git_dir, JOURNAL_REL_DIR)

    @property
    def journal_path(self) -> str:
        return os.path.join(self.journal_dir, JOURNAL_FILENAME)

    # -- serialization ----------------------------------------------------- #
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "transaction_id": self.transaction_id,
            "candidate": self.candidate,
            "enabled_repos": self.enabled_repos,
            "publish_order": self.publish_order,
            "push_order": self.push_order,
            "phase": self.phase.value,
            "repos": {key: st.to_dict() for key, st in self.repos.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], master_git_dir: str) -> TransactionJournal:
        try:
            cls._validate_schema(data, master_git_dir)
            repos = {
                key: RepoState.from_dict(rd)
                for key, rd in data["repos"].items()
            }
            return cls(
                master_git_dir=master_git_dir,
                transaction_id=data["transaction_id"],
                candidate=data["candidate"],
                enabled_repos=list(data["enabled_repos"]),
                publish_order=list(data["publish_order"]),
                push_order=list(data["push_order"]),
                repos=repos,
                phase=TxnPhase(data["phase"]),
            )
        except JournalError:
            raise
        except (TypeError, ValueError, KeyError, AttributeError) as err:
            # Malformed types must never leak as a raw TypeError/ValueError; fail
            # closed with a JournalError instead.
            raise JournalError(f"journal content malformed: {err}") from err

    @staticmethod
    def _validate_schema(data: Any, master_git_dir: str) -> None:
        """Strict schema validation; raise :class:`JournalError` on any defect."""
        _validate_root(data)
        enabled = data["enabled_repos"]
        repos = data["repos"]
        for key in enabled:
            if key not in repos:
                raise JournalError(f"missing repo state for enabled repo {key!r}")
        transaction_id = data["transaction_id"]
        for key, rd in repos.items():
            _validate_repo_state(key, rd, transaction_id)

    # -- disk operations --------------------------------------------------- #
    @classmethod
    def load(cls, master_git_dir: str) -> TransactionJournal | None:
        """Load + strictly validate the journal, or return ``None`` if absent.

        Raises :class:`JournalError` on malformed/duplicate/invalid content.
        """
        master_git_dir = os.path.realpath(master_git_dir)
        path = os.path.join(master_git_dir, JOURNAL_REL_DIR, JOURNAL_FILENAME)
        if os.path.lexists(path) and os.path.realpath(path) != path:
            raise JournalError("journal path is a symlink")
        if not os.path.isfile(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                data = _json.load(f)
        except (_json.JSONDecodeError, ValueError, OSError) as err:
            raise JournalError(f"journal is not valid JSON: {err}") from err
        # Reject a duplicate/second journal file (only one name is allowed).
        try:
            names = os.listdir(os.path.dirname(path))
        except OSError:
            names = []
        for name in names:
            if name != JOURNAL_FILENAME and name.endswith(".json"):
                raise JournalError(f"duplicate journal artifact present: {name!r}")
        return cls.from_dict(data, master_git_dir)

    def write(self) -> None:
        """Atomically write the journal (0600 + fsync file + fsync dir)."""
        self._validate_schema(self.to_dict(), self.master_git_dir)
        _atomic_write_json(self.journal_path, self.to_dict())

    def delete(self) -> None:
        """Delete the journal file and ``fsync`` its parent directory."""
        if os.path.isfile(self.journal_path):
            os.remove(self.journal_path)
        _fsync_dir(self.journal_dir)

    # -- mutation helpers (each rewrites the journal atomically) ------------ #
    def set_phase(self, phase: TxnPhase) -> None:
        self.phase = phase
        self.write()

    def update_repo(
        self,
        key: str,
        *,
        commit_state: RepoCommitState | None = None,
        push_state: RepoPushState | None = None,
        target_commit_sha: str | None = None,
        remote_sha: str | None = None,
        remote_base_sha: str | None = None,
        base_sha: str | None = None,
        dest_sha256: str | None = None,
        rel: str | None = None,
    ) -> None:
        st = self.repos[key]
        if commit_state is not None:
            st.commit_state = commit_state
        if push_state is not None:
            st.push_state = push_state
        if target_commit_sha is not None:
            _validate_git_sha(key, "target_commit_sha", target_commit_sha)
            st.target_commit_sha = target_commit_sha
        if remote_sha is not None:
            _validate_git_sha(key, "remote_sha", remote_sha)
            st.remote_sha = remote_sha
        if remote_base_sha is not None:
            _validate_git_sha(key, "remote_base_sha", remote_base_sha)
            st.remote_base_sha = remote_base_sha
        if base_sha is not None:
            _validate_git_sha(key, "base_sha", base_sha)
            st.base_sha = base_sha
        if dest_sha256 is not None and rel is not None:
            if rel not in st.files:
                raise JournalError(
                    f"repo {key!r} file {rel!r} is not in the immutable manifest"
                )
            _validate_sha256(key, rel, "dest_sha256", dest_sha256)
            st.files[rel].dest_sha256 = dest_sha256
        self.write()

    def record_file_dest_sha(self, key: str, rel: str, sha: str | None) -> None:
        st = self.repos[key]
        if rel not in st.files:
            st.files[rel] = FileEntry()
        _validate_sha256(key, rel, "dest_sha256", sha)
        st.files[rel].dest_sha256 = sha
        self.write()


def validate_journal_roots(
    journal: TransactionJournal,
    actual_roots: dict[str, str],
) -> None:
    """Recovery-time safety check: validate the journal's recorded ``repo_root``
    and ``staging_dir`` for every repo against the *configured* actual roots.

    The journal is self-supplied (it lives on disk and may have been written by a
    prior, possibly tampered, process). Recovery must NOT trust the journal's own
    ``repo_root`` alone; instead it must be cross-checked against the roots the
    running process was actually configured with (``actual_roots``), so a journal
    that points at an unexpected/escaping location fails closed rather than
    operating on the wrong tree.

    Call interface (intended for ``check_update._recover_transaction``)::

        validate_journal_roots(
            journal,
            actual_roots={
                "master": masterdb_diff_folder_path,
                "i18n": i18n_diff_folder_path,
            },
        )

    ``actual_roots`` maps each enabled repo key to the canonical, configured
    working-tree root for that repo (as resolved by the running process, e.g.
    ``masterdb_diff_folder_path`` / ``i18n_diff_folder_path``). For every repo the
    journal references, this raises :class:`JournalError` when:

    * the repo key is not present in ``actual_roots`` (unknown configured root);
    * the journal's ``repo_root`` (when recorded) does not canonicalize to the
      configured root;
    * the journal's ``staging_dir`` does not canonicalize to
      ``<configured_root>.staging/<transaction_id>`` (i.e. it is not the
      journal-owned staging bound to this transaction under the configured root).

    This is a pure validation helper: it performs no disk mutation and leaves the
    journal untouched. It is safe to call immediately after
    :meth:`TransactionJournal.load` and before any recovery dispatch.
    """
    txn_id = journal.transaction_id
    for key, st in journal.repos.items():
        actual = actual_roots.get(key)
        if actual is None:
            raise JournalError(
                f"recovery: configured root missing for repo {key!r}; "
                f"cannot trust journal root {st.repo_root!r}"
            )
        actual_root = os.path.realpath(actual)
        if st.repo_root:
            if os.path.realpath(st.repo_root) != actual_root:
                raise JournalError(
                    f"recovery: repo {key!r} journal root "
                    f"{st.repo_root!r} does not match configured root "
                    f"{actual!r}"
                )
        # The staging dir must be journal-owned under the CONFIGURED root, not the
        # journal's self-supplied root. This prevents a journal from redirecting
        # recovery at an arbitrary staging location.
        expected_staging = os.path.join(actual_root + STAGING_SUFFIX, txn_id)
        if not st.staging_dir:
            raise JournalError(f"recovery: repo {key!r} missing staging_dir")
        if os.path.realpath(st.staging_dir) != expected_staging:
            raise JournalError(
                f"recovery: repo {key!r} staging_dir {st.staging_dir!r} does "
                f"not match configured root staging {expected_staging!r}"
            )


def fsync_directory(directory: str) -> None:
    """Public narrow wrapper used by publication code for durable directory edges."""
    _fsync_dir(directory)


def fsync_file(file_path: str) -> None:
    """Flush a regular file and its containing directory."""
    with open(file_path, "rb") as stream:
        _fsync_file(stream.fileno())
    parent = os.path.dirname(file_path)
    if parent:
        _fsync_dir(parent)


def new_transaction_id() -> str:
    return str(uuid.uuid4())
