"""Central account-state service entrypoint.

Construction-mode step toward P0-A: one process owns account-state collection
and writes `runtime/account_state_latest.json` for scanners/replay/confirmers.
It can also mirror the legacy snapshot output while migration is in progress.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone, timedelta
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
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "部署工具"))
if (ROOT / "交易客户端").exists():
    sys.path.insert(0, str(ROOT / "交易客户端"))

from account_snapshot_service import (
    EVENT_STORE_DB,
    _snapshot_payload,
    collect_accounts_resilient,
    insert_account_snapshot,
    write_html,
    write_snapshot_error,
)
from core.account_state import build_account_state_payload, write_account_state


CST = timezone(timedelta(hours=8))


def collect_state_once(*, write_legacy_snapshot: bool = False, write_db: bool = False) -> dict[str, Any]:
    ts = datetime.now(CST)
    accounts, errors = collect_accounts_resilient()
    rows = [_snapshot_payload(account, ts) for account in accounts]
    status = "ok" if not errors else "partial"
    if rows and all(row.get("stale") for row in rows):
        status = "stale"
    payload = build_account_state_payload(rows, status=status, source="account_state_service", errors=errors)
    path = write_account_state(ROOT, payload)

    if write_legacy_snapshot:
        (ROOT / "runtime").mkdir(parents=True, exist_ok=True)
        (ROOT / "runtime" / "account_snapshot_latest.json").write_text(
            json.dumps({"summary": payload["summary"], "accounts": rows}, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        write_html(accounts)

    if write_db:
        for row in rows:
            if row.get("stale"):
                continue
            insert_account_snapshot(EVENT_STORE_DB, str(row["account"]), row)

    if errors:
        write_snapshot_error(RuntimeError("; ".join(errors)))

    result = {
        "status": status,
        "path": str(path),
        "summary": payload["summary"],
        "ts": payload["generated_at"],
    }
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Central Binance account-state service")
    parser.add_argument("--interval", type=int, default=900)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--write-legacy-snapshot", action="store_true")
    parser.add_argument("--write-db", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    while True:
        try:
            collect_state_once(write_legacy_snapshot=args.write_legacy_snapshot, write_db=args.write_db)
        except Exception as exc:
            write_snapshot_error(exc)
            print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), flush=True)
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(max(10, int(args.interval)))


if __name__ == "__main__":
    raise SystemExit(main())
