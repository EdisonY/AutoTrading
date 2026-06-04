"""Lightweight HTTP API for attention item management.

Provides REST endpoints for acknowledging/confirming attention items
from the portal UI. Uses Python's built-in http.server — no Flask/FastAPI needed.

Endpoints:
  GET  /api/attention        - List current attention items
  POST /api/attention/ack    - Acknowledge an item
  POST /api/attention/resolve - Resolve an item
  GET  /api/health           - Health check

Run on Aliyun as a systemd service.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

CST = timezone(timedelta(hours=8))
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name == "部署工具" else SCRIPT_DIR
EVENT_STORE_DB = ROOT / "runtime" / "event_store.sqlite3"
ATTENTION_JSON = ROOT / "research_memory" / "attention" / "open_items.json"
PORT = 8090


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def get_db() -> sqlite3.Connection:
    con = sqlite3.connect(str(EVENT_STORE_DB))
    con.row_factory = sqlite3.Row
    return con


def item_fingerprint(item: dict[str, Any]) -> str:
    text = "\n".join(
        str(item.get(key) or "")
        for key in ("item_id", "priority", "category", "title", "evidence", "source")
    )
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def ensure_attention_schema(con: sqlite3.Connection) -> None:
    con.execute(
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
    con.execute(
        """
        create table if not exists attention_acknowledgements (
            item_id text,
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
    existing = {
        str(row[1])
        for row in con.execute("pragma table_info(attention_acknowledgements)").fetchall()
    }
    for name in ("status", "fingerprint", "title", "priority", "category", "reason", "acknowledged_at", "payload_json"):
        if name not in existing:
            con.execute(f"alter table attention_acknowledgements add column {name} text")
    existing_items = {str(row[1]) for row in con.execute("pragma table_info(attention_items)").fetchall()}
    for name in ("acknowledged_at", "acknowledged_reason", "fingerprint", "payload_json"):
        if name not in existing_items:
            con.execute(f"alter table attention_items add column {name} text")
    con.commit()


def persist_acknowledgement(con: sqlite3.Connection, item: sqlite3.Row, status: str, user: str) -> None:
    now = now_iso()
    item_dict = dict(item)
    item_dict["status"] = status
    item_dict["acknowledged_at"] = now
    item_dict["acknowledged_reason"] = f"{user}:{status}"
    row = {
        "item_id": item_dict.get("item_id"),
        "status": status,
        "fingerprint": item_fingerprint(item_dict),
        "title": item_dict.get("title"),
        "priority": item_dict.get("priority"),
        "category": item_dict.get("category"),
        "reason": f"{user}:{status}",
        "acknowledged_at": now,
        "payload_json": json.dumps(item_dict, ensure_ascii=False, default=str),
    }
    con.execute("delete from attention_acknowledgements where item_id = ?", (row["item_id"],))
    con.execute(
        """
        insert into attention_acknowledgements (
            item_id, status, fingerprint, title, priority, category, reason, acknowledged_at, payload_json
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["item_id"],
            row["status"],
            row["fingerprint"],
            row["title"],
            row["priority"],
            row["category"],
            row["reason"],
            row["acknowledged_at"],
            row["payload_json"],
        ),
    )
    con.execute(
        "update attention_items set status = ?, acknowledged_at = ?, acknowledged_reason = ?, fingerprint = ?, payload_json = ? where item_id = ?",
        (
            status,
            row["acknowledged_at"],
            row["reason"],
            row["fingerprint"],
            row["payload_json"],
            row["item_id"],
        ),
    )


def load_attention_items() -> list[dict[str, Any]]:
    """Load attention items from SQLite or JSON fallback."""
    # Try SQLite first
    if EVENT_STORE_DB.exists():
        con = get_db()
        try:
            ensure_attention_schema(con)
            rows = con.execute(
                """SELECT item_id, priority, category, title, status, evidence,
                          recommended_action, first_seen, last_seen, last_confirmed_active
                   FROM attention_items
                   WHERE status IN ('open', 'cleared_pending_review')
                   ORDER BY
                     CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END,
                     last_seen DESC"""
            ).fetchall()
            if rows:
                return [dict(row) for row in rows]
        except Exception:
            pass
        finally:
            con.close()

    # Fallback to JSON file
    if ATTENTION_JSON.exists():
        try:
            payload = json.loads(ATTENTION_JSON.read_text(encoding="utf-8", errors="replace"))
            return [item for item in (payload.get("items") or [])
                    if isinstance(item, dict) and item.get("status") in {"open", "cleared_pending_review"}]
        except Exception:
            pass
    return []


def acknowledge_item(item_id: str, user: str = "portal") -> dict[str, Any]:
    """Acknowledge an attention item."""
    # Try SQLite
    if EVENT_STORE_DB.exists():
        con = get_db()
        try:
            ensure_attention_schema(con)
            item = con.execute(
                """SELECT item_id, priority, category, title, status, evidence,
                          recommended_action, first_seen, last_seen, last_confirmed_active, source
                   FROM attention_items WHERE item_id = ?""",
                (item_id,)
            ).fetchone()
            if item:
                persist_acknowledgement(con, item, "acknowledged", user)
                con.commit()
                return {"ok": True, "item_id": item_id, "action": "acknowledged"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            con.close()

    # Fallback: update JSON file
    return _update_json_item_status(item_id, "acknowledged")


def _update_json_item_status(item_id: str, new_status: str) -> dict[str, Any]:
    """Update item status in JSON file (fallback when SQLite unavailable)."""
    if not ATTENTION_JSON.exists():
        return {"ok": False, "error": "No attention JSON found"}
    try:
        payload = json.loads(ATTENTION_JSON.read_text(encoding="utf-8", errors="replace"))
        items = payload.get("items", [])
        found = False
        for item in items:
            if item.get("item_id") == item_id:
                item["status"] = new_status
                item["acknowledged_at"] = now_iso()
                item["acknowledged_reason"] = f"portal:{new_status}"
                found = True
                break
        if not found:
            return {"ok": False, "error": f"Item not found: {item_id}"}
        payload["generated_at"] = now_iso()
        ATTENTION_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return {"ok": True, "item_id": item_id, "action": new_status}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def resolve_item(item_id: str, user: str = "portal") -> dict[str, Any]:
    """Resolve an attention item."""
    # Try SQLite
    if EVENT_STORE_DB.exists():
        con = get_db()
        try:
            ensure_attention_schema(con)
            item = con.execute(
                """SELECT item_id, priority, category, title, status, evidence,
                          recommended_action, first_seen, last_seen, last_confirmed_active, source
                   FROM attention_items WHERE item_id = ?""",
                (item_id,)
            ).fetchone()
            if item:
                persist_acknowledgement(con, item, "resolved", user)
                con.commit()
                return {"ok": True, "item_id": item_id, "action": "resolved"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            con.close()

    # Fallback: update JSON file
    return _update_json_item_status(item_id, "resolved")


def export_attention_json() -> None:
    """Export attention items to JSON for portal sync."""
    items = load_attention_items()
    payload = {
        "generated_at": now_iso(),
        "summary": {
            "total_visible": len(items),
            "open": sum(1 for i in items if i.get("status") == "open"),
            "cleared_pending_review": sum(1 for i in items if i.get("status") == "cleared_pending_review"),
            "counts": {
                p: sum(1 for i in items if i.get("priority") == p)
                for p in ("P0", "P1", "P2", "P3")
            },
        },
        "items": items,
    }
    ATTENTION_JSON.parent.mkdir(parents=True, exist_ok=True)
    ATTENTION_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


class AttentionHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self._json_response({"ok": True, "ts": now_iso()})
        elif parsed.path == "/api/attention":
            items = load_attention_items()
            self._json_response({"ok": True, "items": items, "count": len(items)})
        else:
            self._json_response({"error": "Not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""

        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._json_response({"error": "Invalid JSON"}, 400)
            return

        item_id = data.get("item_id", "")
        user = data.get("user", "portal")

        if parsed.path == "/api/attention/ack":
            if not item_id:
                self._json_response({"error": "item_id required"}, 400)
                return
            result = acknowledge_item(item_id, user)
            export_attention_json()
            self._json_response(result)

        elif parsed.path == "/api/attention/resolve":
            if not item_id:
                self._json_response({"error": "item_id required"}, 400)
                return
            result = resolve_item(item_id, user)
            export_attention_json()
            self._json_response(result)

        else:
            self._json_response({"error": "Not found"}, 404)

    def _json_response(self, data: dict, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format, *args):
        print(f"[{now_iso()}] {format % args}", flush=True)


class AttentionHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Attention API Server")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--db", default=None, help="Path to event_store.sqlite3")
    args = parser.parse_args(argv)

    global EVENT_STORE_DB
    if args.db:
        EVENT_STORE_DB = Path(args.db)

    server = AttentionHTTPServer(("0.0.0.0", args.port), AttentionHandler)
    print(f"Attention API server listening on port {args.port}", flush=True)
    print(f"Database: {EVENT_STORE_DB}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Shutting down...", flush=True)
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
