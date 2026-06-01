"""Unified strategy evolution gate.

This script is read-only. It merges research candidates, shadow experiment
results, counterfactual skip evaluation, manual reviews, and account risk into
one decision feed for the portal. It never changes live strategy code or orders.
"""

from __future__ import annotations

import argparse
import html
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
ROOT = SCRIPT_DIR if (SCRIPT_DIR / "core").exists() else SCRIPT_DIR.parent
MEMORY_DIR = ROOT / "research_memory"
EXPERIMENTS_DIR = ROOT / "experiments"
REPORTS_DIR = ROOT / "reports"
RUNTIME_DIR = ROOT / "runtime"
COUNTERFACTUAL_JSON = REPORTS_DIR / "counterfactual_open_skips_latest.json"
ACCOUNT_SNAPSHOT_JSON = RUNTIME_DIR / "account_snapshot_latest.json"
CST = timezone(timedelta(hours=8))
WINDOWS = (3, 7, 14, 30)
POST_APPROVAL_WINDOWS_HOURS = (24, 72, 168)


def h(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def parse_date(text: Any) -> datetime | None:
    if not text:
        return None
    raw = str(text).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST)
        return dt.astimezone(CST)
    except Exception:
        return None


def sample_window_days(result: dict[str, Any]) -> int:
    window = str(result.get("sample_window") or "")
    if "~" not in window:
        return to_int(result.get("window_days"), 0)
    start_s, end_s = [p.strip() for p in window.split("~", 1)]
    try:
        start = datetime.strptime(start_s[:10], "%Y-%m-%d")
        end = datetime.strptime(end_s[:10], "%Y-%m-%d")
        return max(1, (end - start).days + 1)
    except Exception:
        return to_int(result.get("window_days"), 0)


def row_key(row: dict[str, Any]) -> str:
    return str(row.get("candidate_id") or row.get("experiment_id") or row.get("family_id") or row.get("id") or "")


def candidate_key(candidate: dict[str, Any]) -> str:
    return str(candidate.get("candidate_id") or candidate.get("experiment_id") or candidate.get("family_id") or "")


