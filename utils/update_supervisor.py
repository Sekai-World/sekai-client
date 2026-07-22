"""Fail-closed identity metadata implementation

This module contains NO PROCESS CREATION/SIGNALING - owned by supervisor-
fenced execution logic. Must be paired with process-level implementation
to form complete supervisor system.
"""

from __future__ import annotations

import json
import logging
import math
import os
import stat
import sys
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from utils.git_lock import repo_file_locks

logger = logging.getLogger(__name__)

HARD_TIMEOUT_DEFAULT = 3600.0
TERM_GRACE_DEFAULT = 10.0
SCHEMA_VERSION = 1
RUN_ID_ENV = "SEKAI_UPDATE_RUN_ID"
_MAX_OWNER_SIZE = 64 * 1024  # 64 KiB

__NR_prctl = 157  # x86_64 syscall number for prctl; used only as fallback

_OWNER_FIELDS = {
    "schema_version",
    "run_id",
    "pid",
    "pgid",
    "proc_starttime",
    "task_kind",
    "parent_pid",
    "started_at",
    "deadline",
    "lock_paths",
}


class OwnerCleanupError(RuntimeError):
    """Raised when owner metadata cleanup fails while holding flock."""

    def __init__(self, message: str, failures: list[tuple[str, BaseException]]) -> None:
        super().__init__(message)
        self.failures = failures


class TaskKind(StrEnum):
    ORDINARY = "ordinary"
    DAILY = "daily"


class WatchdogUnsupported(RuntimeError):
    """Raised when the watchdog primitive cannot be safely provided."""


class WatchdogFailed(RuntimeError):
    """Raised when the watchdog execution fails to start or supervise."""


class TargetExecutionError(RuntimeError):
    """Raised when the target process fails to execute."""


@dataclass(frozen=True)
class OwnerMetadata:
    schema_version: int
    run_id: str
    pid: int
    pgid: int
    proc_starttime: int
    task_kind: TaskKind
    parent_pid: int
    started_at: float
    deadline: float
    lock_paths: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "pid": self.pid,
            "pgid": self.pgid,
            "proc_starttime": self.proc_starttime,
            "task_kind": self.task_kind.value,
            "parent_pid": self.parent_pid,
            "started_at": self.started_at,
            "deadline": self.deadline,
            "lock_paths": list(self.lock_paths),
        }

    @classmethod
    def from_dict(cls, value: Any) -> OwnerMetadata:
        if not isinstance(value, dict) or set(value) != _OWNER_FIELDS:
            raise ValueError("owner metadata fields do not match schema")
        if (
            type(value["schema_version"]) is not int
            or value["schema_version"] != SCHEMA_VERSION
        ):
            raise ValueError("unsupported owner metadata schema")
        run_id = _validated_run_id(value["run_id"])
        pid = _positive_int(value["pid"], "pid")
        pgid = _positive_int(value["pgid"], "pgid")
        if pgid != pid:
            raise ValueError("pgid must equal pid (process group leader)")
        starttime = _positive_int(value["proc_starttime"], "proc_starttime")
        parent_pid = _positive_int(value["parent_pid"], "parent_pid")
        try:
            task_kind = TaskKind(value["task_kind"])
        except (TypeError, ValueError) as error:
            raise ValueError("invalid task_kind") from error
        started_at = _finite_number(value["started_at"], "started_at")
        if not math.isfinite(started_at) or started_at <= 0:
            raise ValueError("started_at must be a positive finite number")
        deadline = _finite_number(value["deadline"], "deadline")
        if deadline <= started_at:
            raise ValueError("deadline must be strictly greater than started_at")
        lock_paths = _strict_lock_paths(value["lock_paths"])
        return cls(
            schema_version=SCHEMA_VERSION,
            run_id=run_id,
            pid=pid,
            pgid=pgid,
            proc_starttime=starttime,
            task_kind=task_kind,
            parent_pid=parent_pid,
            started_at=started_at,
            deadline=deadline,
            lock_paths=lock_paths,
        )


@dataclass(frozen=True)
class VerificationResult:
    verified: bool
    reason: str


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _validated_run_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("run_id must be a UUID hex string")
    try:
        parsed = uuid.UUID(hex=value)
    except (ValueError, AttributeError) as error:
        raise ValueError("run_id must be a UUID hex string") from error
    if parsed.hex != value:
        raise ValueError("run_id must use canonical UUID hex form")
    return parsed.hex


