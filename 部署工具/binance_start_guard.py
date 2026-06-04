"""Local systemd start guard for Binance-facing services.

Reads the central queue cooldown table only. It never calls Binance.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
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
DEFAULT_DB = ROOT / "runtime" / "binance_api_queue.sqlite3"


def now_ms() -> int:
    return int(time.time() * 1000)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Block Binance-facing service starts during queue cooldown")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--scope", default="any", help="Cooldown scope to check: any, public, signed, or another queue scope")
    parser.add_argument("--account", default="", help="Optional account/strategy key, e.g. A/v11")
    return parser.parse_args(argv)


def _cooldown_matches(row: sqlite3.Row, *, scope: str, account: str) -> bool:
    row_scope = str(row["scope"] or "")
    row_account = str(row["account"] or "")
    if row_scope == "global":
        return True
    if scope == "any":
        return True
    if row_scope != scope:
        return False
    if not account or not row_account:
        return True
    row_key = row_account.split("/", 1)[0].upper()
    account_key = account.split("/", 1)[0].upper()
    return row_account == account or row_key == account_key


def active_matching_cooldowns(db_path: Path, *, scope: str, account: str, at_ms: int | None = None) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    ts = now_ms() if at_ms is None else int(at_ms)
    conn = sqlite3.connect(str(db_path), timeout=5)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT scope, account, until_ms, reason
            FROM api_cooldowns
            WHERE until_ms > ?
            ORDER BY until_ms DESC
            """,
            (ts,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "scope": str(row["scope"] or ""),
            "account": str(row["account"] or ""),
            "until_ms": int(row["until_ms"] or 0),
            "reason": str(row["reason"] or ""),
        }
        for row in rows
        if _cooldown_matches(row, scope=scope, account=account)
    ]


def guard_status(db_path: Path, *, scope: str, account: str, at_ms: int | None = None) -> dict[str, Any]:
    try:
        cooldowns = active_matching_cooldowns(db_path, scope=scope, account=account, at_ms=at_ms)
    except sqlite3.OperationalError as exc:
        if "no such table: api_cooldowns" in str(exc).lower():
            cooldowns = []
        else:
            return {"allowed": False, "error": str(exc), "fail_closed": True, "db": str(db_path)}
    except Exception as exc:
        return {"allowed": False, "error": str(exc), "fail_closed": True, "db": str(db_path)}
    return {
        "allowed": not cooldowns,
        "scope": scope,
        "account": account,
        "db": str(db_path),
        "active_cooldowns": cooldowns,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = guard_status(Path(args.db), scope=str(args.scope or "any"), account=str(args.account or ""))
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if payload.get("allowed") else 75


if __name__ == "__main__":
    raise SystemExit(main())
