"""Acknowledge durable attention items so resolved issues stay archived."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name == "部署工具" else SCRIPT_DIR
ATTENTION_DIR = ROOT / "research_memory" / "attention"
ATTENTION_JSON = ATTENTION_DIR / "open_items.json"
ACK_JSONL = ATTENTION_DIR / "acknowledgements.jsonl"
EVENT_STORE_DB = ROOT / "runtime" / "event_store.sqlite3"
CST = timezone(timedelta(hours=8))


def read_json(path: Path) -> Any:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def item_fingerprint(item: dict[str, Any]) -> str:
    text = "\n".join(
        str(item.get(key) or "")
        for key in ("item_id", "priority", "category", "title", "evidence", "source")
    )
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        create table if not exists attention_acknowledgements (
            item_id text primary key,
            status text,
            fingerprint text,
            title text,
            priority text,
            category text,
            reason text,
            acknowledged_at text,
            payload_json text
        )
        """
    )
    conn.execute(
        """
        create table if not exists attention_items (
            item_id text primary key,
            priority text,
            category text,
            title text,
            status text,
            first_seen text,
            last_seen text,
            last_confirmed_active text,
            cleared_at text,
            acknowledged_at text,
            acknowledged_reason text,
            evidence text,
            recommended_action text,
            source text,
            fingerprint text,
            payload_json text
        )
        """
    )
    conn.commit()


def persist_db(records: list[dict[str, Any]], items: list[dict[str, Any]]) -> None:
    EVENT_STORE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(EVENT_STORE_DB)
    try:
        ensure_tables(conn)
        for row in records:
            conn.execute(
                """
                insert into attention_acknowledgements (
                    item_id, status, fingerprint, title, priority, category, reason, acknowledged_at, payload_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(item_id) do update set
                    status=excluded.status,
                    fingerprint=excluded.fingerprint,
                    title=excluded.title,
                    priority=excluded.priority,
                    category=excluded.category,
                    reason=excluded.reason,
                    acknowledged_at=excluded.acknowledged_at,
                    payload_json=excluded.payload_json
                """,
                (
                    row.get("item_id"),
                    row.get("status"),
                    row.get("fingerprint"),
                    row.get("title"),
                    row.get("priority"),
                    row.get("category"),
                    row.get("reason"),
                    row.get("acknowledged_at"),
                    json.dumps(row, ensure_ascii=False, default=str),
                ),
            )
        for item in items:
            if not item.get("item_id"):
                continue
            conn.execute(
                """
                insert into attention_items (
                    item_id, priority, category, title, status, first_seen, last_seen,
                    last_confirmed_active, cleared_at, acknowledged_at, acknowledged_reason,
                    evidence, recommended_action, source, fingerprint, payload_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(item_id) do update set
                    status=excluded.status,
                    acknowledged_at=excluded.acknowledged_at,
                    acknowledged_reason=excluded.acknowledged_reason,
                    fingerprint=excluded.fingerprint,
                    payload_json=excluded.payload_json
                """,
                (
                    item.get("item_id"),
                    item.get("priority"),
                    item.get("category"),
                    item.get("title"),
                    item.get("status"),
                    item.get("first_seen"),
                    item.get("last_seen"),
                    item.get("last_confirmed_active"),
                    item.get("cleared_at"),
                    item.get("acknowledged_at"),
                    item.get("acknowledged_reason"),
                    item.get("evidence"),
                    item.get("recommended_action"),
                    item.get("source"),
                    item_fingerprint(item),
                    json.dumps(item, ensure_ascii=False, default=str),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="确认并归档持久关注事项")
    parser.add_argument("--item-id", action="append", required=True, help="要确认的 item_id，可重复")
    parser.add_argument("--reason", default="用户确认归档")
    parser.add_argument("--status", default="archived", choices=["acknowledged", "archived", "resolved"])
    parser.add_argument("--attention-json", default=str(ATTENTION_JSON))
    args = parser.parse_args(argv)

    payload = read_json(Path(args.attention_json))
    if not isinstance(payload, dict):
        raise SystemExit(f"attention payload missing: {args.attention_json}")
    items = payload.get("items") or []
    by_id = {str(item.get("item_id") or ""): item for item in items if isinstance(item, dict)}
    now = datetime.now(CST).isoformat(timespec="seconds")
    existing = read_jsonl(ACK_JSONL)
    existing_by_id = {str(row.get("item_id") or ""): row for row in existing if row.get("item_id")}
    updated = 0
    ack_records: list[dict[str, Any]] = []
    for item_id in args.item_id:
        item = by_id.get(item_id)
        if not item:
            print(f"skip missing item: {item_id}", file=sys.stderr)
            continue
        row = {
            "item_id": item_id,
            "status": args.status,
            "fingerprint": item_fingerprint(item),
            "title": item.get("title"),
            "priority": item.get("priority"),
            "category": item.get("category"),
            "reason": args.reason,
            "acknowledged_at": now,
        }
        existing_by_id[item_id] = row
        ack_records.append(row)
        item["status"] = args.status
        item["acknowledged_at"] = now
        item["acknowledged_reason"] = args.reason
        updated += 1
    payload["summary"] = {
        "total_visible": sum(1 for item in items if item.get("status") in {"open", "cleared_pending_review"}),
        "open": sum(1 for item in items if item.get("status") == "open"),
        "cleared_pending_review": sum(1 for item in items if item.get("status") == "cleared_pending_review"),
        "counts": {
            key: sum(
                1 for item in items
                if item.get("status") in {"open", "cleared_pending_review"} and item.get("priority") == key
            )
            for key in ("P0", "P1", "P2", "P3")
        },
    }
    Path(args.attention_json).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_jsonl(ACK_JSONL, sorted(existing_by_id.values(), key=lambda row: str(row.get("item_id") or "")))
    persist_db(ack_records, items)
    print(json.dumps({"updated": updated, "acknowledgements": str(ACK_JSONL), "db": str(EVENT_STORE_DB)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
