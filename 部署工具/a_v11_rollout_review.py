"""Review A/v11 approved trailing-pullback rollout windows.

This is read-only. It summarizes post-approval A/v11 live results so the
operator can decide whether to keep observing, narrow parameters, or rollback.
It does not change strategy settings or orders.
"""

from __future__ import annotations

import argparse
import collections
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
CST = timezone(timedelta(hours=8))
WINDOWS_HOURS = (24, 72, 168)
DEFAULT_APPROVED_AT = "2026-05-29T11:59:40+08:00"
FEE_SLIPPAGE_PCT = 0.15
NOTIONAL_PER_TRADE = 400.0
ROLLBACK_REVIEW_LOSS_USDT = 80.0


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST)
        return dt.astimezone(CST)
    except Exception:
        return None


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def payload_float(payload: dict[str, Any], *keys: str) -> float:
    raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
    for key in keys:
        if key in payload:
            return to_float(payload.get(key))
        if key in raw:
            return to_float(raw.get(key))
    return 0.0


def payload_text(payload: dict[str, Any], *keys: str) -> str:
    raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
        value = raw.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def find_db(explicit: str = "") -> Path | None:
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    candidates.extend(
        [
            ROOT / "server_logs_tencent" / "runtime" / "event_store.sqlite3",
            ROOT / "runtime" / "event_store.sqlite3",
        ]
    )
    for path in candidates:
        if not path.exists():
            continue
        try:
            with sqlite3.connect(path) as con:
                ok = con.execute("select 1 from sqlite_master where type='table' and name='events'").fetchone()
            if ok:
                return path
        except Exception:
            continue
    return None


def load_approval() -> dict[str, Any]:
    path = ROOT / "research_memory" / "approvals" / "approve_full_live_A_v11_trailing_pullback_2026-05-29.json"
    payload = read_json(path)
    if isinstance(payload, dict):
        return payload
    return {
        "candidate_ids": [
            "EXP-20260527-v11-trailing-pullback-0p8",
            "EXP-20260527-v11-trailing-pullback-1p0",
        ],
        "approved_at": DEFAULT_APPROVED_AT,
        "selected_live_parameter": {"15m_pullback_atr": 1.0, "30m_pullback_atr": 0.8},
    }


def query_rows(db: Path, start: datetime) -> list[sqlite3.Row]:
    with sqlite3.connect(db) as con:
        con.row_factory = sqlite3.Row
        return list(
            con.execute(
                """
                select id, ts, strategy, symbol, event_type, category, side, reason, payload_json
                from events
                where strategy = 'A/v11'
                  and ts >= ?
                  and event_type in ('OPEN','CLOSE','FORCED_CLOSE','OPEN_FAILED','OPEN_SKIPPED','CLOSE_FAILED','FORCED_CLOSE_FAILED','SIGNAL')
                order by ts asc, id asc
                """,
                (start.strftime("%Y-%m-%d"),),
            )
        )


