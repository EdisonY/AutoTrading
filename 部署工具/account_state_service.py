"""Central account-state service entrypoint.

Construction-mode step toward P0-A: one process owns account-state collection
and writes `runtime/account_state_latest.json` for scanners/replay/confirmers.
It can also mirror the legacy snapshot output while migration is in progress.
"""

from __future__ import annotations

import argparse
import hashlib
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
    ACCOUNTS,
    EVENT_STORE_DB,
    _snapshot_payload,
    collect_accounts_resilient,
    insert_account_snapshot,
    write_html,
    write_snapshot_error,
)
from core.account_state import build_account_state_payload, read_account_state_payload, write_account_state
from core.account_state_stream import apply_user_stream_event
from core.account_state import atomic_write_json, utc_now_iso


CST = timezone(timedelta(hours=8))
STREAM_OFFSET_FILENAME = "account_state_stream_offsets.json"


def bootstrap_empty_state(*, root: str | Path = ROOT) -> dict[str, Any]:
    """Write stale placeholder rows without calling Binance.

    This is for post-reset zero-run bootstrapping. User-stream events may turn a
    row fresh later, but an idle websocket must not make this placeholder look
    like a verified exchange snapshot.
    """
    now = utc_now_iso()
    rows = []
    errors = []
    for key, version, desc, _module_name, _class_name, _hard in ACCOUNTS:
        strategy = f"{key}/{version}"
        reason = f"{strategy} bootstrap_empty_no_signed_rest_waiting_for_user_stream"
        errors.append(reason)
        rows.append(
            {
                "ts": now,
                "account": key,
                "strategy": strategy,
                "version": version,
                "desc": desc,
                "stale": True,
                "snapshot_error": reason,
                "wallet_usdt": 0.0,
                "available_usdt": 0.0,
                "margin_usdt": 0.0,
                "unrealized_pnl_usdt": 0.0,
                "open_positions": 0,
                "longs": 0,
                "shorts": 0,
                "notional_usdt": 0.0,
                "used_margin_usdt": 0.0,
                "hard_stop_risk_count": 0,
                "positions": [],
            }
        )
    payload = build_account_state_payload(rows, status="stale", source="bootstrap_empty_no_signed_rest", errors=errors)
    path = write_account_state(root, payload)
    result = {"status": "stale", "path": str(path), "summary": payload["summary"], "ts": payload["generated_at"]}
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return payload


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


def apply_stream_events_once(
    *,
    events_path: str | Path,
    strategy: str,
    root: str | Path = ROOT,
    offset_state_path: str | Path | None = None,
) -> dict[str, Any]:
    payload = read_account_state_payload(root, allow_legacy=False)
    if not payload:
        raise RuntimeError("central account state missing; run a baseline collection first")

    state_path = Path(offset_state_path) if offset_state_path else Path(root) / "runtime" / STREAM_OFFSET_FILENAME
    offset_state = _load_stream_offset_state(state_path)
    applied = 0
    skipped_duplicate = 0
    path = Path(events_path)
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        item = json.loads(raw)
        if not isinstance(item, dict):
            continue
        event_strategy = str(item.get("strategy") or strategy)
        event = item.get("event") if isinstance(item.get("event"), dict) else item
        event_id = _stream_event_id(event_strategy, event)
        if event_id in set(offset_state.get("seen_event_ids") or []):
            skipped_duplicate += 1
            continue
        payload = apply_user_stream_event(payload, strategy=event_strategy, event=event)
        _record_stream_event(offset_state, event_strategy, event_id, event)
        applied += 1
    output = write_account_state(root, payload)
    _write_stream_offset_state(state_path, offset_state)
    result = {
        "status": "ok",
        "path": str(output),
        "stream_events_applied": applied,
        "stream_events_skipped_duplicate": skipped_duplicate,
        "summary": payload.get("summary") or {},
        "ts": payload.get("generated_at"),
    }
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return payload


def _load_stream_offset_state(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"schema_version": 1, "generated_at": utc_now_iso(), "seen_event_ids": [], "strategies": {}}
    if not isinstance(payload, dict):
        return {"schema_version": 1, "generated_at": utc_now_iso(), "seen_event_ids": [], "strategies": {}}
    if not isinstance(payload.get("seen_event_ids"), list):
        payload["seen_event_ids"] = []
    if not isinstance(payload.get("strategies"), dict):
        payload["strategies"] = {}
    return payload


def _write_stream_offset_state(path: Path, payload: dict[str, Any]) -> None:
    out = dict(payload)
    out["schema_version"] = 1
    out["generated_at"] = utc_now_iso()
    out["seen_event_ids"] = list(out.get("seen_event_ids") or [])[-5000:]
    atomic_write_json(path, out)


def _stream_event_id(strategy: str, event: dict[str, Any]) -> str:
    event_type = str(event.get("e") or event.get("event_type") or "")
    event_time = str(event.get("E") or event.get("T") or event.get("event_time") or "")
    transaction_time = str(event.get("T") or event.get("transaction_time") or "")
    update_time = str(event.get("u") or event.get("update_id") or "")
    if event_type or event_time or transaction_time or update_time:
        return f"{strategy}:{event_type}:{event_time}:{transaction_time}:{update_time}"
    stable = json.dumps(event, ensure_ascii=False, sort_keys=True, default=str)
    return f"{strategy}:hash:{hashlib.sha256(stable.encode('utf-8')).hexdigest()}"


def _record_stream_event(state: dict[str, Any], strategy: str, event_id: str, event: dict[str, Any]) -> None:
    seen = list(state.get("seen_event_ids") or [])
    seen.append(event_id)
    state["seen_event_ids"] = seen[-5000:]
    strategies = state.setdefault("strategies", {})
    row = strategies.setdefault(strategy, {})
    row["last_event_id"] = event_id
    row["last_event_type"] = str(event.get("e") or event.get("event_type") or "")
    row["last_event_time"] = event.get("E") or event.get("event_time") or ""
    row["updated_at"] = utc_now_iso()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Central Binance account-state service")
    parser.add_argument("--interval", type=int, default=900)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--write-legacy-snapshot", action="store_true")
    parser.add_argument("--write-db", action="store_true")
    parser.add_argument("--bootstrap-empty", action="store_true", help="Write stale zero placeholders without signed REST")
    parser.add_argument("--stream-events", help="Apply newline-delimited user-data-stream events to central account state")
    parser.add_argument("--stream-strategy", default="", help="Default strategy for raw stream events")
    parser.add_argument("--stream-offset-state", default="", help="Offset/dedup state path for stream events")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.bootstrap_empty:
        bootstrap_empty_state()
        return 0
    if args.stream_events:
        if not args.stream_strategy:
            print(json.dumps({"status": "error", "error": "--stream-strategy is required with --stream-events"}, ensure_ascii=False), flush=True)
            return 2
        try:
            apply_stream_events_once(
                events_path=args.stream_events,
                strategy=args.stream_strategy,
                offset_state_path=args.stream_offset_state or None,
            )
            return 0
        except Exception as exc:
            write_snapshot_error(exc)
            print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), flush=True)
            return 1
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
