"""File-backed Binance API request queue foundation.

This is the P0-A construction-mode bridge from cooperative per-process guards
to a central queue service. It persists request intent, priority, cooldown,
leases, and results without requiring scanners to switch over yet.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = ROOT / "runtime" / "binance_api_queue.sqlite3"

STATUS_QUEUED = "queued"
STATUS_DEFERRED = "deferred"
STATUS_LEASED = "leased"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

PRIORITY_NORMAL = 0
PRIORITY_HIGH = 50
PRIORITY_TRADE = 100


def now_ms() -> int:
    return int(time.time() * 1000)


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, default=str)


def _json_loads(value: str | None) -> Any:
    if not value:
        return {}
    try:
        return json.loads(value)
    except Exception:
        return {}


@dataclass(frozen=True)
class ApiQueueRequest:
    request_id: str
    idempotency_key: str
    scope: str
    account: str
    label: str
    method: str
    path: str
    url: str
    priority: int
    status: str
    earliest_ms: int
    lease_until_ms: int
    attempts: int
    headers: dict[str, Any]
    body: dict[str, Any]
    result_status: int | None
    result_body: Any
    error: str


def _request_from_row(row: sqlite3.Row) -> ApiQueueRequest:
    result_status = row["result_status"]
    return ApiQueueRequest(
        request_id=str(row["request_id"]),
        idempotency_key=str(row["idempotency_key"] or ""),
        scope=str(row["scope"] or ""),
        account=str(row["account"] or ""),
        label=str(row["label"] or ""),
        method=str(row["method"] or ""),
        path=str(row["path"] or ""),
        url=str(row["url"] or ""),
        priority=int(row["priority"] or 0),
        status=str(row["status"] or ""),
        earliest_ms=int(row["earliest_ms"] or 0),
        lease_until_ms=int(row["lease_until_ms"] or 0),
        attempts=int(row["attempts"] or 0),
        headers=_json_loads(row["headers_json"]),
        body=_json_loads(row["body_json"]),
        result_status=int(result_status) if result_status is not None else None,
        result_body=_json_loads(row["result_body_json"]),
        error=str(row["error"] or ""),
    )


class BinanceApiQueue:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS api_requests (
                    request_id TEXT PRIMARY KEY,
                    idempotency_key TEXT UNIQUE,
                    scope TEXT NOT NULL,
                    account TEXT NOT NULL DEFAULT '',
                    label TEXT NOT NULL DEFAULT '',
                    method TEXT NOT NULL,
                    path TEXT NOT NULL,
                    url TEXT NOT NULL DEFAULT '',
                    priority INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    earliest_ms INTEGER NOT NULL DEFAULT 0,
                    lease_until_ms INTEGER NOT NULL DEFAULT 0,
                    leased_by TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    headers_json TEXT NOT NULL DEFAULT '{}',
                    body_json TEXT NOT NULL DEFAULT '{}',
                    result_status INTEGER,
                    result_body_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_api_requests_ready
                    ON api_requests(status, earliest_ms, priority, created_at_ms);
                CREATE INDEX IF NOT EXISTS idx_api_requests_scope
                    ON api_requests(scope, account, status);
                CREATE TABLE IF NOT EXISTS api_cooldowns (
                    cooldown_key TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    account TEXT NOT NULL DEFAULT '',
                    until_ms INTEGER NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                );
                """
            )

    def submit_request(
        self,
        *,
        scope: str,
        method: str,
        path: str,
        account: str = "",
        label: str = "",
        url: str = "",
        headers: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        priority: int = PRIORITY_NORMAL,
        earliest_ms: int | None = None,
        idempotency_key: str | None = None,
    ) -> ApiQueueRequest:
        created = now_ms()
        idem = idempotency_key or f"{scope}:{account}:{method.upper()}:{path}:{uuid.uuid4().hex}"
        request_id = uuid.uuid4().hex
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT * FROM api_requests WHERE idempotency_key = ?",
                    (idem,),
                ).fetchone()
                if existing:
                    conn.execute("COMMIT")
                    return _request_from_row(existing)
                conn.execute(
                    """
                    INSERT INTO api_requests (
                        request_id, idempotency_key, scope, account, label, method,
                        path, url, priority, status, earliest_ms, headers_json,
                        body_json, created_at_ms, updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_id,
                        idem,
                        scope,
                        account,
                        label,
                        method.upper(),
                        path,
                        url,
                        int(priority),
                        STATUS_QUEUED,
                        int(earliest_ms if earliest_ms is not None else created),
                        _json_dumps(headers),
                        _json_dumps(body),
                        created,
                        created,
                    ),
                )
                row = conn.execute("SELECT * FROM api_requests WHERE request_id = ?", (request_id,)).fetchone()
                conn.execute("COMMIT")
                return _request_from_row(row)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def set_cooldown(self, *, scope: str, until_ms: int, reason: str = "", account: str = "") -> None:
        key = f"{scope}:{account}" if account else scope
        ts = now_ms()
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO api_cooldowns (cooldown_key, scope, account, until_ms, reason, created_at_ms, updated_at_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cooldown_key) DO UPDATE SET
                    until_ms = excluded.until_ms,
                    reason = excluded.reason,
                    updated_at_ms = excluded.updated_at_ms
                """,
                (key, scope, account, int(until_ms), reason, ts, ts),
            )

    def active_cooldown(self, *, scope: str, account: str = "", at_ms: int | None = None) -> tuple[int, str]:
        ts = now_ms() if at_ms is None else int(at_ms)
        keys = ["global", scope]
        if account:
            keys.append(f"{scope}:{account}")
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT until_ms, reason FROM api_cooldowns WHERE cooldown_key IN (%s)"
                % ",".join("?" for _ in keys),
                keys,
            ).fetchall()
        active = [(int(row["until_ms"] or 0), str(row["reason"] or "")) for row in rows if int(row["until_ms"] or 0) > ts]
        if not active:
            return 0, ""
        return max(active, key=lambda item: item[0])

    def lease_next(
        self,
        *,
        worker_id: str = "",
        lease_ms: int = 30_000,
        at_ms: int | None = None,
    ) -> ApiQueueRequest | None:
        ts = now_ms() if at_ms is None else int(at_ms)
        lease_until = ts + max(1_000, int(lease_ms))
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    UPDATE api_requests
                    SET status = ?, lease_until_ms = 0, leased_by = '', updated_at_ms = ?
                    WHERE status = ? AND lease_until_ms <= ?
                    """,
                    (STATUS_QUEUED, ts, STATUS_LEASED, ts),
                )
                rows = conn.execute(
                    """
                    SELECT * FROM api_requests
                    WHERE status IN (?, ?) AND earliest_ms <= ?
                    ORDER BY priority DESC, created_at_ms ASC
                    LIMIT 50
                    """,
                    (STATUS_QUEUED, STATUS_DEFERRED, ts),
                ).fetchall()
                for row in rows:
                    cooldown_until, reason = self._active_cooldown_in_conn(
                        conn,
                        scope=str(row["scope"] or ""),
                        account=str(row["account"] or ""),
                        at_ms=ts,
                    )
                    if cooldown_until > ts:
                        conn.execute(
                            """
                            UPDATE api_requests
                            SET status = ?, earliest_ms = ?, error = ?, updated_at_ms = ?
                            WHERE request_id = ?
                            """,
                            (STATUS_DEFERRED, cooldown_until, reason, ts, row["request_id"]),
                        )
                        continue
                    conn.execute(
                        """
                        UPDATE api_requests
                        SET status = ?, lease_until_ms = ?, leased_by = ?, attempts = attempts + 1, updated_at_ms = ?
                        WHERE request_id = ?
                        """,
                        (STATUS_LEASED, lease_until, worker_id, ts, row["request_id"]),
                    )
                    leased = conn.execute("SELECT * FROM api_requests WHERE request_id = ?", (row["request_id"],)).fetchone()
                    conn.execute("COMMIT")
                    return _request_from_row(leased)
                conn.execute("COMMIT")
                return None
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def _active_cooldown_in_conn(
        self,
        conn: sqlite3.Connection,
        *,
        scope: str,
        account: str,
        at_ms: int,
    ) -> tuple[int, str]:
        keys = ["global", scope]
        if account:
            keys.append(f"{scope}:{account}")
        rows = conn.execute(
            "SELECT until_ms, reason FROM api_cooldowns WHERE cooldown_key IN (%s)"
            % ",".join("?" for _ in keys),
            keys,
        ).fetchall()
        active = [(int(row["until_ms"] or 0), str(row["reason"] or "")) for row in rows if int(row["until_ms"] or 0) > at_ms]
        if not active:
            return 0, ""
        return max(active, key=lambda item: item[0])

    def complete_request(self, request_id: str, *, result_status: int, result_body: Any | None = None) -> ApiQueueRequest:
        ts = now_ms()
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE api_requests
                SET status = ?, result_status = ?, result_body_json = ?, error = '',
                    lease_until_ms = 0, leased_by = '', updated_at_ms = ?
                WHERE request_id = ?
                """,
                (STATUS_DONE, int(result_status), _json_dumps(result_body), ts, request_id),
            )
            row = conn.execute("SELECT * FROM api_requests WHERE request_id = ?", (request_id,)).fetchone()
        if not row:
            raise KeyError(request_id)
        return _request_from_row(row)

    def fail_request(
        self,
        request_id: str,
        *,
        error: str,
        retry: bool = False,
        defer_ms: int = 0,
    ) -> ApiQueueRequest:
        ts = now_ms()
        status = STATUS_DEFERRED if retry and defer_ms > 0 else (STATUS_QUEUED if retry else STATUS_FAILED)
        earliest = ts + max(0, int(defer_ms)) if retry else 0
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE api_requests
                SET status = ?, earliest_ms = ?, error = ?, lease_until_ms = 0,
                    leased_by = '', updated_at_ms = ?
                WHERE request_id = ?
                """,
                (status, earliest, error, ts, request_id),
            )
            row = conn.execute("SELECT * FROM api_requests WHERE request_id = ?", (request_id,)).fetchone()
        if not row:
            raise KeyError(request_id)
        return _request_from_row(row)

    def get_request(self, request_id: str) -> ApiQueueRequest | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM api_requests WHERE request_id = ?", (request_id,)).fetchone()
        return _request_from_row(row) if row else None

    def summary(self) -> dict[str, Any]:
        with self._connection() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS count FROM api_requests GROUP BY status").fetchall()
            cooldowns = conn.execute(
                "SELECT scope, account, until_ms, reason FROM api_cooldowns WHERE until_ms > ? ORDER BY until_ms DESC",
                (now_ms(),),
            ).fetchall()
        return {
            "db_path": str(self.db_path),
            "counts": {str(row["status"]): int(row["count"]) for row in rows},
            "active_cooldowns": [
                {
                    "scope": str(row["scope"] or ""),
                    "account": str(row["account"] or ""),
                    "until_ms": int(row["until_ms"] or 0),
                    "reason": str(row["reason"] or ""),
                }
                for row in cooldowns
            ],
        }