def summarize_window(rows: list[sqlite3.Row], start: datetime, end: datetime) -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "since": start.isoformat(timespec="seconds"),
        "until": end.isoformat(timespec="seconds"),
        "events": 0,
        "signals": 0,
        "opens": 0,
        "closes": 0,
        "forced_closes": 0,
        "open_failed": 0,
        "open_skipped": 0,
        "close_failed": 0,
        "realized_pnl_usdt": 0.0,
        "forced_close_pnl_usdt": 0.0,
        "top_losers": [],
        "top_winners": [],
        "close_reasons": [],
        "side_pnl": {},
        "timeframe_pnl": {},
    }
    trades: list[dict[str, Any]] = []
    reason_counter: collections.Counter[str] = collections.Counter()
    side_pnl: collections.defaultdict[str, float] = collections.defaultdict(float)
    tf_pnl: collections.defaultdict[str, float] = collections.defaultdict(float)
    for row in rows:
        event_dt = parse_dt(row["ts"])
        if not event_dt or event_dt < start or event_dt > end:
            continue
        event_type = str(row["event_type"] or "")
        metrics["events"] += 1
        if event_type == "SIGNAL":
            metrics["signals"] += 1
        elif event_type == "OPEN":
            metrics["opens"] += 1
        elif event_type == "OPEN_FAILED":
            metrics["open_failed"] += 1
        elif event_type == "OPEN_SKIPPED":
            metrics["open_skipped"] += 1
        elif event_type in {"CLOSE_FAILED", "FORCED_CLOSE_FAILED"}:
            metrics["close_failed"] += 1
        elif event_type in {"CLOSE", "FORCED_CLOSE"}:
            if event_type == "CLOSE":
                metrics["closes"] += 1
            else:
                metrics["forced_closes"] += 1
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except Exception:
                payload = {}
            pnl = payload_float(payload, "pnl_usd", "pnl_usdt", "realized_pnl_usdt", "pnl")
            reason = payload_text(payload, "reason", "close_reason") or str(row["reason"] or "-")
            timeframe = payload_text(payload, "timeframe", "tf") or "-"
            side = str(row["side"] or payload_text(payload, "side") or "-").lower()
            metrics["realized_pnl_usdt"] += pnl
            if event_type == "FORCED_CLOSE":
                metrics["forced_close_pnl_usdt"] += pnl
            reason_counter[reason] += 1
            side_pnl[side] += pnl
            tf_pnl[timeframe] += pnl
            trades.append(
                {
                    "ts": row["ts"],
                    "symbol": row["symbol"],
                    "side": side,
                    "timeframe": timeframe,
                    "event_type": event_type,
                    "reason": reason,
                    "pnl_usdt": round(pnl, 4),
                }
            )
    closed = int(metrics["closes"]) + int(metrics["forced_closes"])
    cost = closed * NOTIONAL_PER_TRADE * FEE_SLIPPAGE_PCT / 100.0
    metrics["estimated_cost_usdt"] = round(cost, 4)
    metrics["realized_pnl_usdt"] = round(float(metrics["realized_pnl_usdt"]), 4)
    metrics["forced_close_pnl_usdt"] = round(float(metrics["forced_close_pnl_usdt"]), 4)
    metrics["pnl_after_cost_usdt"] = round(float(metrics["realized_pnl_usdt"]) - cost, 4)
    metrics["closed_samples"] = closed
    metrics["forced_close_rate"] = round(float(metrics["forced_closes"]) / max(1, closed), 4)
    metrics["open_failed_rate"] = round(float(metrics["open_failed"]) / max(1, int(metrics["opens"]) + int(metrics["open_failed"])), 4)
    metrics["top_losers"] = sorted(trades, key=lambda item: float(item["pnl_usdt"]))[:8]
    metrics["top_winners"] = sorted(trades, key=lambda item: float(item["pnl_usdt"]), reverse=True)[:5]
    metrics["close_reasons"] = [{"reason": k, "count": v} for k, v in reason_counter.most_common(8)]
    metrics["side_pnl"] = {k: round(v, 4) for k, v in sorted(side_pnl.items())}
    metrics["timeframe_pnl"] = {k: round(v, 4) for k, v in sorted(tf_pnl.items())}
    return metrics


