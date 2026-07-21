"""Durable local outbox for deferred Strapi ID publication.

The check-update generation path records Strapi notifications here while a Git
transaction is still in progress. Delivery is intentionally separate: callers
mark a completed transaction ready only after Git commit/push success, then drain
ready records. State is protected by a small ``flock`` and every mutation is an
atomic JSON rewrite.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import requests
import ujson as json

from utils.redaction import redact_text

SCHEMA_VERSION = 1


class StrapiOutboxError(RuntimeError):
    """Raised when outbox state is malformed or cannot be safely updated."""


def _fsync_dir(directory: str) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_json(target_path: str, payload: dict[str, Any]) -> None:
    parent = os.path.dirname(target_path) or "."
    os.makedirs(parent, exist_ok=True)
    tmp_path = f"{target_path}.tmp.{uuid.uuid4().hex}"
    encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, encoded)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp_path, target_path)
    _fsync_dir(parent)


def _canonical_ids(ids: list[int]) -> list[int]:
    values: set[int] = set()
    for value in ids:
        if isinstance(value, bool) or not isinstance(value, int):
            raise StrapiOutboxError(f"Strapi id is not an integer: {value!r}")
        values.add(value)
    return sorted(values)


def _canonical_endpoint(endpoint: str) -> str:
    if not isinstance(endpoint, str):
        raise StrapiOutboxError("Strapi endpoint must be a string")
    value = endpoint.strip().strip("/")
    if not value or ":" in value or "?" in value or "#" in value:
        raise StrapiOutboxError(f"Strapi endpoint is not a relative path: {endpoint!r}")
    if any(part in ("", ".", "..") for part in value.split("/")):
        raise StrapiOutboxError(f"Strapi endpoint is not canonical: {endpoint!r}")
    return value


def semantic_key(endpoint: str, ids: list[int]) -> tuple[str, str, list[int]]:
    """Return ``(key, canonical_endpoint, canonical_ids)`` for a Strapi post."""
    canonical_endpoint = _canonical_endpoint(endpoint)
    canonical_values = _canonical_ids(ids)
    key_payload = {
        "endpoint": canonical_endpoint,
        "ids": canonical_values,
    }
    encoded = json.dumps(key_payload, ensure_ascii=False, sort_keys=True)
    return (
        hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        canonical_endpoint,
        canonical_values,
    )


class StrapiOutbox:
    """Atomic JSON-file outbox with ``flock``-guarded read/modify/write."""

    def __init__(self, file_path: str, lock_path: str | None = None) -> None:
        self.file_path = os.path.realpath(file_path)
        self.lock_path = os.path.realpath(lock_path or f"{file_path}.lock")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        parent = os.path.dirname(self.lock_path) or "."
        os.makedirs(parent, exist_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def enqueue(
        self,
        endpoint: str,
        ids: list[int],
        *,
        transaction_id: str | None,
    ) -> str | None:
        """Persist a deferred record, deduped by endpoint + canonical IDs.

        Empty ID lists are no-ops. Existing ready records are never downgraded to
        deferred; existing deferred records are rebound to the latest transaction
        that observed the same semantic notification.
        """
        key, canonical_endpoint, canonical_values = semantic_key(endpoint, ids)
        if not canonical_values:
            return None
        now = time.time()
        with self._locked():
            data = self._load_unlocked()
            records = data["records"]
            old = records.get(key)
            ready = bool(old.get("ready")) if isinstance(old, dict) else False
            records[key] = {
                "key": key,
                "endpoint": canonical_endpoint,
                "ids": canonical_values,
                "transaction_id": (
                    old.get("transaction_id") if ready and isinstance(old, dict)
                    else transaction_id
                ),
                "ready": ready,
                "attempts": int(old.get("attempts", 0)) if isinstance(old, dict) else 0,
                "created_at": (
                    old.get("created_at", now) if isinstance(old, dict) else now
                ),
                "updated_at": now,
                "last_error": old.get("last_error") if isinstance(old, dict) else None,
            }
            self._save_unlocked(data)
        return key

    def mark_transaction_ready(self, transaction_id: str) -> int:
        """Mark records from a completed Git transaction eligible for delivery."""
        if not transaction_id:
            return 0
        now = time.time()
        marked = 0
        with self._locked():
            data = self._load_unlocked()
            for record in data["records"].values():
                if (
                    record.get("transaction_id") == transaction_id
                    and not record.get("ready")
                ):
                    record["ready"] = True
                    record["updated_at"] = now
                    marked += 1
            if marked:
                self._save_unlocked(data)
        return marked

    def drain(
        self,
        *,
        base_url: str | None,
        token: str | None,
        post: Callable[..., requests.Response] | None = None,
        timeout: int = 60,
    ) -> dict[str, int]:
        """Send ready records and delete each only after HTTP success.

        Missing/disabled Strapi configuration is not an error: records remain on
        disk for a later cycle. Malformed state raises ``StrapiOutboxError`` and
        no delivery is attempted.
        """
        if not base_url or not token:
            return {"sent": 0, "failed": 0, "retained": 0}
        post_func = post or requests.post
        sent = 0
        failed = 0
        with self._locked():
            data = self._load_unlocked()
            for key in sorted(data["records"].keys()):
                record = data["records"].get(key)
                if not record or not record.get("ready"):
                    continue
                url = f"{base_url.rstrip('/')}/{record['endpoint']}"
                try:
                    response = post_func(
                        url,
                        json=list(record["ids"]),
                        headers={
                            "Authorization": f"Bearer {token}",
                            "X-Strapi-Token": token,
                        },
                        timeout=timeout,
                    )
                    response.raise_for_status()
                except requests.RequestException as err:
                    failed += 1
                    record["attempts"] = int(record.get("attempts", 0)) + 1
                    record["updated_at"] = time.time()
                    record["last_error"] = redact_text(str(err))[:500]
                    self._save_unlocked(data)
                    continue
                del data["records"][key]
                sent += 1
                self._save_unlocked(data)
            # Count while still holding the outbox lock. Never call
            # pending_count() here: it re-enters _locked() and deadlocks.
            retained = len(data["records"])
        return {"sent": sent, "failed": failed, "retained": retained}

    def pending_count(self) -> int:
        with self._locked():
            return len(self._load_unlocked()["records"])

    def _load_unlocked(self) -> dict[str, Any]:
        if (
            os.path.lexists(self.file_path)
            and os.path.realpath(self.file_path) != self.file_path
        ):
            raise StrapiOutboxError("Strapi outbox path is a symlink")
        if not os.path.exists(self.file_path):
            return {"schema_version": SCHEMA_VERSION, "records": {}}
        try:
            with open(self.file_path, encoding="utf-8") as stream:
                data = json.load(stream)
        except (OSError, ValueError, json.JSONDecodeError) as err:
            raise StrapiOutboxError(f"Strapi outbox is not valid JSON: {err}") from err
        self._validate(data)
        return data

    def _save_unlocked(self, data: dict[str, Any]) -> None:
        self._validate(data)
        _atomic_write_json(self.file_path, data)

    def _validate(self, data: Any) -> None:
        if not isinstance(data, dict):
            raise StrapiOutboxError("Strapi outbox root must be an object")
        if set(data) != {"schema_version", "records"}:
            raise StrapiOutboxError("Strapi outbox has unexpected keys")
        if data.get("schema_version") != SCHEMA_VERSION:
            raise StrapiOutboxError("Strapi outbox schema version is unsupported")
        records = data.get("records")
        if not isinstance(records, dict):
            raise StrapiOutboxError("Strapi outbox records must be an object")
        for key, record in records.items():
            self._validate_record(key, record)

    def _validate_record_shape(self, key: str, record: Any) -> dict[str, Any]:
        if not isinstance(key, str) or len(key) != 64:
            raise StrapiOutboxError(f"Strapi outbox key is invalid: {key!r}")
        if not isinstance(record, dict):
            raise StrapiOutboxError(f"Strapi outbox record is invalid: {key!r}")
        required = {
            "key", "endpoint", "ids", "transaction_id", "ready", "attempts",
            "created_at", "updated_at", "last_error",
        }
        if set(record) != required or record.get("key") != key:
            raise StrapiOutboxError(f"Strapi outbox record shape is invalid: {key!r}")
        return record

    def _validate_record(self, key: str, record: Any) -> None:
        record = self._validate_record_shape(key, record)
        expected_key, expected_endpoint, expected_ids = semantic_key(
            record["endpoint"], record["ids"]
        )
        if expected_key != key:
            raise StrapiOutboxError(f"Strapi outbox semantic key mismatch: {key!r}")
        if record["endpoint"] != expected_endpoint or record["ids"] != expected_ids:
            raise StrapiOutboxError(f"Strapi outbox record is not canonical: {key!r}")
        self._validate_record_fields(key, record)

    def _validate_record_fields(self, key: str, record: dict[str, Any]) -> None:
        if record["transaction_id"] is not None and not isinstance(
            record["transaction_id"], str
        ):
            raise StrapiOutboxError(f"Strapi outbox transaction id invalid: {key!r}")
        if not isinstance(record["ready"], bool):
            raise StrapiOutboxError(f"Strapi outbox ready flag invalid: {key!r}")
        if not isinstance(record["attempts"], int) or record["attempts"] < 0:
            raise StrapiOutboxError(f"Strapi outbox attempts invalid: {key!r}")
        if not isinstance(record["created_at"], (int, float)):
            raise StrapiOutboxError(f"Strapi outbox created_at invalid: {key!r}")
        if not isinstance(record["updated_at"], (int, float)):
            raise StrapiOutboxError(f"Strapi outbox updated_at invalid: {key!r}")
        if record["last_error"] is not None and not isinstance(
            record["last_error"], str
        ):
            raise StrapiOutboxError(f"Strapi outbox last_error invalid: {key!r}")
