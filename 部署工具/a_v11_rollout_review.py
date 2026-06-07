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
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name == "部署工具" else SCRIPT_DIR
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.replay_fill import ReplayFillRequest, simulate_replay_fill
from core.replay_depth_cache import default_depth_cache_dirs, load_depth_snapshot
from core.replay_kline_source import load_research_store_kline_rows

CST = timezone(timedelta(hours=8))
WINDOWS_HOURS = (24, 72, 168)
DEFAULT_APPROVED_AT = "2026-05-29T11:59:40+08:00"
FEE_SLIPPAGE_PCT = 0.15
NOTIONAL_PER_TRADE = 400.0
ROLLBACK_REVIEW_LOSS_USDT = 80.0
COST_SENSITIVITY_PCTS = (0.10, 0.15, 0.25)
A_V11_TRAILING_ACTIVATE_ATR = {"15m": 1.0, "30m": 1.2}
A_V11_TRAILING_PULLBACK_ATR = {"15m": 1.0, "30m": 0.8}
KLINE_CACHE_LIMITS = (100, 200, 500, 1000)
DEPTH_CACHE_MAX_AGE_SEC = 300.0
TIMEFRAME_RE = re.compile(r"^(\d+)([mh])$")


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00").split(" [")[0]
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


def nested_payload_dicts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = [payload]
    for key in ("raw", "raw_event", "raw_signal", "signal"):
        value = payload.get(key)
        if isinstance(value, dict):
            items.append(value)
    raw = payload.get("raw")
    if isinstance(raw, dict):
        for key in ("raw_event", "raw_signal", "signal"):
            value = raw.get(key)
            if isinstance(value, dict):
                items.append(value)
    return items


def payload_value(payload: dict[str, Any], *keys: str) -> Any:
    for item in nested_payload_dicts(payload):
        for key in keys:
            value = item.get(key)
            if value not in (None, ""):
                return value
    return None


def payload_num(payload: dict[str, Any], *keys: str) -> float:
    return to_float(payload_value(payload, *keys))


def normalize_timeframe(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if text.isdigit():
        return f"{text}m"
    return text


def timeframe_ms(timeframe: str) -> int:
    match = TIMEFRAME_RE.match(normalize_timeframe(timeframe))
    if not match:
        return 0
    amount = int(match.group(1))
    unit = match.group(2)
    return amount * (60_000 if unit == "m" else 3_600_000)


def classify_exit_model(reason: str) -> str:
    text = str(reason or "").lower()
    if "交易所止盈止损自动平仓" in text or "exchange_auto" in text or "auto_close" in text:
        return "exchange_auto_close"
    if "最大亏损" in text or "硬顶" in text or "max_loss" in text or "hard_stop" in text:
        return "max_loss_guard"
    if "盈利回撤保护" in text or "profit_retrace" in text or "profit protect" in text:
        return "profit_retrace_guard"
    if "震荡平仓" in text or "range_exit" in text:
        return "range_exit"
    if "浮动止损" in text or "trailing" in text:
        return "atr_trailing_stop"
    if "止盈" in text or "take_profit" in text or "take profit" in text:
        return "take_profit"
    return "other"


def build_cost_sensitivity(realized_pnl: float, closed_samples: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for pct in COST_SENSITIVITY_PCTS:
        cost = closed_samples * NOTIONAL_PER_TRADE * pct / 100.0
        after_cost = realized_pnl - cost
        rows.append(
            {
                "cost_pct": pct,
                "estimated_cost_usdt": round(cost, 4),
                "pnl_after_cost_usdt": round(after_cost, 4),
                "rollback_review_loss_hit": after_cost <= -ROLLBACK_REVIEW_LOSS_USDT,
            }
        )
    return rows


def kline_cache_paths(symbol: str, timeframe: str) -> list[Path]:
    safe_symbol = str(symbol or "").upper()
    tf = normalize_timeframe(timeframe)
    paths: list[Path] = []
    for base in (ROOT, ROOT / "server_logs_tencent"):
        for limit in KLINE_CACHE_LIMITS:
            paths.append(base / "runtime" / "kline_cache" / f"{safe_symbol}_{tf}_{limit}.json")
    return paths


def load_cached_kline_rows(symbol: str, timeframe: str) -> tuple[list[list[Any]], str]:
    for path in kline_cache_paths(symbol, timeframe):
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        rows = payload.get("rows") if isinstance(payload, dict) else payload
        if isinstance(rows, list) and rows:
            return rows, str(path)
    return [], ""


def load_replay_kline_rows(symbol: str, timeframe: str, start: datetime, end: datetime) -> tuple[list[list[Any]], str]:
    rows, source = load_research_store_kline_rows(ROOT, symbol, timeframe, start=start, end=end)
    if rows:
        return rows, source
    return load_cached_kline_rows(symbol, timeframe)


def row_open_ms(row: list[Any]) -> int:
    try:
        return int(float(row[0]))
    except Exception:
        return 0


def rows_between(rows: list[list[Any]], timeframe: str, start: datetime, end: datetime) -> list[list[Any]]:
    step_ms = timeframe_ms(timeframe)
    if step_ms <= 0:
        return []
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    first_ms = (start_ms // step_ms) * step_ms
    return [row for row in rows if first_ms <= row_open_ms(row) <= end_ms]


def replay_bars(rows: list[list[Any]]) -> list[dict[str, Any]]:
    bars: list[dict[str, Any]] = []
    for row in rows:
        try:
            bars.append(
                {
                    "ts": datetime.fromtimestamp(int(float(row[0])) / 1000, tz=CST).isoformat(timespec="seconds"),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                }
            )
        except Exception:
            continue
    return bars


def quantity_from_payload(open_payload: dict[str, Any], close_payload: dict[str, Any], entry_price: float) -> float:
    qty = payload_num(close_payload, "exchange_qty", "quantity", "qty")
    if qty > 0:
        return qty
    qty = payload_num(open_payload, "exchange_qty", "quantity", "qty")
    if qty > 0:
        return qty
    leverage = payload_num(open_payload, "leverage") or payload_num(close_payload, "leverage") or 4.0
    if entry_price > 0:
        return NOTIONAL_PER_TRADE / max(entry_price, 1e-12)
    return NOTIONAL_PER_TRADE / max(leverage, 1.0)


def pair_open_close_rows(rows: list[sqlite3.Row], start: datetime, end: datetime) -> list[dict[str, Any]]:
    pending: dict[tuple[str, str, str], list[dict[str, Any]]] = collections.defaultdict(list)
    pairs: list[dict[str, Any]] = []
    for row in rows:
        event_dt = parse_dt(row["ts"])
        if not event_dt:
            continue
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except Exception:
            payload = {}
        event_type = str(row["event_type"] or "")
        symbol = str(row["symbol"] or payload_value(payload, "symbol") or "").upper()
        side = str(row["side"] or payload_value(payload, "side") or "").lower()
        timeframe = normalize_timeframe(payload_value(payload, "timeframe", "tf"))
        key = (symbol, side, timeframe)
        if event_type == "OPEN":
            pending[key].append({"row": row, "payload": payload, "ts": event_dt})
        elif event_type in {"CLOSE", "FORCED_CLOSE"} and start <= event_dt <= end:
            candidates = pending.get(key) or []
            matched_index = None
            entry_time = parse_dt(payload_value(payload, "entry_time"))
            for idx in range(len(candidates) - 1, -1, -1):
                candidate_ts = candidates[idx]["ts"]
                if candidate_ts <= event_dt and (entry_time is None or abs((candidate_ts - entry_time).total_seconds()) <= 600):
                    matched_index = idx
                    break
            if matched_index is None:
                for idx in range(len(candidates) - 1, -1, -1):
                    if candidates[idx]["ts"] <= event_dt:
                        matched_index = idx
                        break
            if matched_index is None:
                pairs.append({"status": "missing_open", "close_row": row, "close_payload": payload, "close_ts": event_dt})
                continue
            open_item = candidates.pop(matched_index)
            pairs.append(
                {
                    "status": "paired",
                    "open_row": open_item["row"],
                    "open_payload": open_item["payload"],
                    "open_ts": open_item["ts"],
                    "close_row": row,
                    "close_payload": payload,
                    "close_ts": event_dt,
                }
            )
    return pairs


def replay_trade_pair(pair: dict[str, Any]) -> dict[str, Any]:
    if pair.get("status") != "paired":
        close_payload = pair.get("close_payload") or {}
        close_row = pair.get("close_row")
        symbol = str(getattr(close_row, "__getitem__", lambda _: "")("symbol") or payload_value(close_payload, "symbol") or "").upper()
        side = str(getattr(close_row, "__getitem__", lambda _: "")("side") or payload_value(close_payload, "side") or "").lower()
        timeframe = normalize_timeframe(payload_value(close_payload, "timeframe", "tf"))
        close_ts = pair.get("close_ts")
        return {
            "status": pair.get("status") or "unpaired",
            "symbol": symbol,
            "side": side,
            "timeframe": timeframe,
            "close_ts": close_ts.isoformat(timespec="seconds") if isinstance(close_ts, datetime) else None,
            "entry_time_hint": str(payload_value(close_payload, "entry_time") or ""),
        }
    open_payload = pair.get("open_payload") or {}
    close_payload = pair.get("close_payload") or {}
    open_row = pair.get("open_row")
    close_row = pair.get("close_row")
    symbol = str(getattr(close_row, "__getitem__", lambda _: "")("symbol") or payload_value(close_payload, "symbol") or "").upper()
    side = str(getattr(close_row, "__getitem__", lambda _: "")("side") or payload_value(close_payload, "side") or "").lower()
    timeframe = normalize_timeframe(payload_value(close_payload, "timeframe", "tf") or payload_value(open_payload, "timeframe", "tf"))
    if timeframe not in A_V11_TRAILING_PULLBACK_ATR:
        return {"status": "unsupported_timeframe", "symbol": symbol, "side": side, "timeframe": timeframe}
    entry_ts = parse_dt(payload_value(close_payload, "entry_time")) or pair.get("open_ts")
    close_ts = pair.get("close_ts")
    entry_price = payload_num(close_payload, "entry_price") or payload_num(open_payload, "price", "entry_price")
    atr = payload_num(open_payload, "atr", "atr_at_entry")
    if not entry_ts or not close_ts:
        return {"status": "missing_time", "symbol": symbol, "side": side, "timeframe": timeframe}
    if entry_price <= 0:
        return {"status": "missing_entry_price", "symbol": symbol, "side": side, "timeframe": timeframe}
    if atr <= 0:
        return {"status": "missing_atr", "symbol": symbol, "side": side, "timeframe": timeframe}
    rows, source = load_replay_kline_rows(symbol, timeframe, entry_ts, close_ts)
    if not rows:
        return {"status": "missing_kline_data", "symbol": symbol, "side": side, "timeframe": timeframe}
    window_rows = rows_between(rows, timeframe, entry_ts, close_ts)
    bars = replay_bars(window_rows)
    if not bars:
        return {"status": "missing_bars", "symbol": symbol, "side": side, "timeframe": timeframe, "kline_source": source}
    stop_loss = payload_num(open_payload, "sl", "stop_loss")
    take_profit = payload_num(open_payload, "tp", "take_profit")
    quantity = quantity_from_payload(open_payload, close_payload, entry_price)
    if quantity <= 0:
        return {"status": "missing_quantity", "symbol": symbol, "side": side, "timeframe": timeframe, "kline_source": source}
    depth_snapshot = load_depth_snapshot(
        symbol,
        entry_ts,
        side=side,
        cache_dirs=default_depth_cache_dirs(ROOT),
        max_age_seconds=DEPTH_CACHE_MAX_AGE_SEC,
    )
    try:
        fill = simulate_replay_fill(
            ReplayFillRequest(
                symbol=symbol,
                side=side,
                entry_price=entry_price,
                quantity=quantity,
                stop_loss=stop_loss or None,
                take_profit=take_profit or None,
                atr=atr,
                trailing_activation_atr=A_V11_TRAILING_ACTIVATE_ATR[timeframe],
                trailing_stop_atr=A_V11_TRAILING_PULLBACK_ATR[timeframe],
                fee_bps=FEE_SLIPPAGE_PCT * 50.0,
                slippage_bps=0.0,
                entry_order_book=depth_snapshot.order_book if depth_snapshot else None,
            ),
            bars,
        )
    except Exception as exc:
        return {"status": "replay_error", "symbol": symbol, "side": side, "timeframe": timeframe, "error": str(exc)[:160]}
    actual_pnl = payload_num(close_payload, "pnl_usd", "pnl_usdt", "realized_pnl_usdt", "pnl")
    actual_exit = payload_num(close_payload, "exit_price")
    delta = fill.net_pnl_usdt - actual_pnl
    return {
        "status": "complete",
        "symbol": symbol,
        "side": side,
        "timeframe": timeframe,
        "entry_ts": entry_ts.isoformat(timespec="seconds"),
        "close_ts": close_ts.isoformat(timespec="seconds"),
        "entry_price": round(entry_price, 8),
        "actual_exit_price": round(actual_exit, 8),
        "replay_exit_price": fill.exit_price,
        "actual_pnl_usdt": round(actual_pnl, 4),
        "replay_pnl_usdt": round(fill.net_pnl_usdt, 4),
        "pnl_delta_usdt": round(delta, 4),
        "exit_reason": str(payload_text(close_payload, "reason", "close_reason") or ""),
        "replay_exit_reason": fill.exit_reason,
        "entry_fill_source": fill.entry_fill_source,
        "depth_slippage_usdt": fill.depth_slippage_usdt,
        "order_book_levels_used": fill.order_book_levels_used,
        "order_book_available_quantity": fill.order_book_available_quantity,
        "order_book_fill_ratio": fill.order_book_fill_ratio,
        "bars_held": fill.bars_held,
        "kline_source": source,
        "depth_snapshot_source": depth_snapshot.source if depth_snapshot else "",
        "depth_snapshot_age_seconds": round(depth_snapshot.age_seconds, 3) if depth_snapshot else None,
        "trailing_activation_atr": A_V11_TRAILING_ACTIVATE_ATR[timeframe],
        "trailing_stop_atr": A_V11_TRAILING_PULLBACK_ATR[timeframe],
    }


def build_replay_fill_comparison(rows: list[sqlite3.Row], start: datetime, end: datetime) -> dict[str, Any]:
    pairs = pair_open_close_rows(rows, start, end)
    results = [replay_trade_pair(pair) for pair in pairs]
    status_counts = collections.Counter(str(item.get("status") or "unknown") for item in results)
    completed = [item for item in results if item.get("status") == "complete"]
    incomplete_examples = [
        {
            "status": item.get("status"),
            "symbol": item.get("symbol"),
            "side": item.get("side"),
            "timeframe": item.get("timeframe"),
            "entry_ts": item.get("entry_ts"),
            "close_ts": item.get("close_ts"),
            "entry_time_hint": item.get("entry_time_hint"),
            "kline_source": item.get("kline_source"),
        }
        for item in results
        if item.get("status") != "complete"
    ][:12]
    deltas = [float(item.get("pnl_delta_usdt") or 0.0) for item in completed]
    replay_pnl = sum(float(item.get("replay_pnl_usdt") or 0.0) for item in completed)
    actual_pnl = sum(float(item.get("actual_pnl_usdt") or 0.0) for item in completed)
    by_exit_reason: collections.Counter[str] = collections.Counter(str(item.get("replay_exit_reason") or "") for item in completed)
    top_delta = sorted(completed, key=lambda item: abs(float(item.get("pnl_delta_usdt") or 0.0)), reverse=True)[:8]
    order_book_rows = [item for item in completed if item.get("entry_fill_source") == "order_book"]
    depth_ages = [float(item.get("depth_snapshot_age_seconds") or 0.0) for item in order_book_rows if item.get("depth_snapshot_age_seconds") is not None]
    return {
        "status": "ready" if completed else "missing_data",
        "window_since": start.isoformat(timespec="seconds"),
        "window_until": end.isoformat(timespec="seconds"),
        "paired_trades": len(pairs),
        "completed": len(completed),
        "completion_rate": round(len(completed) / max(1, len(pairs)), 4),
        "status_counts": dict(status_counts),
        "incomplete_examples": incomplete_examples,
        "actual_pnl_usdt": round(actual_pnl, 4),
        "replay_pnl_usdt": round(replay_pnl, 4),
        "pnl_delta_usdt": round(replay_pnl - actual_pnl, 4),
        "median_abs_delta_usdt": round(median([abs(v) for v in deltas]), 4) if deltas else 0.0,
        "replay_exit_reasons": [{"reason": k, "count": v} for k, v in by_exit_reason.most_common(8)],
        "order_book_fill_count": len(order_book_rows),
        "depth_snapshot_count": len(order_book_rows),
        "depth_slippage_usdt": round(sum(float(item.get("depth_slippage_usdt") or 0.0) for item in completed), 4),
        "avg_order_book_fill_ratio": round(mean_fill_ratio(order_book_rows), 4),
        "avg_depth_snapshot_age_seconds": round(sum(depth_ages) / len(depth_ages), 3) if depth_ages else 0.0,
        "top_deltas": top_delta,
        "note": "Uses local research_store/klines when available, then local kline cache; optional local runtime/depth_cache or research_store/depth_snapshots for entry fill; no Binance API call is made.",
    }


def mean_fill_ratio(rows: list[dict[str, Any]]) -> float:
    values = [float(item.get("order_book_fill_ratio") or 0.0) for item in rows]
    return sum(values) / len(values) if values else 0.0


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
        "exit_models": [],
        "cost_sensitivity": [],
        "side_pnl": {},
        "timeframe_pnl": {},
    }
    trades: list[dict[str, Any]] = []
    reason_counter: collections.Counter[str] = collections.Counter()
    exit_model_counter: collections.Counter[str] = collections.Counter()
    exit_model_pnl: collections.defaultdict[str, float] = collections.defaultdict(float)
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
            exit_model = classify_exit_model(reason)
            timeframe = payload_text(payload, "timeframe", "tf") or "-"
            side = str(row["side"] or payload_text(payload, "side") or "-").lower()
            metrics["realized_pnl_usdt"] += pnl
            if event_type == "FORCED_CLOSE":
                metrics["forced_close_pnl_usdt"] += pnl
            reason_counter[reason] += 1
            exit_model_counter[exit_model] += 1
            exit_model_pnl[exit_model] += pnl
            side_pnl[side] += pnl
            tf_pnl[timeframe] += pnl
            trades.append(
                {
                    "ts": row["ts"],
                    "symbol": row["symbol"],
                    "side": side,
                    "timeframe": timeframe,
                    "event_type": event_type,
                    "exit_model": exit_model,
                    "reason": reason,
                    "pnl_usdt": round(pnl, 4),
                }
            )
    closed = int(metrics["closes"]) + int(metrics["forced_closes"])
    realized_pnl = float(metrics["realized_pnl_usdt"])
    cost = closed * NOTIONAL_PER_TRADE * FEE_SLIPPAGE_PCT / 100.0
    metrics["estimated_cost_usdt"] = round(cost, 4)
    metrics["realized_pnl_usdt"] = round(realized_pnl, 4)
    metrics["forced_close_pnl_usdt"] = round(float(metrics["forced_close_pnl_usdt"]), 4)
    metrics["pnl_after_cost_usdt"] = round(realized_pnl - cost, 4)
    metrics["closed_samples"] = closed
    metrics["forced_close_rate"] = round(float(metrics["forced_closes"]) / max(1, closed), 4)
    metrics["open_failed_rate"] = round(float(metrics["open_failed"]) / max(1, int(metrics["opens"]) + int(metrics["open_failed"])), 4)
    metrics["top_losers"] = sorted(trades, key=lambda item: float(item["pnl_usdt"]))[:8]
    metrics["top_winners"] = sorted(trades, key=lambda item: float(item["pnl_usdt"]), reverse=True)[:5]
    metrics["close_reasons"] = [{"reason": k, "count": v} for k, v in reason_counter.most_common(8)]
    metrics["exit_models"] = [
        {"model": k, "count": v, "pnl_usdt": round(exit_model_pnl[k], 4)}
        for k, v in exit_model_counter.most_common(8)
    ]
    metrics["cost_sensitivity"] = build_cost_sensitivity(realized_pnl, closed)
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


def decision_packet(
    approval: dict[str, Any],
    windows: dict[str, dict[str, Any]],
    decision: dict[str, Any],
    replay_comparisons: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    day = windows.get("24h", {})
    three = windows.get("72h", {})
    week = windows.get("168h", {})
    closed72 = int(three.get("closed_samples") or 0)
    closed168 = int(week.get("closed_samples") or 0)
    if closed168 >= 100:
        maturity = "mature_168h"
    elif closed72 >= 50:
        maturity = "reviewable_72h"
    elif closed72 > 0:
        maturity = "thin_live_window"
    else:
        maturity = "insufficient_live_window"

    close_reasons = [str(item.get("reason") or "") for item in (three.get("close_reasons") or [])[:3]]
    exit_models = [
        "{model}({count}, {pnl:+.2f})".format(
            model=item.get("model") or "other",
            count=int(item.get("count") or 0),
            pnl=float(item.get("pnl_usdt") or 0),
        )
        for item in (three.get("exit_models") or [])[:3]
    ]
    cost_sensitivity = three.get("cost_sensitivity") or []
    conservative_cost = next(
        (item for item in cost_sensitivity if abs(float(item.get("cost_pct") or 0) - 0.25) < 0.000001),
        None,
    )
    top_loser = (three.get("top_losers") or [{}])[0]
    risks = [
        f"72h after-cost pnl {float(three.get('pnl_after_cost_usdt') or 0):+.2f} USDT",
        f"168h after-cost pnl {float(week.get('pnl_after_cost_usdt') or 0):+.2f} USDT",
        f"72h forced close rate {float(three.get('forced_close_rate') or 0):.1%}",
    ]
    if close_reasons:
        risks.append(f"top close reasons: {', '.join(close_reasons)}")
    if exit_models:
        risks.append(f"72h exit models: {', '.join(exit_models)}")
    if conservative_cost:
        risks.append(
            "72h after-cost pnl at 0.25% cost {pnl:+.2f} USDT".format(
                pnl=float(conservative_cost.get("pnl_after_cost_usdt") or 0)
            )
        )
    if top_loser.get("symbol"):
        risks.append(
            "top loser: {symbol} {side} {pnl:+.2f} USDT".format(
                symbol=top_loser.get("symbol"),
                side=top_loser.get("side") or "",
                pnl=float(top_loser.get("pnl_usdt") or 0),
            )
        )
    replay_comparisons = replay_comparisons or {}
    replay72 = replay_comparisons.get("72h") or {}
    if replay72:
        risks.append(
            "72h replay/fill comparison {completed}/{paired} complete, delta {delta:+.2f} USDT".format(
                completed=int(replay72.get("completed") or 0),
                paired=int(replay72.get("paired_trades") or 0),
                delta=float(replay72.get("pnl_delta_usdt") or 0),
            )
        )

    return {
        "change": "A/v11 approved trailing-pullback rollout",
        "live_parameter": approval.get("selected_live_parameter") or {},
        "expected_advantage": approval.get("decision_reason") or "approved full-live trailing-pullback candidate",
        "risk": risks,
        "evidence_maturity": {
            "label": maturity,
            "closed_24h": int(day.get("closed_samples") or 0),
            "closed_72h": closed72,
            "closed_168h": closed168,
        },
        "exit_model_summary_72h": three.get("exit_models") or [],
        "cost_sensitivity_72h": cost_sensitivity,
        "replay_fill_comparison_72h": replay72,
        "rollback_path": [
            "keep automatic rollback disabled",
            "if operator approves, revert A/v11 trailing pullback parameter/release to previous stable version",
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
    replay_comparisons = {
        f"{hours}h": build_replay_fill_comparison(rows, max(approved_at, now - timedelta(hours=hours)), now)
        for hours in WINDOWS_HOURS
    }
    for label, comparison in replay_comparisons.items():
        if label in windows:
            windows[label]["replay_fill_comparison"] = comparison
    decision = verdict(windows)
    packet = decision_packet(approval, windows, decision, replay_comparisons)
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "strategy": "A/v11",
        "db": str(db),
        "approved_at": approved_at.isoformat(timespec="seconds"),
        "candidate_ids": approval.get("candidate_ids") or [],
        "selected_live_parameter": approval.get("selected_live_parameter") or {},
        "decision": decision,
        "decision_packet": packet,
        "replay_fill_comparison": replay_comparisons,
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
    packet = payload.get("decision_packet") or {}
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
        ]
    )
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
    lines.extend(
        [
            "",
            "## 72h Exit Models",
            "",
            "| Exit model | Count | PnL |",
            "| --- | ---: | ---: |",
        ]
    )
    for item in ((payload.get("windows") or {}).get("72h") or {}).get("exit_models") or []:
        lines.append(
            f"| {item.get('model') or '-'} | {int(item.get('count') or 0)} | "
            f"{float(item.get('pnl_usdt') or 0):+.2f} |"
        )
    lines.extend(
        [
            "",
            "## 72h Cost Sensitivity",
            "",
            "| Cost pct | Cost | After cost | Rollback review hit |",
            "| ---: | ---: | ---: | --- |",
        ]
    )
    for item in ((payload.get("windows") or {}).get("72h") or {}).get("cost_sensitivity") or []:
        lines.append(
            f"| {float(item.get('cost_pct') or 0):.2f}% | {float(item.get('estimated_cost_usdt') or 0):.2f} | "
            f"{float(item.get('pnl_after_cost_usdt') or 0):+.2f} | "
            f"{'yes' if item.get('rollback_review_loss_hit') else 'no'} |"
        )
    replay72 = ((payload.get("replay_fill_comparison") or {}).get("72h") or {})
    lines.extend(
        [
            "",
            "## 72h Replay Fill Comparison",
            "",
            f"- Status: `{replay72.get('status') or 'missing_data'}`",
            f"- Paired/completed: `{int(replay72.get('paired_trades') or 0)}/{int(replay72.get('completed') or 0)}`",
            f"- Actual vs replay PnL: `{float(replay72.get('actual_pnl_usdt') or 0):+.2f}` / `{float(replay72.get('replay_pnl_usdt') or 0):+.2f}` USDT",
            f"- Delta: `{float(replay72.get('pnl_delta_usdt') or 0):+.2f}` USDT; median abs delta `{float(replay72.get('median_abs_delta_usdt') or 0):.2f}`",
            f"- Depth entry fills: `{int(replay72.get('order_book_fill_count') or 0)}`; depth slippage `{float(replay72.get('depth_slippage_usdt') or 0):.2f}` USDT; avg depth age `{float(replay72.get('avg_depth_snapshot_age_seconds') or 0):.1f}s`",
            f"- Status counts: `{json.dumps(replay72.get('status_counts') or {}, ensure_ascii=False)}`",
            f"- Incomplete examples: `{json.dumps((replay72.get('incomplete_examples') or [])[:5], ensure_ascii=False)}`",
            f"- Note: {replay72.get('note') or 'local research_store/klines when available, then kline/depth cache; no Binance API call'}",
            "",
            "| Symbol | Side | TF | Fill | Actual PnL | Replay PnL | Delta | Actual exit | Replay exit | Replay reason |",
            "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for item in replay72.get("top_deltas") or []:
        lines.append(
            f"| {item.get('symbol') or '-'} | {item.get('side') or '-'} | {item.get('timeframe') or '-'} | "
            f"{item.get('entry_fill_source') or 'synthetic'} | "
            f"{float(item.get('actual_pnl_usdt') or 0):+.2f} | {float(item.get('replay_pnl_usdt') or 0):+.2f} | "
            f"{float(item.get('pnl_delta_usdt') or 0):+.2f} | {float(item.get('actual_exit_price') or 0):.6g} | "
            f"{float(item.get('replay_exit_price') or 0):.6g} | {item.get('replay_exit_reason') or '-'} |"
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
