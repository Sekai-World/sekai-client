"""SQLite-backed durable outbox for event-ranking delivery."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PRUNE_BATCH_SIZE = 100


@dataclass(frozen=True)
class OutboxMetrics:
    pending: int
    sending: int
    failed: int
    oldest_pending_age_seconds: float


class EventRankingOutbox:
    """Persist, claim, and acknowledge idempotent ranking deliveries."""

    def __init__(
        self,
        path: str,
        *,
        retry_base_seconds: float = 5.0,
        retention_seconds: float = 86400.0,
    ) -> None:
        if retry_base_seconds <= 0:
            raise ValueError("retry_base_seconds must be positive")
        if retention_seconds <= 0:
            raise ValueError("retention_seconds must be positive")
        self.path = Path(path)
        self.retry_base_seconds = retry_base_seconds
        self.retention_seconds = retention_seconds
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS event_ranking_outbox (
                    idempotency_key TEXT PRIMARY KEY,
                    region TEXT NOT NULL,
                    event_id INTEGER NOT NULL,
                    collected_at INTEGER NOT NULL,
                    data_type TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK(status IN ('pending','sending','sent','failed')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL,
                    claim_id TEXT,
                    claim_expires_at REAL,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    sent_at REAL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS event_ranking_outbox_ready "
                "ON event_ranking_outbox(status, next_attempt_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS event_ranking_outbox_terminal "
                "ON event_ranking_outbox(status, collected_at)"
            )
        self.path.chmod(0o600)
        self.prune_terminal()

    def enqueue(
        self,
        *,
        region: str,
        event_id: int,
        collected_at: int,
        data_type: str,
        endpoint: str,
        payload: dict[str, Any],
    ) -> str:
        key = f"{region}:{event_id}:{collected_at}:{data_type}"
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO event_ranking_outbox
                (idempotency_key,region,event_id,collected_at,data_type,endpoint,
                 payload_json,status,next_attempt_at,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,'pending',?,?,?)""",
                (
                    key,
                    region,
                    event_id,
                    collected_at,
                    data_type,
                    endpoint,
                    encoded,
                    now,
                    now,
                    now,
                ),
            )
        return key

    def drain(
        self,
        deliver: Callable[[str, dict[str, Any], str], None],
        *,
        limit: int = 20,
        lease_seconds: float = 120.0,
        max_attempts: int = 10,
        max_duration_seconds: float = 30.0,
    ) -> dict[str, int]:
        if limit < 0:
            raise ValueError("limit must be non-negative")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if max_duration_seconds <= 0:
            raise ValueError("max_duration_seconds must be positive")
        deadline = time.monotonic() + max_duration_seconds
        self._prune_terminal(deadline=deadline)
        result = {"sent": 0, "failed": 0, "retained": 0}
        for _ in range(limit):
            if time.monotonic() >= deadline:
                break
            claimed = self._claim(lease_seconds)
            if claimed is None:
                break
            key, endpoint, payload, claim_id, attempts = claimed
            try:
                deliver(endpoint, payload, key)
            except Exception as error:
                terminal = attempts >= max_attempts
                self._reject(key, claim_id, type(error).__name__, attempts, terminal)
                result["failed"] += int(terminal)
                result["retained"] += int(not terminal)
            else:
                self._ack(key, claim_id)
                result["sent"] += 1
        return result

    def prune_terminal(self) -> int:
        """Remove terminal records older than the configured retention window."""
        return self._prune_terminal(deadline=None)

    def _prune_terminal(self, *, deadline: float | None) -> int:
        """Remove old terminal records in bounded batches until the deadline."""
        # collected_at is the event timestamp in milliseconds since epoch.
        cutoff = (time.time() - self.retention_seconds) * 1000
        deleted = 0
        while deadline is None or time.monotonic() < deadline:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT idempotency_key FROM event_ranking_outbox "
                    "WHERE status IN ('sent','failed') AND collected_at <= ? "
                    "ORDER BY collected_at,idempotency_key LIMIT ?",
                    (cutoff, _PRUNE_BATCH_SIZE),
                ).fetchall()
                if not rows:
                    break
                cursor = connection.executemany(
                    "DELETE FROM event_ranking_outbox "
                    "WHERE idempotency_key=? AND status IN ('sent','failed')",
                    ((row["idempotency_key"],) for row in rows),
                )
                deleted += cursor.rowcount
            if len(rows) < _PRUNE_BATCH_SIZE:
                break
        return deleted

    def _claim(
        self, lease_seconds: float
    ) -> tuple[str, str, dict[str, Any], str, int] | None:
        now = time.time()
        claim_id = uuid.uuid4().hex
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE event_ranking_outbox SET status='pending',claim_id=NULL,"
                "claim_expires_at=NULL "
                "WHERE status='sending' AND claim_expires_at<=?",
                (now,),
            )
            row = connection.execute(
                "SELECT * FROM event_ranking_outbox WHERE status='pending' "
                "AND next_attempt_at<=? "
                "ORDER BY collected_at,idempotency_key LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                return None
            attempts = int(row["attempts"]) + 1
            connection.execute(
                "UPDATE event_ranking_outbox SET status='sending',attempts=?,"
                "claim_id=?,"
                "claim_expires_at=?,updated_at=? WHERE idempotency_key=?",
                (attempts, claim_id, now + lease_seconds, now, row["idempotency_key"]),
            )
            return (
                row["idempotency_key"],
                row["endpoint"],
                json.loads(row["payload_json"]),
                claim_id,
                attempts,
            )

    def _ack(self, key: str, claim_id: str) -> None:
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                "UPDATE event_ranking_outbox SET status='sent',claim_id=NULL,"
                "claim_expires_at=NULL,sent_at=?,updated_at=?,last_error=NULL "
                "WHERE idempotency_key=? AND claim_id=?",
                (now, now, key, claim_id),
            )

    def _reject(
        self, key: str, claim_id: str, error: str, attempts: int, terminal: bool
    ) -> None:
        now = time.time()
        delay = min(3600.0, self.retry_base_seconds * (2 ** min(attempts - 1, 10)))
        with self._connect() as connection:
            connection.execute(
                "UPDATE event_ranking_outbox SET status=?,claim_id=NULL,"
                "claim_expires_at=NULL,next_attempt_at=?,updated_at=?,last_error=? "
                "WHERE idempotency_key=? AND claim_id=?",
                (
                    "failed" if terminal else "pending",
                    now + delay,
                    now,
                    error,
                    key,
                    claim_id,
                ),
            )

    def metrics(self) -> OutboxMetrics:
        now = time.time()
        with self._connect() as connection:
            counts = dict(
                connection.execute(
                    "SELECT status,COUNT(*) FROM event_ranking_outbox GROUP BY status"
                )
            )
            oldest = connection.execute(
                "SELECT MIN(created_at) FROM event_ranking_outbox "
                "WHERE status IN ('pending','sending')"
            ).fetchone()[0]
        return OutboxMetrics(
            pending=counts.get("pending", 0),
            sending=counts.get("sending", 0),
            failed=counts.get("failed", 0),
            oldest_pending_age_seconds=max(0.0, now - oldest)
            if oldest is not None
            else 0.0,
        )
