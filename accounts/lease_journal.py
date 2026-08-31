"""Crash-safe journal for remote account lease acquisition and release."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from fcntl import LOCK_EX, LOCK_UN, flock
from hashlib import sha256
from pathlib import Path
from stat import S_IMODE
from uuid import uuid4


@dataclass(frozen=True)
class LeaseOperation:
    region: str
    consumer: str
    idempotency_key: str
    lease_id: str | None = None
    expires_at: datetime | None = None
    release_pending: bool = False


class LeaseJournal:
    """Persist one logical acquire operation without storing credentials."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def load_or_create(self, region: str, consumer: str) -> LeaseOperation:
        target = self._path(region, consumer)
        with self._locked(target):
            current = self._load(target, region, consumer)
            now = datetime.now(UTC)
            if current is not None and (
                current.expires_at is None or current.expires_at > now
            ):
                return current
            operation = LeaseOperation(region, consumer, f"login-{uuid4()}")
            self._write(operation)
            return operation

    def load(self, region: str, consumer: str) -> LeaseOperation | None:
        target = self._path(region, consumer)
        with self._locked(target):
            return self._load(target, region, consumer)

    def _load(self, target: Path, region: str, consumer: str) -> LeaseOperation | None:
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError("remote lease journal is unreadable") from error
        try:
            expires_at = (
                datetime.fromisoformat(payload["expires_at"])
                if payload.get("expires_at")
                else None
            )
            operation = LeaseOperation(
                region=payload["region"],
                consumer=payload["consumer"],
                idempotency_key=payload["idempotency_key"],
                lease_id=payload.get("lease_id"),
                expires_at=expires_at,
                release_pending=payload.get("release_pending", False),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("remote lease journal is invalid") from error
        if (
            operation.region != region
            or operation.consumer != consumer
            or not operation.idempotency_key
            or (operation.release_pending and not operation.lease_id)
            or (operation.expires_at and operation.expires_at.tzinfo is None)
        ):
            raise RuntimeError("remote lease journal is invalid")
        return operation

    def mark_acquired(
        self, operation: LeaseOperation, lease_id: str, expires_at: datetime
    ) -> LeaseOperation:
        acquired = LeaseOperation(
            operation.region,
            operation.consumer,
            operation.idempotency_key,
            lease_id,
            expires_at.astimezone(UTC),
        )
        with self._locked(self._path(operation.region, operation.consumer)):
            self._write(acquired)
        return acquired

    def mark_renewed(
        self, operation: LeaseOperation, expires_at: datetime
    ) -> LeaseOperation:
        if not operation.lease_id:
            raise RuntimeError("cannot renew an unconfirmed lease")
        if expires_at.tzinfo is None:
            raise ValueError("lease expiry must be timezone-aware")
        renewed = LeaseOperation(
            operation.region,
            operation.consumer,
            operation.idempotency_key,
            operation.lease_id,
            expires_at.astimezone(UTC),
            operation.release_pending,
        )
        with self._locked(self._path(operation.region, operation.consumer)):
            self._write(renewed)
        return renewed

    def mark_release_pending(self, operation: LeaseOperation) -> LeaseOperation:
        pending = LeaseOperation(
            operation.region,
            operation.consumer,
            operation.idempotency_key,
            operation.lease_id,
            operation.expires_at,
            True,
        )
        if not pending.lease_id:
            raise RuntimeError("cannot release an unconfirmed lease")
        with self._locked(self._path(operation.region, operation.consumer)):
            self._write(pending)
        return pending

    def clear(self, operation: LeaseOperation) -> None:
        target = self._path(operation.region, operation.consumer)
        with self._locked(target):
            current = self._load(target, operation.region, operation.consumer)
            if current is None or current.idempotency_key != operation.idempotency_key:
                return
            target.unlink()
            self._fsync_directory()

    def _path(self, region: str, consumer: str) -> Path:
        identity = sha256(f"{region}:{consumer}".encode()).hexdigest()[:24]
        return self.directory / f"lease-{identity}.json"

    def _write(self, operation: LeaseOperation) -> None:
        payload = {
            "version": 1,
            "region": operation.region,
            "consumer": operation.consumer,
            "idempotency_key": operation.idempotency_key,
            "lease_id": operation.lease_id,
            "expires_at": operation.expires_at.isoformat()
            if operation.expires_at
            else None,
            "release_pending": operation.release_pending,
        }
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.directory, prefix=".lease-", suffix=".tmp"
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, separators=(",", ":"), sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path(operation.region, operation.consumer))
            self._fsync_directory()
        except Exception:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise

    @contextmanager
    def _locked(self, target: Path) -> Iterator[None]:
        self._ensure_directory()
        lock_path = target.with_suffix(".lock")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            flock(descriptor, LOCK_EX)
            yield
        finally:
            flock(descriptor, LOCK_UN)
            os.close(descriptor)

    def _ensure_directory(self) -> None:
        resolved = self.directory.resolve()
        temporary_root = Path(tempfile.gettempdir()).resolve()
        if resolved == temporary_root:
            raise RuntimeError(
                "lease journal cannot use the shared temporary directory"
            )
        try:
            metadata = resolved.stat()
        except FileNotFoundError:
            self.directory.mkdir(mode=0o700, parents=True)
            metadata = resolved.stat()
        if metadata.st_uid != os.geteuid() or S_IMODE(metadata.st_mode) & 0o077:
            raise RuntimeError(
                "lease journal directory must be private and owner-controlled"
            )

    def _fsync_directory(self) -> None:
        descriptor = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
