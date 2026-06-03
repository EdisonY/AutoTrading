"""Review B/v16 approved full-live rollout windows.

Read-only. Summarizes post-approval B/v16 live results so the operator can
decide whether to keep observing, narrow parameters, or prepare rollback.
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
ROOT = SCRIPT_DIR.parent if (SCRIPT_DIR.parent / "PROJECT_STATE.md").exists() else SCRIPT_DIR
CST = timezone(timedelta(hours=8))
WINDOWS_HOURS = (24, 72, 168)
DEFAULT_APPROVED_AT = "2026-05-31T02:00:00+08:00"
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
    path = ROOT / "research_memory" / "approvals" / "approve_full_live_B_v16_sample_expansion_2026-05-31.json"
    payload = read_json(path)
    if isinstance(payload, dict):
        return payload
    return {
        "candidate_ids": [
            "EXP-20260527-v16-atr-stop-bands",
            "EXP-20260527-v16-overheat-cap-85",
        ],
        "approved_at": DEFAULT_APPROVED_AT,
        "selected_live_parameter": {"score_max": 85},
    }


def query_rows(db: Path, start: datetime) -> list[sqlite3.Row]:
    with sqlite3.connect(db) as con:
        con.row_factory = sqlite3.Row
        return list(
            con.execute(
                """
                select id, ts, strategy, symbol, event_type, category, side, reason, payload_json
                from events
                where strategy = 'B/v16'
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
        "score_bucket_pnl": {},
    }
    trades: list[dict[str, Any]] = []
    reason_counter: collections.Counter[str] = collections.Counter()
    side_pnl: collections.defaultdict[str, float] = collections.defaultdict(float)
    score_bucket_pnl: collections.defaultdict[str, float] = collections.defaultdict(float)
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
            metrics["closes" if event_type == "CLOSE" else "forced_closes"] += 1
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except Exception:
                payload = {}
            pnl = payload_float(payload, "pnl_usd", "pnl_usdt", "realized_pnl_usdt", "pnl")
            reason = payload_text(payload, "reason", "close_reason") or str(row["reason"] or "-")
            side = str(row["side"] or payload_text(payload, "side") or "-").lower()
            score = payload_float(payload, "score", "entry_score", "net_score")
            score_bucket = "unknown"
            if score:
                score_bucket = ">=85" if abs(score) >= 85 else "<85"
            metrics["realized_pnl_usdt"] += pnl
            if event_type == "FORCED_CLOSE":
                metrics["forced_close_pnl_usdt"] += pnl
            reason_counter[reason] += 1
            side_pnl[side] += pnl
            score_bucket_pnl[score_bucket] += pnl
            trades.append(
                {
                    "ts": row["ts"],
                    "symbol": row["symbol"],
                    "side": side,
                    "event_type": event_type,
                    "reason": reason,
                    "score_bucket": score_bucket,
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
    metrics["score_bucket_pnl"] = {k: round(v, 4) for k, v in sorted(score_bucket_pnl.items())}
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
        actions.append("Review whether B/v16 full-live candidates should keep full observation.")
    if float(three.get("forced_close_rate") or 0) >= 0.10:
        label = "manual_review_required"
        priority = "P1"
        actions.append("Forced-close rate is high; split ATR stop bands by market regime first.")
    if int(day.get("open_failed") or 0) >= 5:
        label = "rollback_review_required"
        priority = "P1"
        actions.append("24h OPEN_FAILED pressure is high; prepare rollback or narrowing review.")
    if not actions:
        actions.append("Keep collecting samples; do not expand risk.")
    return {"priority": priority, "status": label, "recommended_actions": actions}


def decision_packet(approval: dict[str, Any], windows: dict[str, dict[str, Any]], decision: dict[str, Any]) -> dict[str, Any]:
    three = windows.get("72h", {})
    week = windows.get("168h", {})
    closed72 = int(three.get("closed_samples") or 0)
    closed168 = int(week.get("closed_samples") or 0)
    maturity = "mature_168h" if closed168 >= 100 else "reviewable_72h" if closed72 >= 50 else "thin_live_window"
    top_loser = (three.get("top_losers") or [{}])[0]
    close_reasons = [str(item.get("reason") or "") for item in (three.get("close_reasons") or [])[:3]]
    risks = [
        f"72h after-cost pnl {float(three.get('pnl_after_cost_usdt') or 0):+.2f} USDT",
        f"72h forced close rate {float(three.get('forced_close_rate') or 0):.1%}",
        f"72h open failed rate {float(three.get('open_failed_rate') or 0):.1%}",
    ]
    if close_reasons:
        risks.append(f"top close reasons: {', '.join(close_reasons)}")
    if top_loser.get("symbol"):
        risks.append(
            "top loser: {symbol} {side} {pnl:+.2f} USDT".format(
                symbol=top_loser.get("symbol"),
                side=top_loser.get("side") or "",
                pnl=float(top_loser.get("pnl_usdt") or 0),
            )
        )
    return {
        "change": "B/v16 approved ATR stop bands + overheat score cap rollout",
        "live_parameter": approval.get("selected_live_parameter") or {},
        "expected_advantage": approval.get("next_step") or "approved full-live B/v16 candidates",
        "risk": risks,
        "evidence_maturity": {"label": maturity, "closed_72h": closed72, "closed_168h": closed168},
        "rollback_path": [
            "keep automatic rollback disabled",
            "if operator approves, revert B/v16 score_max/ATR stop bands to previous stable config",
            "rerun 24h/72h rollout review after revert",
        ],
        "operator_action": decision.get("status") or "",
        "automation": "disabled_report_only",
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
        "strategy": "B/v16",
        "db": str(db),
        "approved_at": approved_at.isoformat(timespec="seconds"),
        "candidate_ids": approval.get("candidate_ids") or [],
        "selected_live_parameter": approval.get("selected_live_parameter") or {},
        "decision": decision,
        "decision_packet": decision_packet(approval, windows, decision),
        "windows": windows,
    }


def render_md(payload: dict[str, Any]) -> str:
    decision = payload.get("decision") or {}
    packet = payload.get("decision_packet") or {}
    lines = [
        "# B/v16 Full-Live Rollout Review",
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
            "## Decision Packet",
            "",
            f"- Change: {packet.get('change') or '-'}",
            f"- Expected advantage: {packet.get('expected_advantage') or '-'}",
            f"- Evidence maturity: `{((packet.get('evidence_maturity') or {}).get('label')) or '-'}`",
            f"- Risk: {'; '.join(packet.get('risk') or [])}",
            f"- Rollback path: {'; '.join(packet.get('rollback_path') or [])}",
            f"- Automation: `{packet.get('automation') or 'disabled_report_only'}`",
            "",
            "## Windows",
            "",
            "| Window | Opens | Closed | Forced | Open failed | PnL | Cost | After cost | Forced rate | Open fail rate |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for name, row in (payload.get("windows") or {}).items():
        lines.append(
            f"| {name} | {int(row.get('opens') or 0)} | {int(row.get('closed_samples') or 0)} | "
            f"{int(row.get('forced_closes') or 0)} | {int(row.get('open_failed') or 0)} | "
            f"{float(row.get('realized_pnl_usdt') or 0):+.2f} | {float(row.get('estimated_cost_usdt') or 0):.2f} | "
            f"{float(row.get('pnl_after_cost_usdt') or 0):+.2f} | {float(row.get('forced_close_rate') or 0):.1%} | "
            f"{float(row.get('open_failed_rate') or 0):.1%} |"
        )
    lines.extend(["", "## Top Losers"])
    for item in ((payload.get("windows") or {}).get("72h") or {}).get("top_losers") or []:
        lines.append(f"- `{item.get('ts')}` {item.get('symbol')} {item.get('side')} {float(item.get('pnl_usdt') or 0):+.2f} USDT: {item.get('reason')}")
    return "\n".join(lines) + "\n"


def write_outputs(payload: dict[str, Any], runtime_dir: Path, reports_dir: Path) -> None:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "b_v16_rollout_review_latest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    (reports_dir / "b_v16_rollout_review_latest.md").write_text(render_md(payload), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only B/v16 full-live rollout review")
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