def group_by_key(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = row_key(row)
        if key:
            grouped.setdefault(key, []).append(row)
    return grouped


def experiment_verdict(result: dict[str, Any]) -> tuple[str, list[str]]:
    notes: list[str] = []
    sample = to_int(result.get("sample_trades"))
    original = to_float(result.get("original_pnl"))
    shadow = to_float(result.get("shadow_pnl"))
    pnl_delta = shadow - original
    avoided = to_float(result.get("avoided_loss"))
    missed = to_float(result.get("missed_profit"))
    hard_before = to_int(result.get("hard_stop_before"))
    hard_after = to_int(result.get("hard_stop_after"))
    status = str(result.get("promotion_status") or "").lower()
    change_type = str(result.get("change_type") or "")
    note_text = " ".join(str(n) for n in (result.get("notes") or []))

    if "reject" in status:
        notes.append(f"实验状态为 {status}")
        return "fail", notes
    if sample < 30:
        notes.append(f"样本不足 {sample}/30")
        return "insufficient", notes
    if (
        "confirmation" in change_type
        and abs(original) < 1e-9
        and abs(shadow) < 1e-9
        and abs(avoided) < 1e-9
        and abs(missed) < 1e-9
    ) or "没有真实成交PnL" in note_text:
        notes.append("缺少真实/纸面撮合 PnL，不能作为晋级证据")
        return "observe", notes
    if pnl_delta < 0:
        notes.append(f"影子 PnL 低于原版 {pnl_delta:+.2f}")
        return "fail", notes
    if hard_after > hard_before:
        notes.append(f"硬顶/硬底增加 {hard_before}->{hard_after}")
        return "fail", notes
    if missed > avoided and missed > 0:
        notes.append(f"错过盈利 {missed:.2f} 大于避免亏损 {avoided:.2f}")
        return "observe", notes
    if status == "approved_candidate":
        notes.append("实验进入待人工审批")
        return "pass", notes
    if status == "approved_for_small_live":
        notes.append("人工批准小仓观察，不等于全量已验证")
        return "observe", notes
    notes.append(f"影子 PnL 增量 {pnl_delta:+.2f}")
    return "pass", notes


def account_risk_by_strategy(snapshot: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(snapshot, dict):
        return out
    for account in snapshot.get("accounts") or []:
        strategy = str(account.get("strategy") or "")
        if not strategy:
            continue
        out[strategy] = {
            "risk_count": to_int(account.get("hard_stop_risk_count") or account.get("risk_count")),
            "sizing_violation_count": to_int(account.get("sizing_violation_count")),
            "unrealized_pnl_usdt": to_float(account.get("unrealized_pnl_usdt")),
            "open_positions": to_int(account.get("open_positions")),
        }
    return out


def relevant_counterfactual_rows(candidate: dict[str, Any], counterfactual: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(counterfactual, dict):
        return []
    strategy = str(candidate.get("strategy") or candidate.get("base_strategy") or "")
    change_type = str(candidate.get("change_type") or "")
    rows = [r for r in counterfactual.get("filters") or [] if str(r.get("strategy") or "") == strategy]
    if "confirmation" in change_type:
        return [r for r in rows if "confirmation" in str(r.get("filter") or "")]
    if "replacement" in change_type:
        return [r for r in rows if "position_replacement" in str(r.get("filter") or "")]
    if "stage" in change_type or "filter" in change_type:
        return [
            r for r in rows
            if any(k in str(r.get("filter") or "") for k in ("stage_guard", "tail_guard", "threshold", "risk_gate"))
        ]
    return rows[:3]


def counterfactual_verdict(candidate: dict[str, Any], rows: list[dict[str, Any]]) -> tuple[str, dict[str, Any], list[str]]:
    if not rows:
        return "missing", {"samples": 0, "pnl": 0.0}, ["无匹配反事实证据"]
    samples = sum(to_int(r.get("samples")) for r in rows)
    pnl = sum(to_float(r.get("pnl")) for r in rows)
    change_type = str(candidate.get("change_type") or "")
    notes = [f"匹配反事实样本 {samples}，PnL {pnl:+.2f}"]
    if samples < 20:
        return "insufficient", {"samples": samples, "pnl": round(pnl, 4)}, notes + ["反事实样本不足 20"]
    if "confirmation" in change_type or "replacement" in change_type:
        if pnl > 0:
            return "support", {"samples": samples, "pnl": round(pnl, 4)}, notes + ["放宽/替换方向得到反事实支持"]
        return "oppose", {"samples": samples, "pnl": round(pnl, 4)}, notes + ["放宽后反事实收益为负"]
    if pnl < 0:
        return "support", {"samples": samples, "pnl": round(pnl, 4)}, notes + ["阻断类过滤保护了账户"]
    if pnl > 0:
        return "oppose", {"samples": samples, "pnl": round(pnl, 4)}, notes + ["阻断类过滤可能错杀盈利"]
    return "neutral", {"samples": samples, "pnl": round(pnl, 4)}, notes


def window_status(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for window in WINDOWS:
        covered = [r for r in results if sample_window_days(r) >= window]
        if not covered:
            out[f"{window}d"] = {"status": "insufficient", "sample_trades": 0, "pnl_delta": 0.0}
            continue
        best = max(covered, key=lambda r: sample_window_days(r))
        verdict, notes = experiment_verdict(best)
        out[f"{window}d"] = {
            "status": verdict,
            "sample_trades": to_int(best.get("sample_trades")),
            "pnl_delta": round(to_float(best.get("shadow_pnl")) - to_float(best.get("original_pnl")), 4),
            "sample_window": best.get("sample_window"),
            "notes": notes,
        }
    return out


def priority_from(status: str) -> str:
    if status == "rollback_required":
        return "P0"
    if status == "rollback_watch":
        return "P1"
    if status == "verified_upgrade_ready":
        return "P0"
    if status == "ready_for_review":
        return "P1"
    if status in {"full_live_monitoring", "small_live_monitoring", "shadow_validating", "counterfactual_supported"}:
        return "P2"
    if status == "rejected":
        return "REJECT"
    return "P3"


# Fee/slippage adjustment constants
FEE_SLIPPAGE_ADJUSTMENT_PCT = 0.15  # 0.15% total cost per round-trip
MIN_SAMPLE_TRADES = 30
MIN_SAMPLE_FOR_P0 = 50
MIN_SAMPLE_FOR_P1 = 30

# Rollback trigger thresholds
ROLLBACK_OPEN_FAILED_THRESHOLD = 5
ROLLBACK_PF_DECAY_RATIO = 0.8  # new PF < old PF * 0.8
ROLLBACK_HARD_STOP_RATIO = 1.5  # new hard-stop rate > old * 1.5
ROLLBACK_ACCOUNT_LOSS_USDT = 200  # 7-day account loss
POST_APPROVAL_MIN_CLOSED = 20
POST_APPROVAL_MIN_CLOSED_BY_HOURS = {
    24: 20,
    72: 50,
    168: 100,
}
POST_APPROVAL_LOSS_USDT = 80
POST_APPROVAL_FORCED_CLOSE_RATE = 0.12
POST_APPROVAL_OPEN_FAILED_RATE = 0.08
POST_APPROVAL_NOTIONAL_PER_TRADE = 400.0


def adjust_pnl_for_fees(pnl: float, sample_trades: int, notional_per_trade: float = 400.0) -> float:
    """Adjust PnL by deducting estimated fee/slippage."""
    total_notional = notional_per_trade * sample_trades
    fee_cost = total_notional * FEE_SLIPPAGE_ADJUSTMENT_PCT / 100
    return pnl - fee_cost


def check_rollback_triggers(
    results: list[dict[str, Any]],
    account_risk: dict[str, Any],
) -> list[str]:
    """Check if rollback triggers are activated."""
    triggers = []
    if not results:
        return triggers
    latest = results[-1]

    # Check sample sufficiency
    sample = to_int(latest.get("sample_trades"))
    if sample < MIN_SAMPLE_TRADES:
        triggers.append(f"样本不足 {sample}/{MIN_SAMPLE_TRADES}，不升至P0/P1")

    # Check if shadow PnL is worse than original
    original = to_float(latest.get("original_pnl"))
    shadow = to_float(latest.get("shadow_pnl"))
    if original > 0 and shadow < original * ROLLBACK_PF_DECAY_RATIO:
        triggers.append(f"影子PnL {shadow:.2f} < 原版 {original:.2f} × {ROLLBACK_PF_DECAY_RATIO}")

    # Check hard-stop increase
    hs_before = to_int(latest.get("hard_stop_before"))
    hs_after = to_int(latest.get("hard_stop_after"))
    if hs_before > 0 and hs_after > hs_before * ROLLBACK_HARD_STOP_RATIO:
        triggers.append(f"硬顶触发率增加 {hs_before}→{hs_after}")

    # Check account risk
    if account_risk.get("sizing_violation_count"):
        triggers.append(f"尺寸违规 {account_risk.get('sizing_violation_count')}")
    if account_risk.get("risk_count"):
        triggers.append(f"硬顶风险 {account_risk.get('risk_count')}")

    return triggers


def load_full_live_approvals(memory_dir: Path) -> dict[str, dict[str, Any]]:
    approval_dir = memory_dir / "approvals"
    rows: list[dict[str, Any]] = []
    for path in (approval_dir / "manual_actions.jsonl", approval_dir / "manual_actions_latest.jsonl"):
        rows.extend(read_jsonl(path))
    if approval_dir.exists():
        for path in approval_dir.glob("approve_full_live_*.json"):
            payload = read_json(path)
            if isinstance(payload, dict):
                rows.append(payload)
    approved: dict[str, dict[str, Any]] = {}
    for row in rows:
        if str(row.get("manual_action") or "") != "approve_full_live":
            continue
        if str(row.get("approved_scope") or "") != "full_live":
            continue
        ids: list[Any] = []
        ids.extend(row.get("candidate_ids") or [])
        ids.extend(row.get("experiment_ids") or [])
        for key in ("candidate_id", "experiment_id"):
            value = row.get(key)
            if value:
                ids.append(value)
        for candidate_id in ids:
            text = str(candidate_id or "").strip()
            if text:
                approved[text] = row
    return approved


def find_event_db(runtime_dir: Path, explicit: str | None = None) -> Path | None:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    root = runtime_dir.parent
    candidates.extend(
        [
            root / "server_logs_tencent" / "runtime" / "event_store.sqlite3",
            runtime_dir / "event_store.sqlite3",
        ]
    )
    for path in candidates:
        if not path.exists():
            continue
        try:
            conn = sqlite3.connect(str(path))
            has_events = conn.execute("select 1 from sqlite_master where type='table' and name='events'").fetchone()
            conn.close()
        except Exception:
            has_events = None
        if has_events:
            return path
    return None


def payload_float(payload: dict[str, Any], keys: tuple[str, ...]) -> float:
    for key in keys:
        if key in payload:
            return to_float(payload.get(key))
    return 0.0


def classify_regime(metrics: dict[str, Any]) -> dict[str, Any]:
    event_count = max(1, to_int(metrics.get("event_count")))
    avg_abs_change = to_float(metrics.get("abs_change_sum")) / event_count
    avg_abs_velocity = to_float(metrics.get("abs_velocity_sum")) / event_count
    volume_samples = to_int(metrics.get("volume_samples"))
    avg_quote_volume = to_float(metrics.get("quote_volume_sum")) / max(1, volume_samples)
    long_count = to_int(metrics.get("long_count"))
    short_count = to_int(metrics.get("short_count"))
    directional_total = max(1, long_count + short_count)
    long_ratio = long_count / directional_total
    forced_closes = to_int(metrics.get("forced_closes"))
    opens = to_int(metrics.get("opens"))

    signals: list[str] = []
    label = "range"
    if avg_abs_change >= 8 or avg_abs_velocity >= 0.35 or forced_closes >= max(3, opens // 8):
        label = "high_volatility"
        signals.append("large_move_or_forced_close")
    elif volume_samples and avg_quote_volume < 5_000_000:
        label = "low_liquidity"
        signals.append("low_quote_volume")
    elif long_ratio >= 0.65 or long_ratio <= 0.35:
        label = "trend"
        signals.append("directional_imbalance")
    else:
        signals.append("balanced_flow")

    return {
        "label": label,
        "signals": signals,
        "avg_abs_change_pct": round(avg_abs_change, 4),
        "avg_abs_velocity_pct": round(avg_abs_velocity, 4),
        "avg_quote_volume": round(avg_quote_volume, 4) if volume_samples else 0.0,
        "long_ratio": round(long_ratio, 4),
    }


def classify_window_quality(metrics: dict[str, Any]) -> dict[str, Any]:
    window_hours = to_int(metrics.get("window_hours"))
    required_closed = POST_APPROVAL_MIN_CLOSED_BY_HOURS.get(window_hours, POST_APPROVAL_MIN_CLOSED)
    opens = to_int(metrics.get("opens"))
    closes = to_int(metrics.get("closes"))
    forced_closes = to_int(metrics.get("forced_closes"))
    open_failed = to_int(metrics.get("open_failed"))
    close_failed = to_int(metrics.get("close_failed"))
    closed_total = closes + forced_closes
    attempted_opens = opens + open_failed
    forced_rate = forced_closes / max(1, closed_total)
    open_failed_rate = open_failed / max(1, attempted_opens)
    cost = closed_total * POST_APPROVAL_NOTIONAL_PER_TRADE * FEE_SLIPPAGE_ADJUSTMENT_PCT / 100
    pnl_after_cost = to_float(metrics.get("realized_pnl_usdt")) - cost
    reasons: list[str] = []
    if close_failed:
        reasons.append(f"close_failed={close_failed}")
    if closed_total < required_closed:
        label = "maturing"
        reasons.append(f"closed_samples={closed_total}/{required_closed}")
    else:
        label = "ok"
        if pnl_after_cost <= -POST_APPROVAL_LOSS_USDT:
            label = "bad"
            reasons.append(f"pnl_after_cost={pnl_after_cost:.2f}")
        if forced_rate >= POST_APPROVAL_FORCED_CLOSE_RATE:
            label = "bad"
            reasons.append(f"forced_close_rate={forced_rate:.1%}")
        if open_failed_rate >= POST_APPROVAL_OPEN_FAILED_RATE:
            label = "bad"
            reasons.append(f"open_failed_rate={open_failed_rate:.1%}")
    if not reasons:
        reasons.append("window_quality_ok")
    return {
        "label": label,
        "closed_samples": closed_total,
        "required_closed_samples": required_closed,
        "forced_close_rate": round(forced_rate, 4),
        "open_failed_rate": round(open_failed_rate, 4),
        "estimated_cost_usdt": round(cost, 4),
        "realized_pnl_after_cost": round(pnl_after_cost, 4),
        "reasons": reasons[:4],
    }


def summarize_post_approval_windows(db_path: Path | None, approvals: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if not db_path or not approvals:
        return {}
    now = datetime.now(CST)
    out: dict[str, dict[str, Any]] = {}
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
    except Exception:
        return out
    try:
        cache: dict[str, list[sqlite3.Row]] = {}
        for candidate_id, approval in approvals.items():
            strategy = str(approval.get("base_strategy") or approval.get("strategy") or "")
            approved_at = parse_date(approval.get("approved_at") or approval.get("applied_at"))
            if not strategy or not approved_at:
                continue
            if strategy not in cache:
                cache[strategy] = list(
                    conn.execute(
                        """
                        select ts, strategy, symbol, event_type, category, side, payload_json
                        from events
                        where strategy = ?
                        """,
                        (strategy,),
                    )
                )
            windows: dict[str, dict[str, Any]] = {}
            for hours in POST_APPROVAL_WINDOWS_HOURS:
                start = max(approved_at, now - timedelta(hours=hours))
                seen: set[tuple[str, str, str, str, str]] = set()
                metrics = {
                    "window_hours": hours,
                    "status": "ready" if now - approved_at >= timedelta(hours=hours) else "maturing",
                    "since": start.isoformat(timespec="seconds"),
                    "opens": 0,
                    "closes": 0,
                    "forced_closes": 0,
                    "open_failed": 0,
                    "close_failed": 0,
                    "open_skipped": 0,
                    "realized_pnl_usdt": 0.0,
                    "latest_event_ts": "",
                    "event_count": 0,
                    "long_count": 0,
                    "short_count": 0,
                    "abs_change_sum": 0.0,
                    "abs_velocity_sum": 0.0,
                    "quote_volume_sum": 0.0,
                    "volume_samples": 0,
                }
                for row in cache[strategy]:
                    ts_text = str(row["ts"] or "")
                    event_dt = parse_date(ts_text)
                    if not event_dt or event_dt < start:
                        continue
                    event_type = str(row["event_type"] or "")
                    key = (
                        ts_text,
                        str(row["symbol"] or ""),
                        event_type,
                        str(row["side"] or ""),
                        str(row["category"] or ""),
                    )
                    if key in seen:
                        continue
                    seen.add(key)
                    metrics["event_count"] += 1
                    side_text = str(row["side"] or "").lower()
                    if side_text == "long":
                        metrics["long_count"] += 1
                    elif side_text == "short":
                        metrics["short_count"] += 1
                    try:
                        payload = json.loads(row["payload_json"] or "{}")
                    except Exception:
                        payload = {}
                    abs_change = abs(payload_float(payload, ("sentinel_change_pct", "change_pct", "price_change_pct", "pnl_pct")))
                    abs_velocity = abs(payload_float(payload, ("sentinel_abs_velocity_pct", "sentinel_velocity_pct", "velocity_pct")))
                    quote_volume = payload_float(payload, ("sentinel_quote_volume", "quote_volume", "volume_usdt", "turnover_usdt"))
                    metrics["abs_change_sum"] += abs_change
                    metrics["abs_velocity_sum"] += abs_velocity
                    if quote_volume > 0:
                        metrics["quote_volume_sum"] += quote_volume
                        metrics["volume_samples"] += 1
                    if ts_text > str(metrics["latest_event_ts"]):
                        metrics["latest_event_ts"] = ts_text
                    if event_type == "OPEN":
                        metrics["opens"] += 1
                    elif event_type == "CLOSE":
                        metrics["closes"] += 1
                    elif event_type == "FORCED_CLOSE":
                        metrics["forced_closes"] += 1
                    elif event_type == "OPEN_FAILED":
                        metrics["open_failed"] += 1
                    elif event_type in {"CLOSE_FAILED", "FORCED_CLOSE_FAILED"}:
                        metrics["close_failed"] += 1
                    elif event_type == "OPEN_SKIPPED":
                        metrics["open_skipped"] += 1
                    if event_type in {"CLOSE", "FORCED_CLOSE"}:
                        metrics["realized_pnl_usdt"] += payload_float(payload, ("pnl_usd", "pnl_usdt", "realized_pnl_usdt", "pnl"))
                metrics["realized_pnl_usdt"] = round(float(metrics["realized_pnl_usdt"]), 4)
                metrics["regime"] = classify_regime(metrics)
                metrics["quality"] = classify_window_quality(metrics)
                for key in ("abs_change_sum", "abs_velocity_sum", "quote_volume_sum"):
                    metrics.pop(key, None)
                windows[f"{hours}h"] = metrics
            out[candidate_id] = {
                "approved_at": approved_at.isoformat(timespec="seconds"),
                "strategy": strategy,
                "windows": windows,
            }
    finally:
        conn.close()
    return out


def rollback_watch_verdict(
    results: list[dict[str, Any]],
    account_risk: dict[str, Any],
    post_approval: dict[str, Any] | None = None,
) -> tuple[str, list[str]]:
    triggers: list[str] = []
    priority = ""
    if account_risk.get("sizing_violation_count"):
        priority = "P0"
        triggers.append(f"尺寸违规 {account_risk.get('sizing_violation_count')}")
    if account_risk.get("risk_count"):
        priority = "P0"
        triggers.append(f"硬顶风险 {account_risk.get('risk_count')}")
    if post_approval:
        for label, metrics in (post_approval.get("windows") or {}).items():
            close_failed = to_int(metrics.get("close_failed"))
            if close_failed:
                priority = "P0"
                triggers.append(f"{label} 关闭确认失败 {close_failed}")
                break
        day_window = (post_approval.get("windows") or {}).get("24h") or {}
        day_open_failed = to_int(day_window.get("open_failed"))
        if day_open_failed >= ROLLBACK_OPEN_FAILED_THRESHOLD:
            priority = priority or "P1"
            triggers.append(f"24h OPEN_FAILED {day_open_failed}")
        for label, metrics in (post_approval.get("windows") or {}).items():
            quality = metrics.get("quality") or {}
            if quality.get("label") == "bad":
                priority = priority or "P1"
                reason = "; ".join(str(x) for x in (quality.get("reasons") or [])[:2])
                triggers.append(f"{label} 实盘窗口质量差 {reason}")
                break
    if to_float(account_risk.get("unrealized_pnl_usdt")) <= -ROLLBACK_ACCOUNT_LOSS_USDT:
        priority = priority or "P1"
        triggers.append(f"策略账户浮亏 {to_float(account_risk.get('unrealized_pnl_usdt')):.2f} USDT")
    if results:
        latest = results[-1]
        sample = to_int(latest.get("sample_trades"))
        if sample >= MIN_SAMPLE_TRADES:
            original = to_float(latest.get("original_pnl"))
            shadow = to_float(latest.get("shadow_pnl"))
            adjusted_shadow = adjust_pnl_for_fees(shadow, sample)
            if original > 0 and shadow < original * ROLLBACK_PF_DECAY_RATIO:
                priority = priority or "P1"
                triggers.append(f"影子PnL {shadow:.2f} < 原版 {original:.2f} × {ROLLBACK_PF_DECAY_RATIO}")
            if adjusted_shadow < 0 and shadow < original:
                priority = priority or "P1"
                triggers.append(f"扣费后影子PnL {adjusted_shadow:.2f} 且弱于原版 {original:.2f}")
            hs_before = to_int(latest.get("hard_stop_before"))
            hs_after = to_int(latest.get("hard_stop_after"))
            if hs_before > 0 and hs_after > hs_before * ROLLBACK_HARD_STOP_RATIO:
                priority = priority or "P1"
                triggers.append(f"硬顶触发率增加 {hs_before}→{hs_after}")
            if hs_before == 0 and hs_after >= 3:
                priority = priority or "P1"
                triggers.append(f"新增硬顶触发 {hs_after}")
            open_failed = to_int(latest.get("open_failed_after") or latest.get("open_failed"))
            if open_failed >= ROLLBACK_OPEN_FAILED_THRESHOLD:
                priority = priority or "P1"
                triggers.append(f"OPEN_FAILED {open_failed}")
    return priority, triggers


def post_approval_sample_blockers(post_approval: dict[str, Any] | None) -> list[str]:
    if not post_approval:
        return []
    blockers: list[str] = []
    for label, metrics in (post_approval.get("windows") or {}).items():
        if metrics.get("status") != "ready":
            continue
        quality = metrics.get("quality") or {}
        required = to_int(quality.get("required_closed_samples"))
        closed = to_int(quality.get("closed_samples"))
        if required and closed < required:
            blockers.append(f"{label} 实盘样本未达最低数 {closed}/{required}")
    return blockers


def classify_decision(
    candidate: dict[str, Any],
    results: list[dict[str, Any]],
    review: dict[str, Any] | None,
    cf_status: str,
    account_risk: dict[str, Any],
    full_live_approval: dict[str, Any] | None = None,
    post_approval: dict[str, Any] | None = None,
) -> tuple[str, str, list[str]]:
    blockers: list[str] = []
    win = window_status(results)
    latest = results[-1] if results else {}
    exp_status, exp_notes = experiment_verdict(latest) if latest else ("missing", ["无影子实验结果"])
    promotion_status = str((review or {}).get("promotion_status") or latest.get("promotion_status") or candidate.get("status") or "")

    if full_live_approval:
        rollback_priority, rollback_triggers = rollback_watch_verdict(results, account_risk, post_approval)
        if rollback_priority == "P0":
            return "rollback_required", "review_or_rollback_live_change", rollback_triggers
        if rollback_priority == "P1":
            return "rollback_watch", "investigate_live_degradation", rollback_triggers
        sample = to_int(latest.get("sample_trades")) if latest else 0
        if sample < MIN_SAMPLE_TRADES:
            blockers.append(f"全量放开后样本继续收集中 {sample}/{MIN_SAMPLE_TRADES}")
        blockers.extend(post_approval_sample_blockers(post_approval))
        return "full_live_monitoring", "keep_full_live_monitoring", blockers

    # Rollback triggers
    rollback_triggers = check_rollback_triggers(results, account_risk)
    blockers.extend(rollback_triggers)

    if account_risk.get("sizing_violation_count"):
        blockers.append(f"账户存在尺寸违规 {account_risk.get('sizing_violation_count')}")
    if account_risk.get("risk_count"):
        blockers.append(f"账户硬顶风险 {account_risk.get('risk_count')}")
    if exp_status == "fail" or "reject" in promotion_status:
        blockers.extend(exp_notes[:2])
        return "rejected", "reject_or_rework", blockers

    # Fee-adjusted PnL check
    sample = to_int(latest.get("sample_trades"))
    original_pnl = to_float(latest.get("original_pnl"))
    shadow_pnl = to_float(latest.get("shadow_pnl"))
    adjusted_shadow = adjust_pnl_for_fees(shadow_pnl, sample)

    pass_14 = win["14d"]["status"] == "pass"
    pass_30 = win["30d"]["status"] == "pass"
    pass_7 = win["7d"]["status"] == "pass"

    # P0: strong multi-window evidence + enough samples + fee-adjusted positive
    if pass_14 and pass_30 and cf_status in {"support", "neutral"} and not blockers:
        if sample >= MIN_SAMPLE_FOR_P0 and adjusted_shadow > 0:
            return "verified_upgrade_ready", "review_for_expansion", blockers
        elif sample < MIN_SAMPLE_FOR_P0:
            blockers.append(f"P0需要≥{MIN_SAMPLE_FOR_P0}样本，当前{sample}")

    # P1: promising for decision-maker review
    if pass_7 and pass_14 and cf_status != "oppose" and not blockers:
        if sample >= MIN_SAMPLE_FOR_P1:
            return "ready_for_review", "review_for_small_live", blockers
        else:
            blockers.append(f"P1需要≥{MIN_SAMPLE_FOR_P1}样本，当前{sample}")

    if "approved_for_small_live" in promotion_status:
        blockers.extend(exp_notes[:2])
        return "small_live_monitoring", "keep_small_live_monitoring", blockers
    if cf_status == "support":
        blockers.append("长窗口影子验证不足")
        return "counterfactual_supported", "run_multi_window_shadow", blockers
    if exp_status in {"pass", "observe"}:
        blockers.extend(exp_notes[:2])
        return "shadow_validating", "continue_shadow_validation", blockers
    blockers.extend(exp_notes[:2])
    return "observe", "continue_observation", blockers


def evidence_score(results: list[dict[str, Any]], cf_status: str, win: dict[str, dict[str, Any]]) -> int:
    score = 0
    if results:
        latest = results[-1]
        sample = to_int(latest.get("sample_trades"))
        score += min(20, sample // 5)
        verdict, _ = experiment_verdict(latest)
        if verdict == "pass":
            score += 25
        elif verdict == "observe":
            score += 10
        elif verdict == "fail":
            score -= 20
    if cf_status == "support":
        score += 20
    elif cf_status == "oppose":
        score -= 20
    for label in ("3d", "7d", "14d", "30d"):
        if win[label]["status"] == "pass":
            score += 8
    return max(0, min(100, score))


def risk_score(results: list[dict[str, Any]], cf_status: str, account_risk: dict[str, Any], win: dict[str, dict[str, Any]]) -> int:
    score = 0
    if account_risk.get("sizing_violation_count"):
        score += 25
    if account_risk.get("risk_count"):
        score += 20
    if results:
        latest = results[-1]
        delta = to_float(latest.get("shadow_pnl")) - to_float(latest.get("original_pnl"))
        if delta < 0:
            score += 25
        if to_float(latest.get("missed_profit")) > to_float(latest.get("avoided_loss")):
            score += 15
        if to_int(latest.get("sample_trades")) < 30:
            score += 15
    if cf_status == "oppose":
        score += 20
    if win["14d"]["status"] == "insufficient" or win["30d"]["status"] == "insufficient":
        score += 20
    return max(0, min(100, score))


def build_decisions(
    candidates: list[dict[str, Any]],
    experiments: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    counterfactual: dict[str, Any] | None,
    account_snapshot: dict[str, Any] | None,
    full_live_approvals: dict[str, dict[str, Any]] | None = None,
    post_approval_windows: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    experiments_by_key = group_by_key(experiments)
    review_by_key = {row_key(r): r for r in reviews if row_key(r)}
    account_by_strategy = account_risk_by_strategy(account_snapshot)
    full_live_approvals = full_live_approvals or {}
    post_approval_windows = post_approval_windows or {}

    records: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        key = candidate_key(candidate)
        if key:
            records[key] = dict(candidate)
    for exp in experiments:
        key = row_key(exp)
        if key and key not in records:
            records[key] = {
                "candidate_id": exp.get("candidate_id") or exp.get("experiment_id"),
                "strategy": exp.get("base_strategy"),
                "problem": exp.get("hypothesis") or exp.get("experiment_id"),
                "proposal": exp.get("change_type") or "shadow_experiment",
                "change_type": exp.get("change_type"),
                "family_id": exp.get("family_id"),
                "status": exp.get("promotion_status"),
            }
    for key, approval in full_live_approvals.items():
        if key and key not in records:
            records[key] = {
                "candidate_id": key,
                "strategy": approval.get("base_strategy") or approval.get("strategy"),
                "problem": approval.get("decision_reason") or approval.get("manual_action") or "approved_full_live",
                "proposal": approval.get("selected_live_parameter") or approval.get("manual_action") or "approved_full_live",
                "change_type": approval.get("change_type") or "approved_full_live",
                "family_id": approval.get("family_id") or "",
                "status": approval.get("manual_action") or "approve_full_live",
            }

    decisions: list[dict[str, Any]] = []
    for key, candidate in records.items():
        results = experiments_by_key.get(key, [])
        # Also attach results that use the same experiment id but no candidate id.
        if not results:
            same_family = str(candidate.get("family_id") or "")
            results = [r for r in experiments if same_family and r.get("family_id") == same_family]
        review = review_by_key.get(key)
        cf_rows = relevant_counterfactual_rows(candidate, counterfactual)
        cf_status, cf_metrics, cf_notes = counterfactual_verdict(candidate, cf_rows)
        strategy = str(candidate.get("strategy") or candidate.get("base_strategy") or "")
        acc_risk = account_by_strategy.get(strategy, {})
        full_live_approval = full_live_approvals.get(key)
        post_approval = post_approval_windows.get(key)
        status, action, blockers = classify_decision(candidate, results, review, cf_status, acc_risk, full_live_approval, post_approval)
        win = window_status(results)
        latest = results[-1] if results else {}
        ev_score = evidence_score(results, cf_status, win)
        rk_score = risk_score(results, cf_status, acc_risk, win)
        decisions.append(
            {
                "candidate_id": key,
                "family_id": candidate.get("family_id") or latest.get("family_id") or "",
                "strategy": strategy,
                "change_type": candidate.get("change_type") or latest.get("change_type") or "",
                "proposal": candidate.get("proposal") or candidate.get("problem") or latest.get("experiment_id") or "",
                "status": status,
                "priority": priority_from(status),
                "recommended_action": action,
                "evidence_score": ev_score,
                "risk_score": rk_score,
                "windows": win,
                "latest_experiment": {
                    "experiment_id": latest.get("experiment_id"),
                    "sample_window": latest.get("sample_window"),
                    "sample_trades": to_int(latest.get("sample_trades")),
                    "original_pnl": round(to_float(latest.get("original_pnl")), 4),
                    "shadow_pnl": round(to_float(latest.get("shadow_pnl")), 4),
                    "pnl_delta": round(to_float(latest.get("shadow_pnl")) - to_float(latest.get("original_pnl")), 4),
                    "avoided_loss": round(to_float(latest.get("avoided_loss")), 4),
                    "missed_profit": round(to_float(latest.get("missed_profit")), 4),
                    "hard_stop_before": to_int(latest.get("hard_stop_before")),
                    "hard_stop_after": to_int(latest.get("hard_stop_after")),
                    "promotion_status": latest.get("promotion_status"),
                },
                "counterfactual": {
                    "status": cf_status,
                    **cf_metrics,
                    "notes": cf_notes[:3],
                },
                "account_risk": acc_risk,
                "blockers": blockers[:5],
                "manual_review": review or {},
                "approved_full_live": bool(full_live_approval),
                "full_live_approval": full_live_approval or {},
                "post_approval_live": post_approval or {},
            }
        )

    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "REJECT": 4}
    return sorted(decisions, key=lambda r: (order.get(r["priority"], 9), -int(r.get("evidence_score") or 0), int(r.get("risk_score") or 0)))


def summarize_expansion_readiness(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for decision in decisions:
        if not decision.get("approved_full_live"):
            continue
        windows = ((decision.get("post_approval_live") or {}).get("windows") or {})
        day = windows.get("24h") or {}
        quality = day.get("quality") or {}
        closed = to_int(quality.get("closed_samples"))
        required = to_int(quality.get("required_closed_samples"))
        missing = max(required - closed, 0) if required else 0
        close_failed = to_int(day.get("close_failed"))
        open_failed = to_int(day.get("open_failed"))
        quality_label = str(quality.get("label") or day.get("status") or "unknown")
        if close_failed:
            action = "pause_and_review_close_loop"
        elif quality_label == "bad":
            action = "pause_expansion_review_quality"
        elif missing > 0:
            action = "continue_controlled_sampling"
        else:
            action = "ready_for_quality_review"
        items.append({
            "strategy": decision.get("strategy") or "",
            "candidate_id": decision.get("candidate_id") or "",
            "priority": decision.get("priority") or "",
            "status": decision.get("status") or "",
            "quality": quality_label,
            "closed_samples_24h": closed,
            "required_samples_24h": required,
            "missing_samples_24h": missing,
            "open_failed_24h": open_failed,
            "close_failed_24h": close_failed,
            "pnl_after_cost_24h": round(to_float(quality.get("realized_pnl_after_cost")), 4),
            "action": action,
        })
    ready = sum(1 for item in items if item["action"] == "ready_for_quality_review")
    maturing = sum(1 for item in items if item["action"] == "continue_controlled_sampling")
    pause = sum(1 for item in items if str(item["action"]).startswith("pause_"))
    total_missing = sum(int(item.get("missing_samples_24h") or 0) for item in items)
    top_gap = sorted(items, key=lambda item: int(item.get("missing_samples_24h") or 0), reverse=True)[:3]
    return {
        "approved_count": len(items),
        "ready_count": ready,
        "maturing_count": maturing,
        "pause_count": pause,
        "missing_samples_24h": total_missing,
        "top_gaps": top_gap,
        "items": items[:12],
    }


def build_payload(args: argparse.Namespace) -> dict[str, Any]:
    memory_dir = Path(args.memory_dir)
    experiments_dir = Path(args.experiments_dir)
    reports_dir = Path(args.reports_dir)
    runtime_dir = Path(args.runtime_dir)
    candidates = read_jsonl(memory_dir / "hypotheses" / "candidates_latest.jsonl")
    experiments = read_jsonl(experiments_dir / "results" / "windowed_latest.jsonl")
    if not experiments:
        experiments = read_jsonl(experiments_dir / "results" / "latest.jsonl")
    reviews = read_jsonl(memory_dir / "promotions" / "reviews_latest.jsonl")
    full_live_approvals = load_full_live_approvals(memory_dir)
    db_path = find_event_db(runtime_dir, args.db)
    post_approval_windows = summarize_post_approval_windows(db_path, full_live_approvals)
    counterfactual = read_json(reports_dir / "counterfactual_open_skips_latest.json")
    account_snapshot = read_json(runtime_dir / "account_snapshot_latest.json")
    decisions = build_decisions(candidates, experiments, reviews, counterfactual, account_snapshot, full_live_approvals, post_approval_windows)
    expansion_readiness = summarize_expansion_readiness(decisions)
    counts = {p: sum(1 for d in decisions if d.get("priority") == p) for p in ("P0", "P1", "P2", "P3", "REJECT")}
    top = next((d for d in decisions if d.get("priority") in {"P0", "P1", "P2"}), None)
    return {
        "generated_at": now_iso(),
        "version": "strategy_evolution_gate_v1",
        "sources": {
            "candidates": str(memory_dir / "hypotheses" / "candidates_latest.jsonl"),
            "experiments": str(experiments_dir / "results"),
            "reviews": str(memory_dir / "promotions" / "reviews_latest.jsonl"),
            "counterfactual": str(reports_dir / "counterfactual_open_skips_latest.json"),
            "account_snapshot": str(runtime_dir / "account_snapshot_latest.json"),
            "event_db": str(db_path or ""),
        },
        "summary": {
            "candidate_count": len(candidates),
            "experiment_count": len(experiments),
            "decision_count": len(decisions),
            "counts": counts,
            "top_priority": top.get("priority") if top else "P3",
            "top_candidate_id": top.get("candidate_id") if top else "",
            "top_status": top.get("status") if top else "none",
            "full_live_watch_count": sum(1 for d in decisions if d.get("approved_full_live")),
            "rollback_watch_count": sum(1 for d in decisions if d.get("status") in {"rollback_required", "rollback_watch"}),
            "post_approval_window_count": sum(1 for d in decisions if d.get("post_approval_live")),
            "expansion_readiness": expansion_readiness,
        },
        "decisions": decisions,
    }


def render_md(payload: dict[str, Any]) -> str:
    lines = [
        "# 策略进化统一门禁",
        "",
        f"- 生成时间: {payload.get('generated_at')}",
        f"- 候选: {payload['summary']['candidate_count']}，实验记录: {payload['summary']['experiment_count']}，门禁结论: {payload['summary']['decision_count']}",
        f"- 优先级: {payload['summary']['counts']}",
        f"- Full-live 观察: {payload['summary'].get('full_live_watch_count', 0)}，回滚观察/要求: {payload['summary'].get('rollback_watch_count', 0)}",
        f"- 放开后实盘窗口: {payload['summary'].get('post_approval_window_count', 0)} 个候选已生成 24h/72h/168h 观察窗",
        f"- 扩样成熟度: {payload['summary'].get('expansion_readiness', {})}",
        "",
        "| 优先级 | 状态 | 策略 | 候选 | 证据分 | 风险分 | 建议动作 | 关键阻塞 |",
        "|---|---|---|---|---:|---:|---|---|",
    ]
    for d in payload.get("decisions", []):
        blockers = "; ".join(d.get("blockers") or []) or "-"
        lines.append(
            f"| {d.get('priority')} | {d.get('status')} | {d.get('strategy')} | "
            f"{d.get('candidate_id')} | {d.get('evidence_score')} | {d.get('risk_score')} | "
            f"{d.get('recommended_action')} | {blockers} |"
        )
    return "\n".join(lines) + "\n"


def render_html(payload: dict[str, Any]) -> str:
    rows = "".join(
        f"""
<tr>
  <td><b>{h(d.get('priority'))}</b></td>
  <td>{h(d.get('status'))}</td>
  <td>{h(d.get('strategy'))}</td>
  <td>{h(d.get('candidate_id'))}</td>
  <td>{h(d.get('evidence_score'))}</td>
  <td>{h(d.get('risk_score'))}</td>
  <td>{h(d.get('recommended_action'))}</td>
  <td>{h('; '.join(d.get('blockers') or []) or '-')}</td>
</tr>
""".strip()
        for d in payload.get("decisions", [])
    ) or '<tr><td colspan="8">暂无候选</td></tr>'
    detail_rows = "".join(
        f"""
<article>
  <h2>{h(d.get('priority'))} {h(d.get('candidate_id'))}</h2>
  <p>{h(d.get('proposal'))}</p>
  <dl>
    <dt>策略</dt><dd>{h(d.get('strategy'))}</dd>
    <dt>状态</dt><dd>{h(d.get('status'))}</dd>
    <dt>影子PnL</dt><dd>{float((d.get('latest_experiment') or {}).get('original_pnl') or 0):+.2f} -> {float((d.get('latest_experiment') or {}).get('shadow_pnl') or 0):+.2f}</dd>
    <dt>反事实</dt><dd>{h((d.get('counterfactual') or {}).get('status'))} / samples={h((d.get('counterfactual') or {}).get('samples'))} / pnl={float((d.get('counterfactual') or {}).get('pnl') or 0):+.2f}</dd>
    <dt>窗口</dt><dd>{h(json.dumps(d.get('windows') or {}, ensure_ascii=False))}</dd>
    <dt>放开后实盘</dt><dd>{h(json.dumps((d.get('post_approval_live') or {}).get('windows') or {}, ensure_ascii=False))}</dd>
  </dl>
</article>
""".strip()
        for d in payload.get("decisions", [])[:8]
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>策略进化统一门禁</title>
<style>
body {{ margin:0; font-family:Arial, "Microsoft YaHei", sans-serif; color:#0f172a; background:#f6f8fb; }}
main {{ max-width:1180px; margin:0 auto; padding:28px 18px; }}
header, article, section {{ background:#fff; border:1px solid #e5e7eb; border-radius:8px; padding:18px; margin-bottom:14px; }}
h1 {{ margin:0 0 8px; font-size:28px; }}
h2 {{ margin:0 0 8px; font-size:18px; }}
p {{ color:#475467; line-height:1.6; }}
table {{ width:100%; border-collapse:collapse; min-width:900px; }}
th,td {{ padding:10px; border-bottom:1px solid #e7edf6; text-align:left; font-size:13px; }}
th {{ background:#f1f5f9; }}
.table-wrap {{ overflow:auto; }}
dl {{ display:grid; grid-template-columns:120px 1fr; gap:6px 12px; }}
dt {{ color:#667085; }}
dd {{ margin:0; }}
</style>
</head>
<body>
<main>
  <header>
    <h1>策略进化统一门禁</h1>
    <p>生成时间 {h(payload.get('generated_at'))}。只读裁判层：合并候选、影子实验、反事实、人工审批和账户风险，不改实盘。</p>
    <p>优先级统计：{h(payload.get('summary', {}).get('counts'))}</p>
  </header>
  <section class="table-wrap">
    <table>
      <thead><tr><th>优先级</th><th>状态</th><th>策略</th><th>候选</th><th>证据分</th><th>风险分</th><th>建议动作</th><th>关键阻塞</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </section>
  {detail_rows}
</main>
</body>
</html>"""


def write_outputs(payload: dict[str, Any], reports_dir: Path, runtime_dir: Path) -> None:
    write_json(runtime_dir / "strategy_evolution_latest.json", payload)
    write_json(reports_dir / "strategy_evolution_latest.json", payload)
    (reports_dir / "strategy_evolution_latest.md").write_text(render_md(payload), encoding="utf-8")
    (reports_dir / "strategy_evolution_latest.html").write_text(render_html(payload), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Unified read-only strategy evolution gate")
    parser.add_argument("--memory-dir", default=str(MEMORY_DIR))
    parser.add_argument("--experiments-dir", default=str(EXPERIMENTS_DIR))
    parser.add_argument("--reports-dir", default=str(REPORTS_DIR))
    parser.add_argument("--runtime-dir", default=str(RUNTIME_DIR))
    parser.add_argument("--db", default="", help="Optional event_store.sqlite3 path for post-approval live windows")
    args = parser.parse_args(argv)
    reports_dir = Path(args.reports_dir)
    runtime_dir = Path(args.runtime_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    payload = build_payload(args)
    write_outputs(payload, reports_dir, runtime_dir)
    counts = payload["summary"]["counts"]
    print(
        f"strategy_evolution_gate: decisions={payload['summary']['decision_count']} "
        f"P0={counts.get('P0', 0)} P1={counts.get('P1', 0)} P2={counts.get('P2', 0)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