def verdict(windows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    day = windows.get("24h", {})
    three = windows.get("72h", {})
    week = windows.get("168h", {})
    actions: list[str] = []
    label = "observe"
    priority = "P2"
    if float(three.get("pnl_after_cost_usdt") or 0) <= -ROLLBACK_REVIEW_LOSS_USDT:
        label = "manual_review_required"
        priority = "P1"
        actions.append("复核 A/v11 trailing-pullback 是否继续保留当前 live 参数。")
    if float(week.get("pnl_after_cost_usdt") or 0) <= -ROLLBACK_REVIEW_LOSS_USDT:
        actions.append("若 168h 窗口继续恶化，准备人工回滚到上一版 trailing 参数。")
    if float(day.get("pnl_after_cost_usdt") or 0) >= 0 and priority != "P1":
        label = "continue_observation"
        actions.append("24h 窗口未显示亏损压力，继续观察。")
    if not actions:
        actions.append("继续收集样本，不改实盘阈值。")
    return {
        "priority": priority,
        "status": label,
        "recommended_actions": actions,
    }


def build_payload(db: Path, approval: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(CST)
    approved_at = parse_dt(approval.get("approved_at") or approval.get("applied_at")) or parse_dt(DEFAULT_APPROVED_AT) or now
    rows = query_rows(db, approved_at - timedelta(hours=1))
    windows: dict[str, dict[str, Any]] = {}
    for hours in WINDOWS_HOURS:
        start = max(approved_at, now - timedelta(hours=hours))
        windows[f"{hours}h"] = summarize_window(rows, start, now)
    decision = verdict(windows)
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "strategy": "A/v11",
        "db": str(db),
        "approved_at": approved_at.isoformat(timespec="seconds"),
        "candidate_ids": approval.get("candidate_ids") or [],
        "selected_live_parameter": approval.get("selected_live_parameter") or {},
        "decision": decision,
        "windows": windows,
    }


def render_md(payload: dict[str, Any]) -> str:
    decision = payload.get("decision") or {}
    lines = [
        "# A/v11 Trailing Rollout Review",
        "",
        f"- Generated: `{payload.get('generated_at')}`",
        f"- Approved at: `{payload.get('approved_at')}`",
        f"- Candidates: `{', '.join(payload.get('candidate_ids') or [])}`",
        f"- Live parameter: `{json.dumps(payload.get('selected_live_parameter') or {}, ensure_ascii=False)}`",
        f"- Status: `{decision.get('priority')}/{decision.get('status')}`",
        "",
        "## Actions",
    ]
    lines.extend(f"- {item}" for item in decision.get("recommended_actions") or [])
    lines.extend(
        [
            "",
            "## Windows",
            "",
            "| Window | Opens | Closed | Forced | Open failed | PnL | Cost | After cost | Forced rate |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for name, row in (payload.get("windows") or {}).items():
        lines.append(
            f"| {name} | {int(row.get('opens') or 0)} | {int(row.get('closed_samples') or 0)} | "
            f"{int(row.get('forced_closes') or 0)} | {int(row.get('open_failed') or 0)} | "
            f"{float(row.get('realized_pnl_usdt') or 0):+.2f} | {float(row.get('estimated_cost_usdt') or 0):.2f} | "
            f"{float(row.get('pnl_after_cost_usdt') or 0):+.2f} | {float(row.get('forced_close_rate') or 0):.1%} |"
        )
    lines.extend(["", "## Top Losers"])
    for item in ((payload.get("windows") or {}).get("72h") or {}).get("top_losers") or []:
        lines.append(f"- `{item.get('ts')}` {item.get('symbol')} {item.get('side')} {float(item.get('pnl_usdt') or 0):+.2f} USDT: {item.get('reason')}")
    return "\n".join(lines) + "\n"


def write_outputs(payload: dict[str, Any], runtime_dir: Path, reports_dir: Path) -> None:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "a_v11_rollout_review_latest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    (reports_dir / "a_v11_rollout_review_latest.md").write_text(render_md(payload), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only A/v11 trailing rollout review")
    parser.add_argument("--db", default="")
    parser.add_argument("--runtime-dir", default=str(ROOT / "runtime"))
    parser.add_argument("--reports-dir", default=str(ROOT / "reports"))
    args = parser.parse_args(argv)
    db = find_db(args.db)
    if not db:
        raise SystemExit("event_store.sqlite3 not found")
    payload = build_payload(db, load_approval())
    write_outputs(payload, Path(args.runtime_dir), Path(args.reports_dir))
    decision = payload["decision"]
    win72 = payload["windows"].get("72h", {})
    print(
        json.dumps(
            {
                "status": decision.get("status"),
                "priority": decision.get("priority"),
                "pnl_after_cost_72h": win72.get("pnl_after_cost_usdt"),
                "closed_72h": win72.get("closed_samples"),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