def _strict_lock_paths(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("lock_paths must be a list")
    if not value:
        raise ValueError("lock_paths must be a non-empty list")
    seen: set[str] = set()
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise ValueError("lock_paths must contain non-empty strings")
        real = os.path.realpath(item)
        if real != item:
            raise ValueError(f"lock path must be canonical realpath: {item}")
        if real in seen:
            raise ValueError("lock_paths contains duplicate canonical paths")
        seen.add(real)
        result.append(real)
    if result != sorted(result):
        raise ValueError("lock_paths must be sorted")
    return tuple(result)


def _normalized_paths_user(value: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Normalize user-provided paths (for build_owner_metadata only)."""
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("lock_paths must be a non-empty list")
    if any(not isinstance(item, str) or not item for item in value):
        raise ValueError("lock_paths must contain non-empty strings")
    return tuple(sorted({os.path.realpath(item) for item in value}))


def build_owner_metadata(
    *,
    run_id: str,
    pid: int,
    pgid: int,
    proc_starttime: int,
    task_kind: TaskKind,
    parent_pid: int,
    started_at: float,
    deadline: float,
    lock_paths: list[str] | tuple[str, ...],
) -> OwnerMetadata:
    normalized = _normalized_paths_user(lock_paths)
    return OwnerMetadata.from_dict(
        {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "pid": pid,
            "pgid": pgid,
            "proc_starttime": proc_starttime,
            "task_kind": task_kind.value,
            "parent_pid": parent_pid,
            "started_at": started_at,
            "deadline": deadline,
            "lock_paths": list(normalized),
        }
    )


def parse_proc_stat_starttime(line: str) -> int | None:
    """Return Linux ``/proc/<pid>/stat`` field 22 (process start time)."""
    closing_parenthesis = line.rfind(")")
    if closing_parenthesis < 0:
        return None
    tail = line[closing_parenthesis + 1 :].split()
    if len(tail) <= 19:
        return None
    try:
        result = int(tail[19])
    except ValueError:
        return None
    return result if result > 0 else None


def read_proc_starttime(pid: int) -> int | None:
    if sys.platform != "linux":
        return None
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as file:
            return parse_proc_stat_starttime(file.read())
    except OSError:
        return None


def read_proc_marker(pid: int) -> str | None:
    if sys.platform != "linux":
        return None
    try:
        with open(f"/proc/{pid}/environ", "rb") as file:
            entries = file.read().split(b"\0")
    except OSError:
        return None
    prefix = f"{RUN_ID_ENV}=".encode()
    for entry in entries:
        if entry.startswith(prefix):
            return entry[len(prefix) :].decode("utf-8", "surrogateescape")
    return None


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def owner_metadata_path(lock_path: str) -> str:
    return f"{os.path.realpath(lock_path)}.owner.json"


def _fsync_directory(directory: str) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: str, payload: bytes) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary_path = tempfile.mkstemp(prefix=".owner-", dir=directory)
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("short owner metadata write")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary_path, path)
        _fsync_directory(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass


def _read_owner_file_safely(path: str) -> bytes:
    """Read owner file with size limit, O_NOFOLLOW, and regular file check."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("owner metadata is not a regular file")
        if st.st_size > _MAX_OWNER_SIZE:
            raise ValueError("owner metadata exceeds size limit")
        data = b""
        while len(data) < _MAX_OWNER_SIZE:
            chunk = os.read(fd, _MAX_OWNER_SIZE - len(data))
            if not chunk:
                break
            data += chunk
        if len(data) >= _MAX_OWNER_SIZE:
            raise ValueError("owner metadata exceeds size limit")
        return data
    finally:
        os.close(fd)


def _load_owner_metadata_strict(lock_path: str) -> OwnerMetadata | None:
    """Load owner metadata with strict schema validation.

    Must be called while holding the corresponding flock.
    """
    path = owner_metadata_path(lock_path)
    try:
        data = _read_owner_file_safely(path)
        parsed = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
        return OwnerMetadata.from_dict(parsed)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise ValueError(f"duplicate JSON key: {key}")
        seen.add(key)
    return dict(pairs)


def load_owner_metadata(lock_path: str) -> OwnerMetadata | None:
    """Load owner metadata from disk.

    Note: For matched-delete operations, this must be called while holding
    the corresponding flock to avoid TOCTOU races.
    """
    return _load_owner_metadata_strict(lock_path)


def _metadata_matches_full(left: OwnerMetadata, right: OwnerMetadata) -> bool:
    return (
        left.schema_version == right.schema_version
        and left.run_id == right.run_id
        and left.pid == right.pid
        and left.pgid == right.pgid
        and left.proc_starttime == right.proc_starttime
        and left.task_kind == right.task_kind
        and left.parent_pid == right.parent_pid
        and left.started_at == right.started_at
        and left.deadline == right.deadline
        and left.lock_paths == right.lock_paths
    )


def delete_owner_metadata_if_matched(lock_path: str, metadata: OwnerMetadata) -> bool:
    """Delete owner metadata if it fully matches the provided metadata.

    Must be called while holding the corresponding flock to avoid TOCTOU races.
    Does not delete the lock file itself.
    """
    path = owner_metadata_path(lock_path)
    current = _load_owner_metadata_strict(lock_path)
    if current is None or not _metadata_matches_full(current, metadata):
        return False
    try:
        os.unlink(path)
    except FileNotFoundError:
        return False
    _fsync_directory(os.path.dirname(path) or ".")
    return True


def delete_owner_metadata_for_locks_if_matched(metadata: OwnerMetadata) -> None:
    """Best-effort deletion of all owner metadata files for the given metadata.

    Must be called while holding all corresponding flocks.
    Cleanup errors are collected but the first error is not raised to avoid
    masking the original write failure. Caller should inspect results.
    """
    for lock_path in metadata.lock_paths:
        try:
            delete_owner_metadata_if_matched(lock_path, metadata)
        except Exception:
            pass


def write_owner_metadata_for_locks(metadata: OwnerMetadata) -> None:
    payload = json.dumps(metadata.to_dict(), separators=(",", ":")).encode()
    written: list[str] = []
    try:
        for lock_path in metadata.lock_paths:
            _atomic_write(owner_metadata_path(lock_path), payload)
            written.append(lock_path)
    except Exception as write_error:
        cleanup_failures: list[tuple[str, BaseException]] = []
        for lock_path in metadata.lock_paths:
            try:
                delete_owner_metadata_if_matched(lock_path, metadata)
            except Exception as cleanup_error:
                cleanup_failures.append((lock_path, cleanup_error))
        if cleanup_failures:
            raise OwnerCleanupError(
                f"owner write failed and cleanup had {len(cleanup_failures)} errors",
                cleanup_failures,
            ) from write_error
        raise


def verify_owner(
    metadata: OwnerMetadata,
    required_locks: list[str] | tuple[str, ...],
    expected_kind: TaskKind | None = None,
) -> VerificationResult:
    if sys.platform != "linux":
        return VerificationResult(False, "unsupported_platform")
    process_result = _verify_process_identity(metadata)
    if process_result is not None:
        return process_result
    return _verify_owner_scope(metadata, required_locks, expected_kind)


def _verify_process_identity(
    metadata: OwnerMetadata,
) -> VerificationResult | None:
    if not process_exists(metadata.pid):
        return VerificationResult(False, "pid_not_alive")
    try:
        actual_pgid = os.getpgid(metadata.pid)
    except (OSError, ProcessLookupError, PermissionError):
        return VerificationResult(False, "pgid_unreadable")
    if actual_pgid != metadata.pgid:
        return VerificationResult(False, "pgid_mismatch")
    if actual_pgid == os.getpgrp():
        return VerificationResult(False, "pgid_is_supervisor")
    try:
        if actual_pgid == os.getpgid(metadata.parent_pid):
            return VerificationResult(False, "pgid_is_parent")
    except (OSError, ProcessLookupError, PermissionError):
        pass
    if read_proc_starttime(metadata.pid) != metadata.proc_starttime:
        return VerificationResult(False, "starttime_mismatch")
    if read_proc_marker(metadata.pid) != metadata.run_id:
        return VerificationResult(False, "run_id_mismatch")
    return None


def _verify_owner_scope(
    metadata: OwnerMetadata,
    required_locks: list[str] | tuple[str, ...],
    expected_kind: TaskKind | None,
) -> VerificationResult:
    try:
        normalized_required = _strict_lock_paths(list(required_locks))
    except ValueError:
        return VerificationResult(False, "required_locks_invalid")
    if normalized_required != metadata.lock_paths:
        return VerificationResult(False, "lock_paths_mismatch")
    if expected_kind is not None and metadata.task_kind is not expected_kind:
        return VerificationResult(False, "task_kind_mismatch")
    return VerificationResult(True, "verified")


@contextmanager
def claimed_repo_locks(metadata: OwnerMetadata) -> Iterator[None]:
    """Acquire every flock before publishing advisory owner metadata.

    On write failure, attempts best-effort cleanup of all owner files while
    still holding flocks. Cleanup failures raise OwnerCleanupError unless the
    body already raised; in that case the body exception remains primary and
    cleanup failures are logged and attached as a note. Lock files are never
    deleted.
    """
    with repo_file_locks(metadata.lock_paths, non_blocking=True):
        write_owner_metadata_for_locks(metadata)
        body_error: BaseException | None = None
        try:
            yield
        except BaseException as error:
            body_error = error
            raise
        finally:
            cleanup_failures: list[tuple[str, BaseException]] = []
            for lock_path in metadata.lock_paths:
                try:
                    delete_owner_metadata_if_matched(lock_path, metadata)
                except Exception as error:
                    cleanup_failures.append((lock_path, error))
            if cleanup_failures:
                cleanup_error = OwnerCleanupError(
                    "owner cleanup had "
                    f"{len(cleanup_failures)} errors while holding flocks",
                    cleanup_failures,
                )
                if body_error is None:
                    raise cleanup_error
                body_error.add_note(f"{cleanup_error}: {cleanup_failures!r}")
                logger.error(
                    "owner cleanup failed while preserving the context body "
                    "exception: %s; failures=%r",
                    cleanup_error,
                    cleanup_failures,
                )


def cleanup_stale_owner_metadata_while_locked(metadata: OwnerMetadata) -> None:
    """Remove stale owner metadata for the given metadata's lock paths.

    Must be called while holding ALL corresponding flocks (typically via
    repo_file_locks). This allows a new claim to recover from a crashed
    previous run by cleaning up its stale owner files before writing new ones.

    Does not signal any processes. Only deletes owner metadata files that
    fully match the provided metadata.
    """
    for lock_path in metadata.lock_paths:
        delete_owner_metadata_if_matched(lock_path, metadata)
