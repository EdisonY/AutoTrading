from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1


DDL = [
    """
    create table if not exists meta (
        key text primary key,
        value text not null
    )
    """,
    """
    create table if not exists events (
        id integer primary key autoincrement,
        ts text not null,
        strategy text not null default '',
        symbol text not null default '',
        event_type text not null,
        category text not null default '',
        side text not null default '',
        score real,
        stage text not null default '',
        layer text not null default '',
        reason text not null default '',
        source text not null default '',
        payload_json text not null
    )
    """,
    "create index if not exists idx_events_ts on events(ts)",
    "create index if not exists idx_events_strategy_ts on events(strategy, ts)",
    "create index if not exists idx_events_symbol_ts on events(symbol, ts)",
    "create index if not exists idx_events_type_category on events(event_type, category)",
    """
    create table if not exists account_snapshots (
        id integer primary key autoincrement,
        ts text not null,
        account text not null,
        wallet_usdt real,
        margin_usdt real,
        available_usdt real,
        unrealized_pnl_usdt real,
        open_positions integer not null default 0,
        payload_json text not null
    )
    """,
    "create index if not exists idx_account_snapshots_account_ts on account_snapshots(account, ts)",
    """
    create table if not exists baseline_runs (
        id integer primary key autoincrement,
        ts text not null,
        host text not null default '',
        payload_json text not null
    )
    """,
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        for stmt in DDL:
            conn.execute(stmt)
        conn.execute(
            "insert or replace into meta(key, value) values(?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )
        conn.commit()


@contextmanager
def connect(path: Path):
    init_db(path)
    conn = sqlite3.connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def normalize_event(raw: dict[str, Any], *, source: str = "") -> dict[str, Any]:
    event_type = str(raw.get("event") or raw.get("status") or raw.get("type") or "EVENT").upper()
    reason = str(raw.get("reason") or raw.get("skip_reason") or raw.get("msg") or "")
    score = raw.get("score", raw.get("raw_score", raw.get("net_score")))
    try:
        score_value = float(score) if score not in (None, "") else None
    except Exception:
        score_value = None
    strategy = str(raw.get("strategy") or "")
    if source.startswith("A/v11/"):
        strategy = "A/v11"
    elif source.startswith("B/v16/"):
        strategy = "B/v16"
    elif source.startswith("C/v14/"):
        strategy = "C/v14"
    return {
        "ts": str(raw.get("time") or raw.get("ts") or raw.get("timestamp") or utc_now_iso()),
        "strategy": strategy,
        "symbol": str(raw.get("symbol") or ""),
        "event_type": event_type,
        "category": str(raw.get("category") or ""),
        "side": str(raw.get("side") or raw.get("trade_side") or ""),
        "score": score_value,
        "stage": str(raw.get("decision_stage") or raw.get("stage") or ""),
        "layer": str(raw.get("filter_layer") or raw.get("layer") or ""),
        "reason": reason,
        "source": source,
        "payload_json": json_dumps(raw),
    }


def insert_events(path: Path, events: Iterable[dict[str, Any]], *, source: str = "") -> int:
    rows = [normalize_event(event, source=source) for event in events]
    if not rows:
        return 0
    with connect(path) as conn:
        conn.executemany(
            """
            insert into events(
                ts, strategy, symbol, event_type, category, side, score,
                stage, layer, reason, source, payload_json
            ) values(
                :ts, :strategy, :symbol, :event_type, :category, :side, :score,
                :stage, :layer, :reason, :source, :payload_json
            )
            """,
            rows,
        )
    return len(rows)


def insert_baseline(path: Path, payload: dict[str, Any], *, host: str = "") -> None:
    with connect(path) as conn:
        conn.execute(
            "insert into baseline_runs(ts, host, payload_json) values(?, ?, ?)",
            (utc_now_iso(), host, json_dumps(payload)),
        )


def insert_account_snapshot(path: Path, account: str, snapshot: dict[str, Any]) -> None:
    with connect(path) as conn:
        conn.execute(
            """
            insert into account_snapshots(
                ts, account, wallet_usdt, margin_usdt, available_usdt,
                unrealized_pnl_usdt, open_positions, payload_json
            ) values(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(snapshot.get("ts") or utc_now_iso()),
                account,
                snapshot.get("wallet_usdt"),
                snapshot.get("margin_usdt"),
                snapshot.get("available_usdt"),
                snapshot.get("unrealized_pnl_usdt"),
                int(snapshot.get("open_positions") or 0),
                json_dumps(snapshot),
            ),
        )


class EventStoreWriter:
    def __init__(self, path: Path | None = None, *, enabled: bool | None = None):
        env_enabled = os.environ.get("EVENT_STORE_ENABLED", "1").strip().lower() not in {"0", "false", "no"}
        self.enabled = env_enabled if enabled is None else enabled
        self.path = Path(path or os.environ.get("EVENT_STORE_PATH", "runtime/event_store.sqlite3"))
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._failed = False
        self._failure_count = 0

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            init_db(self.path)
            self._conn = sqlite3.connect(self.path, timeout=2.0)
            self._conn.execute("pragma journal_mode=wal")
            self._conn.execute("pragma synchronous=normal")
        return self._conn

    def write_event(self, raw: dict[str, Any], *, source: str = "") -> bool:
        if not self.enabled or self._failed:
            return False
        try:
            row = normalize_event(raw, source=source)
            with self._lock:
                conn = self._connection()
                conn.execute(
                    """
                    insert into events(
                        ts, strategy, symbol, event_type, category, side, score,
                        stage, layer, reason, source, payload_json
                    ) values(
                        :ts, :strategy, :symbol, :event_type, :category, :side, :score,
                        :stage, :layer, :reason, :source, :payload_json
                    )
                    """,
                    row,
                )
                conn.commit()
            return True
        except Exception:
            self._failed = True
            self._failure_count += 1
            if self._failure_count < 10:
                self._failed = False
                self.close()
            return False
