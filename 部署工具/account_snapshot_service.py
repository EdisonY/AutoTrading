"""Collect account snapshots, write SQLite rows, and refresh the HTML view."""

from __future__ import annotations

import argparse
import json
import os
import re
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

from account_snapshot_html import build_html, collect
from core.audit_log import write_jsonl_with_daily_shard
from core.event_store import insert_account_snapshot


CST = timezone(timedelta(hours=8))
EVENT_STORE_DB = ROOT / "runtime" / "event_store.sqlite3"
REPORT_DIR = ROOT / "复盘报告"
LOG_PATH = ROOT / "logs" / "account_snapshots.jsonl"
ERROR_PATH = ROOT / "runtime" / "account_snapshot_error_latest.json"
ERROR_LOG_PATH = ROOT / "logs" / "account_snapshot_errors.jsonl"
A_V11_TARGET_MARGIN_USDT = 100.0
A_V11_MARGIN_TOLERANCE_PCT = 0.05
BAN_UNTIL_RE = re.compile(r"banned until\s+(\d{12,})", re.IGNORECASE)
BAN_RESUME_PADDING_SECONDS = 5 * 60


def _sizing_violations(account: dict[str, Any], positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if account.get("key") != "A":
        return []
    low = A_V11_TARGET_MARGIN_USDT * (1 - A_V11_MARGIN_TOLERANCE_PCT)
    high = A_V11_TARGET_MARGIN_USDT * (1 + A_V11_MARGIN_TOLERANCE_PCT)
    violations = []
    for pos in positions:
        qty = abs(float(pos.get("qty") or 0))
        entry = float(pos.get("entry") or 0)
        leverage = float(pos.get("lev") or 0)
        current_margin = float(pos.get("margin") or 0)
        initial_margin = (qty * entry / leverage) if qty > 0 and entry > 0 and leverage > 0 else current_margin
        if initial_margin <= 0 or low <= initial_margin <= high:
            continue
        violations.append({
            "symbol": pos.get("symbol"),
            "side": pos.get("side"),
            "qty": qty,
            "margin": initial_margin,
            "current_margin": current_margin,
            "notional": float(pos.get("notional") or 0),
            "target_margin": A_V11_TARGET_MARGIN_USDT,
            "deviation_pct": ((initial_margin - A_V11_TARGET_MARGIN_USDT) / A_V11_TARGET_MARGIN_USDT * 100),
        })
    return violations


def _snapshot_payload(account: dict[str, Any], ts: datetime) -> dict[str, Any]:
    positions = account.get("positions") or []
    sizing_violations = _sizing_violations(account, positions)
    worst = min(positions, key=lambda p: float(p.get("upnl") or 0), default={})
    best = max(positions, key=lambda p: float(p.get("upnl") or 0), default={})
    return {
        "ts": ts.isoformat(),
        "account": account.get("key"),
        "strategy": f"{account.get('key')}/{account.get('version')}",
        "version": account.get("version"),
        "desc": account.get("desc"),
        "wallet_usdt": float(account.get("wallet") or 0),
        "available_usdt": float(account.get("available") or 0),
        "margin_usdt": float(account.get("margin") or 0),
        "unrealized_pnl_usdt": float(account.get("upnl") or 0),
        "open_positions": len(positions),
        "longs": int(account.get("longs") or 0),
        "shorts": int(account.get("shorts") or 0),
        "notional_usdt": float(account.get("notional") or 0),
        "used_margin_usdt": float(account.get("used_margin") or 0),
        "hard_stop_risk_count": int(account.get("over_hard") or 0),
        "hard_stop_pct": float(account.get("hard") or 0),
        "sizing_violation_count": len(sizing_violations),
        "sizing_violations": sizing_violations,
        "best_position": best,
        "worst_position": worst,
        "positions": positions,
    }


def write_html(accounts: list[dict[str, Any]]) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    html_text = build_html(accounts)
    (REPORT_DIR / "account_snapshot_latest.html").write_text(html_text, encoding="utf-8")


def collect_once() -> list[dict[str, Any]]:
    ts = datetime.now(CST)
    accounts = collect()
    write_html(accounts)
    rows = [_snapshot_payload(account, ts) for account in accounts]
    for row in rows:
        insert_account_snapshot(EVENT_STORE_DB, str(row["account"]), row)
        if os.environ.get("ACCOUNT_SNAPSHOT_JSONL_ENABLED", "0").strip().lower() in {"1", "true", "yes"}:
            write_jsonl_with_daily_shard(LOG_PATH, row)
    summary = {
        "ts": ts.isoformat(),
        "accounts": len(rows),
        "wallet_usdt": round(sum(float(r["wallet_usdt"]) for r in rows), 4),
        "available_usdt": round(sum(float(r["available_usdt"]) for r in rows), 4),
        "margin_usdt": round(sum(float(r["margin_usdt"]) for r in rows), 4),
        "unrealized_pnl_usdt": round(sum(float(r["unrealized_pnl_usdt"]) for r in rows), 4),
        "open_positions": sum(int(r["open_positions"]) for r in rows),
    }
    (ROOT / "runtime").mkdir(parents=True, exist_ok=True)
    (ROOT / "runtime" / "account_snapshot_latest.json").write_text(
        json.dumps({"summary": summary, "accounts": rows}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    if ERROR_PATH.exists():
        ERROR_PATH.unlink()
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return rows


def retry_at_from_error(text: str) -> datetime | None:
    match = BAN_UNTIL_RE.search(text or "")
    if not match:
        return None
    try:
        raw_ms = int(match.group(1))
    except ValueError:
        return None
    return datetime.fromtimestamp(raw_ms / 1000, CST) + timedelta(seconds=BAN_RESUME_PADDING_SECONDS)


def retry_delay_from_error_file(now: datetime) -> float:
    if not ERROR_PATH.exists():
        return 0.0
    try:
        payload = json.loads(ERROR_PATH.read_text(encoding="utf-8", errors="replace"))
        retry_raw = payload.get("retry_at")
        retry_at = (
            datetime.fromisoformat(str(retry_raw).replace("Z", "+00:00"))
            if retry_raw
            else retry_at_from_error(str(payload.get("error") or ""))
        )
        if not retry_at:
            return 0.0
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=CST)
        return max(0.0, (retry_at.astimezone(CST) - now).total_seconds())
    except Exception:
        return 0.0


def write_snapshot_error(exc: Exception) -> dict[str, Any]:
    now = datetime.now(CST)
    retry_at = retry_at_from_error(str(exc))
    payload = {
        "ts": now.isoformat(),
        "status": "error",
        "error": str(exc),
    }
    if retry_at:
        payload["retry_at"] = retry_at.isoformat()
        payload["retry_after_seconds"] = max(0, int((retry_at - now).total_seconds()))
    ERROR_PATH.parent.mkdir(parents=True, exist_ok=True)
    ERROR_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    ERROR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_jsonl_with_daily_shard(ERROR_LOG_PATH, payload)
    return payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="实时账户快照入库服务")
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--once", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    while True:
        retry_delay = retry_delay_from_error_file(datetime.now(CST))
        if retry_delay > 0:
            payload = {
                "status": "cooldown",
                "sleep_seconds": int(retry_delay),
                "ts": datetime.now(CST).isoformat(),
            }
            print(json.dumps(payload, ensure_ascii=False), flush=True)
            if args.once:
                return 2
            time.sleep(retry_delay)
            continue
        try:
            collect_once()
        except Exception as exc:
            error_payload = write_snapshot_error(exc)
            print(json.dumps({"status": "error", "error": str(exc), "ts": datetime.now(CST).isoformat()}, ensure_ascii=False), flush=True)
            if error_payload.get("retry_after_seconds") and not args.once:
                time.sleep(max(10, int(error_payload["retry_after_seconds"])))
                continue
        if args.once:
            return 0
        time.sleep(max(10, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
