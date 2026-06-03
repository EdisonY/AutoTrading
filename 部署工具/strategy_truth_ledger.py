"""Strategy Truth Ledger - separate active strategy PnL from recovery positions.

Reads from SQLite event_store.sqlite3 and account_snapshots to produce:
- runtime/strategy_truth_latest.json
- reports/strategy_truth_latest.md
- Optional SQLite tables for aggregation

Run on Aliyun analysis node after syncing data from Tencent.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.replay_fill import ReplayFillRequest, simulate_replay_fill
from core.replay_kline_source import load_research_store_kline_rows

CST = timezone(timedelta(hours=8))
KLINE_CACHE_LIMITS = (100, 200, 500, 1000)
RECOVERY_REPLAY_TIMEFRAMES = ("15m", "30m", "1h")

STRATEGY_MAP = {
    "A/v11": {"account": "A", "name": "半木夏"},
    "B/v16": {"account": "B", "name": "订单流"},
    "C/v14": {"account": "C", "name": "四维度"},
}

FEE_RATE_TAKER = 0.0005  # 0.05% taker fee per side

RECOVERY_STRATEGY_EXIT_PROFILES = {
    "A/v11": {
        "min_mfe_pct_on_margin": 2.0,
        "trailing_watch_drawdown_pct": 2.0,
        "mfe_drawdown_review_pct": 4.0,
    },
    "B/v16": {
        "min_mfe_pct_on_margin": 3.0,
        "trailing_watch_drawdown_pct": 3.0,
        "mfe_drawdown_review_pct": 6.0,
    },
    "C/v14": {
        "min_mfe_pct_on_margin": 3.0,
        "trailing_watch_drawdown_pct": 3.0,
        "mfe_drawdown_review_pct": 7.0,
    },
    "default": {
        "min_mfe_pct_on_margin": 2.5,
        "trailing_watch_drawdown_pct": 2.5,
        "mfe_drawdown_review_pct": 5.0,
    },
}


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST)
        return dt.astimezone(CST)
    except Exception:
        return None


def safe_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val) if val is not None else default
    except (ValueError, TypeError):
        return default


def payload_float(payload: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
    for key in keys:
        if key in payload:
            return safe_float(payload.get(key), default)
        if key in raw:
            return safe_float(raw.get(key), default)
    return default


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


def payload_value(payload: dict[str, Any], *keys: str) -> Any:
    """Read top-level or one-level nested raw event/signal values."""
    candidates = [payload]
    for nested_key in ("raw", "raw_signal", "raw_event"):
        nested = payload.get(nested_key)
        if isinstance(nested, dict):
            candidates.append(nested)
            raw_nested = nested.get("raw")
            if isinstance(raw_nested, dict):
                candidates.append(raw_nested)
    for source in candidates:
        for key in keys:
            if key in source:
                return source.get(key)
    return None


def safe_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y"}:
        return True
    if text in {"0", "false", "no", "n"}:
        return False
    return None


def load_open_events(con: sqlite3.Connection, days: int = 30) -> list[dict[str, Any]]:
    """Load OPEN events from the events table."""
    cutoff = (datetime.now(CST) - timedelta(days=days)).isoformat()
    rows = con.execute(
        """SELECT id, ts, strategy, symbol, side, score, payload_json
           FROM events
           WHERE event_type = 'OPEN' AND ts >= ?
           ORDER BY ts""",
        (cutoff,),
    ).fetchall()
    events = []
    for row in rows:
        payload = json.loads(row[6]) if row[6] else {}
        events.append({
            "id": row[0],
            "ts": row[1],
            "strategy": row[2],
            "symbol": row[3],
            "side": row[4],
            "score": safe_float(row[5]),
            "entry_price": safe_float(payload.get("price")),
            "leverage": safe_float(payload.get("leverage"), 4.0),
            "atr": safe_float(payload.get("atr")),
            "reasons": payload.get("reasons", ""),
        })
    return events


def load_close_events(con: sqlite3.Connection, days: int = 30) -> list[dict[str, Any]]:
    """Load CLOSE and FORCED_CLOSE events."""
    cutoff = (datetime.now(CST) - timedelta(days=days)).isoformat()
    rows = con.execute(
        """SELECT id, ts, strategy, symbol, event_type, side, payload_json
           FROM events
           WHERE event_type IN ('CLOSE', 'FORCED_CLOSE') AND ts >= ?
           ORDER BY ts""",
        (cutoff,),
    ).fetchall()
    events = []
    for row in rows:
        payload = json.loads(row[6]) if row[6] else {}
        events.append({
            "id": row[0],
            "ts": row[1],
            "strategy": row[2],
            "symbol": row[3],
            "event_type": row[4],
            "side": row[5],
            "exit_price": payload_float(payload, "exit_price", "price", "close_price"),
            "pnl_usd": payload_float(payload, "pnl_usd", "pnl_usdt", "realized_pnl_usdt", "pnl"),
            "pnl_pct": payload_float(payload, "pnl_pct", "pnl_percent", "return_pct"),
            "reason": payload_text(payload, "reason", "close_reason"),
            "entry_price": payload_float(payload, "entry_price"),
            "entry_time": payload_text(payload, "entry_time"),
        })
    return events


def load_latest_snapshots(con: sqlite3.Connection) -> list[dict[str, Any]]:
    """Load the latest account snapshots with positions."""
    rows = con.execute(
        """SELECT id, ts, account, wallet_usdt, margin_usdt, available_usdt,
                  unrealized_pnl_usdt, open_positions, payload_json
           FROM account_snapshots
           ORDER BY id DESC LIMIT 30"""
    ).fetchall()
    # Group by account, take latest per account
    by_account: dict[str, dict] = {}
    for row in rows:
        acct = row[2]
        if acct in by_account:
            continue
        payload = json.loads(row[8]) if row[8] else {}
        positions = payload.get("positions", [])
        by_account[acct] = {
            "ts": row[1],
            "account": acct,
            "wallet_usdt": safe_float(row[3]),
            "margin_usdt": safe_float(row[4]),
            "available_usdt": safe_float(row[5]),
            "unrealized_pnl_usdt": safe_float(row[6]),
            "open_positions": int(row[7] or 0),
            "positions": positions,
        }
    return list(by_account.values())


def load_snapshot_history(con: sqlite3.Connection, days: int = 30) -> list[dict[str, Any]]:
    """Load account snapshot position history for read-only recovery-path review."""
    cutoff = (datetime.now(CST) - timedelta(days=days)).isoformat()
    rows = con.execute(
        """SELECT ts, account, payload_json
           FROM account_snapshots
           WHERE ts >= ?
           ORDER BY ts ASC, id ASC""",
        (cutoff,),
    ).fetchall()
    history: list[dict[str, Any]] = []
    for ts, account, payload_json in rows:
        try:
            payload = json.loads(payload_json) if payload_json else {}
        except Exception:
            payload = {}
        positions = payload.get("positions") if isinstance(payload.get("positions"), list) else []
        for pos in positions:
            margin = safe_float(pos.get("margin"))
            upnl = safe_float(pos.get("upnl"))
            entry = safe_float(pos.get("entry"))
            mark = safe_float(pos.get("mark"))
            side = str(pos.get("side") or "").lower()
            directional_return = 0.0
            if entry > 0 and mark > 0:
                directional_return = (entry - mark) / entry * 100 if side == "short" else (mark - entry) / entry * 100
            history.append(
                {
                    "ts": ts,
                    "account": account,
                    "symbol": str(pos.get("symbol") or ""),
                    "side": side,
                    "margin": margin,
                    "unrealized_pnl": upnl,
                    "unrealized_pnl_pct_on_margin": upnl / margin * 100 if margin > 0 else 0.0,
                    "directional_return_pct": directional_return,
                }
            )
    return history


def normalize_side(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"long", "buy"}:
        return "long"
    if text in {"short", "sell"}:
        return "short"
    return text


def opposite_side(side: str) -> str:
    return "short" if normalize_side(side) == "long" else "long" if normalize_side(side) == "short" else ""


def load_recovery_signal_events(con: sqlite3.Connection, days: int = 30) -> list[dict[str, Any]]:
    """Load read-only signal/reject evidence for recovery-position review."""
    cutoff = (datetime.now(CST) - timedelta(days=days)).isoformat()
    rows = con.execute(
        """SELECT id, ts, strategy, symbol, event_type, side, score, stage, layer, reason, payload_json
           FROM events
           WHERE event_type IN ('SIGNAL', 'SIGNAL_ONLY', 'OPEN_SKIPPED')
             AND ts >= ?
           ORDER BY ts ASC, id ASC""",
        (cutoff,),
    ).fetchall()
    events: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row[10]) if row[10] else {}
        except Exception:
            payload = {}
        side = normalize_side(
            row[5]
            or payload_value(payload, "trade_side", "side")
            or payload_value(payload, "recommended_side", "signal_side")
        )
        if side not in {"long", "short"}:
            continue
        events.append(
            {
                "id": row[0],
                "ts": row[1],
                "strategy": row[2],
                "symbol": row[3],
                "event_type": row[4],
                "side": side,
                "score": safe_float(row[6], safe_float(payload_value(payload, "net_score", "score", "raw_score"))),
                "timeframe": str(payload_value(payload, "timeframe", "tf") or ""),
                "can_trade": safe_bool(payload_value(payload, "can_trade")),
                "stage": row[7],
                "layer": row[8],
                "reason": row[9] or str(payload_value(payload, "reason", "skip_reason") or ""),
            }
        )
    return events


def compact_signal_event(event: dict[str, Any] | None) -> dict[str, Any]:
    if not event:
        return {}
    return {
        "ts": event.get("ts"),
        "event_type": event.get("event_type"),
        "side": event.get("side"),
        "score": round(safe_float(event.get("score")), 2),
        "timeframe": event.get("timeframe") or "",
        "can_trade": event.get("can_trade"),
        "stage": event.get("stage") or "",
        "layer": event.get("layer") or "",
        "reason": event.get("reason") or "",
    }


def attach_recovery_signal_evidence(
    recovery: list[dict[str, Any]],
    signal_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach same-strategy same/opposite signal facts to recovery positions."""
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for event in signal_events:
        key = (str(event.get("strategy") or ""), str(event.get("symbol") or ""))
        by_key.setdefault(key, []).append(event)

    for pos in recovery:
        strategy = str(pos.get("strategy") or "")
        symbol = str(pos.get("symbol") or "")
        side = normalize_side(pos.get("side"))
        first_seen = parse_dt(pos.get("first_seen_ts") or pos.get("snapshot_ts"))
        candidates = by_key.get((strategy, symbol), [])
        if first_seen:
            candidates = [event for event in candidates if (parse_dt(event.get("ts")) or first_seen) >= first_seen]
        same = [event for event in candidates if normalize_side(event.get("side")) == side]
        opposite = [event for event in candidates if normalize_side(event.get("side")) == opposite_side(side)]
        same_open_like = [
            event
            for event in same
            if event.get("event_type") == "OPEN_SKIPPED" or event.get("can_trade") is True
        ]
        opposite_open_like = [
            event
            for event in opposite
            if event.get("event_type") == "OPEN_SKIPPED" or event.get("can_trade") is True
        ]
        pos["same_strategy_signal_count"] = len(same)
        pos["opposite_signal_count"] = len(opposite)
        pos["same_strategy_open_like_count"] = len(same_open_like)
        pos["opposite_open_like_count"] = len(opposite_open_like)
        pos["latest_same_strategy_signal"] = compact_signal_event(same[-1] if same else None)
        pos["latest_opposite_signal"] = compact_signal_event(opposite[-1] if opposite else None)
        if opposite_open_like:
            pos["signal_shadow_action"] = "opposite_signal_review"
        elif same_open_like:
            pos["signal_shadow_action"] = "same_strategy_reopen_supported"
        elif opposite:
            pos["signal_shadow_action"] = "weak_opposite_signal_seen"
        elif same:
            pos["signal_shadow_action"] = "weak_same_strategy_signal_seen"
        else:
            pos["signal_shadow_action"] = "no_recent_same_strategy_signal"
    return recovery


def match_trades(
    open_events: list[dict],
    close_events: list[dict],
) -> list[dict[str, Any]]:
    """Match OPEN events with CLOSE events to create trade records."""
    # Index close events by (strategy, symbol, side)
    close_index: dict[tuple, list[dict]] = {}
    for ce in close_events:
        key = (ce["strategy"], ce["symbol"], ce["side"])
        close_index.setdefault(key, []).append(ce)

    trades = []
    for oe in open_events:
        key = (oe["strategy"], oe["symbol"], oe["side"])
        closes = close_index.get(key, [])

        # Find the earliest close after this open
        matched_close = None
        for ce in closes:
            if ce["ts"] >= oe["ts"]:
                matched_close = ce
                break

        entry_dt = parse_dt(oe["ts"])
        if matched_close:
            exit_dt = parse_dt(matched_close["ts"])
            holding_minutes = (exit_dt - entry_dt).total_seconds() / 60 if entry_dt and exit_dt else 0
            pnl = safe_float(matched_close["pnl_usd"])
            # Estimate fee: entry + exit, based on notional
            entry_price = oe["entry_price"]
            notional = 100.0 * oe["leverage"]  # approximate
            fee = notional * FEE_RATE_TAKER * 2 if entry_price else 0

            trades.append({
                "strategy": oe["strategy"],
                "symbol": oe["symbol"],
                "side": oe["side"],
                "entry_time": oe["ts"],
                "exit_time": matched_close["ts"],
                "holding_minutes": round(holding_minutes, 1),
                "entry_price": entry_price,
                "exit_price": matched_close["exit_price"],
                "score": oe["score"],
                "pnl_usd": pnl,
                "pnl_pct": safe_float(matched_close["pnl_pct"]),
                "fee_estimate": round(fee, 2),
                "net_pnl": round(pnl - fee, 2),
                "close_reason": matched_close["reason"],
                "close_type": matched_close["event_type"],
                "is_active_trade": True,
                "is_recovery": False,
                "is_open": False,
            })
        else:
            # Still open
            trades.append({
                "strategy": oe["strategy"],
                "symbol": oe["symbol"],
                "side": oe["side"],
                "entry_time": oe["ts"],
                "exit_time": None,
                "holding_minutes": None,
                "entry_price": oe["entry_price"],
                "exit_price": None,
                "score": oe["score"],
                "pnl_usd": None,
                "pnl_pct": None,
                "fee_estimate": 0,
                "net_pnl": None,
                "close_reason": None,
                "close_type": None,
                "is_active_trade": True,
                "is_recovery": False,
                "is_open": True,
            })

    return trades


def identify_recovery_positions(
    snapshots: list[dict],
    open_events: list[dict],
) -> list[dict[str, Any]]:
    """Identify positions in snapshots that have no matching OPEN event."""
    # Build set of (strategy, symbol, side) from recent OPEN events
    open_keys = set()
    for oe in open_events:
        open_keys.add((oe["strategy"], oe["symbol"], oe["side"]))

    # Also map account -> strategy
    account_to_strategy = {v["account"]: k for k, v in STRATEGY_MAP.items()}

    recovery = []
    for snap in snapshots:
        acct = snap["account"]
        strategy = account_to_strategy.get(acct, acct)
        for pos in snap["positions"]:
            sym = pos.get("symbol", "")
            side = pos.get("side", "").lower()
            key = (strategy, sym, side)
            if key not in open_keys:
                recovery.append({
                    "strategy": strategy,
                    "symbol": sym,
                    "side": side,
                    "account": acct,
                    "entry_price": safe_float(pos.get("entry")),
                    "mark_price": safe_float(pos.get("mark")),
                    "qty": safe_float(pos.get("qty")),
                    "leverage": safe_float(pos.get("lev"), 4.0),
                    "notional": safe_float(pos.get("notional")),
                    "margin": safe_float(pos.get("margin")),
                    "unrealized_pnl": safe_float(pos.get("upnl")),
                    "snapshot_ts": snap["ts"],
                    "is_active_trade": False,
                    "is_recovery": True,
                    "is_open": True,
                })
    return recovery


def enrich_recovery_path_metrics(
    recovery: list[dict[str, Any]],
    snapshot_history: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach first-seen, MFE/MAE, and drawdown facts to recovery positions."""
    by_key: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in snapshot_history:
        key = (
            str(row.get("account") or ""),
            str(row.get("symbol") or ""),
            str(row.get("side") or "").lower(),
        )
        by_key.setdefault(key, []).append(row)

    for pos in recovery:
        key = (
            str(pos.get("account") or ""),
            str(pos.get("symbol") or ""),
            str(pos.get("side") or "").lower(),
        )
        rows = by_key.get(key, [])
        pct_values = [safe_float(row.get("unrealized_pnl_pct_on_margin")) for row in rows]
        return_values = [safe_float(row.get("directional_return_pct")) for row in rows]
        margin = safe_float(pos.get("margin"))
        current_pct = safe_float(pos.get("unrealized_pnl")) / margin * 100 if margin > 0 else 0.0
        if pct_values:
            mfe_pct = max(pct_values)
            mae_pct = min(pct_values)
            pos["first_seen_ts"] = rows[0].get("ts")
            pos["path_samples"] = len(rows)
            pos["mfe_pct_on_margin"] = round(mfe_pct, 2)
            pos["mae_pct_on_margin"] = round(mae_pct, 2)
            pos["drawdown_from_mfe_pct_on_margin"] = round(current_pct - mfe_pct, 2)
        else:
            pos["first_seen_ts"] = pos.get("snapshot_ts")
            pos["path_samples"] = 0
            pos["mfe_pct_on_margin"] = 0.0
            pos["mae_pct_on_margin"] = 0.0
            pos["drawdown_from_mfe_pct_on_margin"] = 0.0
        pos["mfe_price_pct"] = round(max(return_values), 4) if return_values else 0.0
        pos["mae_price_pct"] = round(min(return_values), 4) if return_values else 0.0
    return recovery


def evaluate_recovery_exit_policies(recovery: list[dict]) -> dict[str, Any]:
    """Shadow-test candidate exit policies for recovery positions."""
    now = datetime.now(CST)
    policies = {
        "age_4h": {"label": "4小时时间退出", "would_exit": 0, "would_hold": 0},
        "age_8h": {"label": "8小时时间退出", "would_exit": 0, "would_hold": 0},
        "age_24h": {"label": "24小时时间退出", "would_exit": 0, "would_hold": 0},
        "trailing_2pct": {"label": "2%回撤退出", "would_exit": 0, "would_hold": 0},
        "opposite_signal": {"label": "反向信号退出", "would_exit": 0, "would_hold": 0},
    }
    for pos in recovery:
        snap_dt = parse_dt(pos.get("snapshot_ts"))
        age_hours = (now - snap_dt).total_seconds() / 3600 if snap_dt else 0
        upnl = pos.get("unrealized_pnl", 0)
        margin = pos.get("margin", 0)
        upnl_pct = (upnl / margin * 100) if margin > 0 else 0

        # Age-based exits
        if age_hours >= 4:
            policies["age_4h"]["would_exit"] += 1
        else:
            policies["age_4h"]["would_hold"] += 1
        if age_hours >= 8:
            policies["age_8h"]["would_exit"] += 1
        else:
            policies["age_8h"]["would_hold"] += 1
        if age_hours >= 24:
            policies["age_24h"]["would_exit"] += 1
        else:
            policies["age_24h"]["would_hold"] += 1

        # Trailing stop after adoption (2% drawdown from entry)
        if upnl_pct < -2:
            policies["trailing_2pct"]["would_exit"] += 1
        else:
            policies["trailing_2pct"]["would_hold"] += 1

        if int(pos.get("opposite_open_like_count") or 0) > 0:
            policies["opposite_signal"]["would_exit"] += 1
        else:
            policies["opposite_signal"]["would_hold"] += 1

    return policies


def recovery_strategy_exit_profile(strategy: str) -> dict[str, float]:
    profile = RECOVERY_STRATEGY_EXIT_PROFILES.get(strategy) or RECOVERY_STRATEGY_EXIT_PROFILES["default"]
    return {key: safe_float(value) for key, value in profile.items()}


def normalize_timeframe(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"15", "15min", "15m"}:
        return "15m"
    if text in {"30", "30min", "30m"}:
        return "30m"
    if text in {"60", "60min", "60m", "1h"}:
        return "1h"
    return text


def timeframe_ms(timeframe: str) -> int:
    tf = normalize_timeframe(timeframe)
    if not tf:
        return 0
    try:
        unit = tf[-1]
        value = int(tf[:-1])
    except Exception:
        return 0
    if unit == "m":
        return value * 60_000
    if unit == "h":
        return value * 60 * 60_000
    return 0


def kline_cache_paths(symbol: str, timeframe: str) -> list[Path]:
    safe_symbol = str(symbol or "").upper().replace("/", "_")
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


def preferred_recovery_replay_timeframes(pos: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    for key in ("latest_same_strategy_signal", "latest_opposite_signal"):
        signal = pos.get(key)
        if isinstance(signal, dict):
            tf = normalize_timeframe(signal.get("timeframe"))
            if tf:
                candidates.append(tf)
    candidates.extend(RECOVERY_REPLAY_TIMEFRAMES)
    out: list[str] = []
    for tf in candidates:
        if tf and tf not in out:
            out.append(tf)
    return out


def recovery_replay_quantity(pos: dict[str, Any], entry_price: float) -> float:
    qty = abs(safe_float(pos.get("qty")))
    if qty > 0:
        return qty
    notional = abs(safe_float(pos.get("notional")))
    if entry_price > 0 and notional > 0:
        return notional / entry_price
    margin = abs(safe_float(pos.get("margin")))
    leverage = max(1.0, safe_float(pos.get("leverage"), 4.0))
    if entry_price > 0 and margin > 0:
        return margin * leverage / entry_price
    return 0.0


def build_recovery_bar_replay_evidence(pos: dict[str, Any]) -> dict[str, Any]:
    """Replay a recovery-position path through local research_store/cache Klines."""
    strategy = str(pos.get("strategy") or "")
    symbol = str(pos.get("symbol") or "").upper()
    side = normalize_side(pos.get("side"))
    entry_price = safe_float(pos.get("entry_price"))
    snapshot_ts = parse_dt(pos.get("snapshot_ts"))
    first_seen = parse_dt(pos.get("first_seen_ts") or pos.get("snapshot_ts"))
    leverage = max(1.0, safe_float(pos.get("leverage"), 4.0))
    profile = recovery_strategy_exit_profile(strategy)
    activation_pct = profile["min_mfe_pct_on_margin"] / leverage
    trailing_pct = profile["trailing_watch_drawdown_pct"] / leverage
    quantity = recovery_replay_quantity(pos, entry_price)
    attempts: list[dict[str, Any]] = []

    if not symbol or side not in {"long", "short"}:
        return {"status": "missing_identity", "action": "replay_data_gap", "automation": "disabled_report_only"}
    if not first_seen or not snapshot_ts or snapshot_ts < first_seen:
        return {"status": "missing_time", "action": "replay_data_gap", "automation": "disabled_report_only"}
    if entry_price <= 0:
        return {"status": "missing_entry_price", "action": "replay_data_gap", "automation": "disabled_report_only"}
    if quantity <= 0:
        return {"status": "missing_quantity", "action": "replay_data_gap", "automation": "disabled_report_only"}

    for timeframe in preferred_recovery_replay_timeframes(pos):
        rows, source = load_replay_kline_rows(symbol, timeframe, first_seen, snapshot_ts)
        if not rows:
            attempts.append({"timeframe": timeframe, "status": "missing_kline_data"})
            continue
        window_rows = rows_between(rows, timeframe, first_seen, snapshot_ts)
        bars = replay_bars(window_rows)
        if not bars:
            attempts.append({"timeframe": timeframe, "status": "missing_bars", "kline_source": source})
            continue
        try:
            fill = simulate_replay_fill(
                ReplayFillRequest(
                    symbol=symbol,
                    side=side,
                    entry_price=entry_price,
                    quantity=quantity,
                    trailing_stop_pct=trailing_pct,
                    trailing_activation_pct=activation_pct,
                    fee_bps=FEE_RATE_TAKER * 10_000,
                    slippage_bps=0.0,
                ),
                bars,
            )
        except Exception as exc:
            attempts.append({"timeframe": timeframe, "status": "replay_error", "error": str(exc)[:160]})
            continue
        current_upnl = safe_float(pos.get("unrealized_pnl"))
        action = "bar_replay_exit_manual_review" if fill.exit_reason != "end_of_window" else "bar_replay_hold_bias"
        return {
            "status": "complete",
            "action": action,
            "symbol": symbol,
            "side": side,
            "timeframe": timeframe,
            "entry_ts": first_seen.isoformat(timespec="seconds"),
            "snapshot_ts": snapshot_ts.isoformat(timespec="seconds"),
            "entry_price": round(entry_price, 8),
            "replay_exit_price": fill.exit_price,
            "replay_exit_reason": fill.exit_reason,
            "replay_exit_ts": fill.exit_ts,
            "replay_pnl_usdt": round(fill.net_pnl_usdt, 4),
            "current_unrealized_pnl_usdt": round(current_upnl, 4),
            "pnl_delta_vs_current_usdt": round(fill.net_pnl_usdt - current_upnl, 4),
            "bars_held": fill.bars_held,
            "kline_source": source,
            "trailing_activation_pct": round(activation_pct, 4),
            "trailing_stop_pct": round(trailing_pct, 4),
            "thresholds_on_margin": profile,
            "automation": "disabled_report_only",
            "note": "local research_store/klines when available, then kline cache; no Binance API call; recovery entry time is first-seen snapshot, not original exchange open time",
        }
    return {
        "status": "missing_data",
        "action": "replay_data_gap",
        "attempts": attempts[:6],
        "thresholds_on_margin": profile,
        "automation": "disabled_report_only",
        "note": "local research_store/klines when available, then kline cache; no Binance API call",
    }


def evaluate_recovery_bar_replay_evidence(recovery: list[dict[str, Any]]) -> dict[str, Any]:
    """Attach and summarize report-only bar replay evidence for recovery positions."""
    action_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    positions: list[dict[str, Any]] = []
    for pos in recovery:
        evidence = build_recovery_bar_replay_evidence(pos)
        pos["recovery_replay_evidence"] = evidence
        action = str(evidence.get("action") or "replay_data_gap")
        status = str(evidence.get("status") or "unknown")
        action_counts[action] = action_counts.get(action, 0) + 1
        status_counts[status] = status_counts.get(status, 0) + 1
        positions.append(
            {
                "strategy": pos.get("strategy"),
                "symbol": pos.get("symbol"),
                "side": pos.get("side"),
                "status": status,
                "action": action,
                "timeframe": evidence.get("timeframe") or "",
                "replay_exit_reason": evidence.get("replay_exit_reason") or "",
                "replay_pnl_usdt": evidence.get("replay_pnl_usdt"),
                "pnl_delta_vs_current_usdt": evidence.get("pnl_delta_vs_current_usdt"),
            }
        )
    return {
        "policy": "report_only_local_kline_bar_replay_for_recovery_positions",
        "automation": "disabled_report_only",
        "action_counts": dict(sorted(action_counts.items())),
        "status_counts": dict(sorted(status_counts.items())),
        "manual_review_positions": int(action_counts.get("bar_replay_exit_manual_review", 0)),
        "data_gap_positions": int(action_counts.get("replay_data_gap", 0)),
        "positions": positions,
    }


def build_recovery_strategy_exit_evidence(pos: dict[str, Any]) -> dict[str, Any]:
    """Build report-only, strategy-aware recovery exit evidence for one position."""
    strategy = str(pos.get("strategy") or "")
    profile = recovery_strategy_exit_profile(strategy)
    mfe = safe_float(pos.get("mfe_pct_on_margin"))
    mae = safe_float(pos.get("mae_pct_on_margin"))
    drawdown = safe_float(pos.get("drawdown_from_mfe_pct_on_margin"))
    age_hours = safe_float(pos.get("age_hours"))
    same_open_like = int(pos.get("same_strategy_open_like_count") or 0)
    opposite_open_like = int(pos.get("opposite_open_like_count") or 0)

    triggers: list[str] = []
    if opposite_open_like > 0:
        action = "opposite_signal_manual_review"
        triggers.append("opposite_open_like_signal")
    elif (
        mfe >= profile["min_mfe_pct_on_margin"]
        and drawdown <= -profile["mfe_drawdown_review_pct"]
    ):
        action = "mfe_drawdown_manual_review"
        triggers.append("mfe_drawdown_review")
    elif (
        mfe >= profile["min_mfe_pct_on_margin"]
        and drawdown <= -profile["trailing_watch_drawdown_pct"]
    ):
        action = "recovery_trailing_watch"
        triggers.append("mfe_trailing_watch")
    elif same_open_like > 0:
        action = "same_side_reopen_hold_bias"
        triggers.append("same_side_open_like_signal")
    else:
        action = "keep_shadow_monitoring"

    return {
        "profile": strategy if strategy in RECOVERY_STRATEGY_EXIT_PROFILES else "default",
        "action": action,
        "triggers": triggers,
        "mfe_pct_on_margin": round(mfe, 2),
        "mae_pct_on_margin": round(mae, 2),
        "drawdown_from_mfe_pct_on_margin": round(drawdown, 2),
        "age_hours": round(age_hours, 2),
        "same_strategy_open_like_count": same_open_like,
        "opposite_open_like_count": opposite_open_like,
        "thresholds": profile,
        "automation": "disabled_report_only",
    }


def evaluate_recovery_strategy_exit_evidence(recovery: list[dict[str, Any]]) -> dict[str, Any]:
    """Attach and summarize strategy-specific recovery exit evidence."""
    now = datetime.now(CST)
    action_counts: dict[str, int] = {}
    positions: list[dict[str, Any]] = []
    for pos in recovery:
        snap_dt = parse_dt(pos.get("snapshot_ts"))
        age_hours = (now - snap_dt).total_seconds() / 3600 if snap_dt else safe_float(pos.get("age_hours"))
        evidence = build_recovery_strategy_exit_evidence({**pos, "age_hours": age_hours})
        pos["strategy_exit_evidence"] = evidence
        action = str(evidence.get("action") or "keep_shadow_monitoring")
        action_counts[action] = action_counts.get(action, 0) + 1
        positions.append(
            {
                "strategy": pos.get("strategy"),
                "symbol": pos.get("symbol"),
                "side": pos.get("side"),
                "action": action,
                "triggers": evidence.get("triggers") or [],
                "mfe_pct_on_margin": evidence.get("mfe_pct_on_margin"),
                "drawdown_from_mfe_pct_on_margin": evidence.get("drawdown_from_mfe_pct_on_margin"),
                "opposite_open_like_count": evidence.get("opposite_open_like_count"),
                "same_strategy_open_like_count": evidence.get("same_strategy_open_like_count"),
            }
        )
    return {
        "policy": "report_only_strategy_specific_recovery_exit_evidence",
        "automation": "disabled_report_only",
        "action_counts": dict(sorted(action_counts.items())),
        "manual_review_positions": sum(
            action_counts.get(action, 0)
            for action in ("opposite_signal_manual_review", "mfe_drawdown_manual_review")
        ),
        "watch_positions": int(action_counts.get("recovery_trailing_watch", 0)),
        "hold_bias_positions": int(action_counts.get("same_side_reopen_hold_bias", 0)),
        "profile_thresholds": RECOVERY_STRATEGY_EXIT_PROFILES,
        "positions": positions,
    }


def review_recovery_positions(recovery: list[dict]) -> dict[str, Any]:
    """Build read-only recovery-position review facts without changing exit behavior."""
    now = datetime.now(CST)
    positions: list[dict[str, Any]] = []
    risk_counts = {"none": 0, "watch": 0, "review": 0}
    total_margin = 0.0
    total_upnl = 0.0
    oldest_age = 0.0
    for pos in recovery:
        snap_dt = parse_dt(pos.get("snapshot_ts"))
        age_hours = (now - snap_dt).total_seconds() / 3600 if snap_dt else safe_float(pos.get("age_hours"))
        margin = safe_float(pos.get("margin"))
        upnl = safe_float(pos.get("unrealized_pnl"))
        upnl_pct = upnl / margin * 100 if margin > 0 else 0.0
        oldest_age = max(oldest_age, age_hours)
        total_margin += margin
        total_upnl += upnl
        opposite_open_like = int(pos.get("opposite_open_like_count") or 0)
        strategy_exit_evidence = pos.get("strategy_exit_evidence") or build_recovery_strategy_exit_evidence(
            {**pos, "age_hours": age_hours}
        )
        pos["strategy_exit_evidence"] = strategy_exit_evidence
        strategy_exit_action = str(strategy_exit_evidence.get("action") or "keep_shadow_monitoring")
        replay_evidence = pos.get("recovery_replay_evidence") or build_recovery_bar_replay_evidence(pos)
        pos["recovery_replay_evidence"] = replay_evidence
        replay_action = str(replay_evidence.get("action") or "replay_data_gap")
        if strategy_exit_action in {"opposite_signal_manual_review", "mfe_drawdown_manual_review"}:
            risk = "review"
            action = strategy_exit_action
        elif replay_action == "bar_replay_exit_manual_review":
            risk = "review"
            action = replay_action
        elif age_hours >= 24 or upnl_pct <= -5:
            risk = "review"
            action = "manual_review"
        elif strategy_exit_action == "recovery_trailing_watch" or age_hours >= 8 or upnl_pct <= -2:
            risk = "watch"
            action = strategy_exit_action if strategy_exit_action == "recovery_trailing_watch" else "keep_shadow_monitoring"
        else:
            risk = "none"
            action = strategy_exit_action if strategy_exit_action == "same_side_reopen_hold_bias" else "hold_baseline"
        risk_counts[risk] += 1
        positions.append(
            {
                "strategy": pos.get("strategy"),
                "account": pos.get("account"),
                "symbol": pos.get("symbol"),
                "side": pos.get("side"),
                "age_hours": round(age_hours, 2),
                "entry_price": pos.get("entry_price"),
                "mark_price": pos.get("mark_price"),
                "qty": pos.get("qty"),
                "margin_usdt": round(margin, 2),
                "unrealized_pnl_usdt": round(upnl, 2),
                "unrealized_pnl_pct_on_margin": round(upnl_pct, 2),
                "first_seen_ts": pos.get("first_seen_ts"),
                "path_samples": int(pos.get("path_samples") or 0),
                "mfe_pct_on_margin": round(safe_float(pos.get("mfe_pct_on_margin")), 2),
                "mae_pct_on_margin": round(safe_float(pos.get("mae_pct_on_margin")), 2),
                "drawdown_from_mfe_pct_on_margin": round(safe_float(pos.get("drawdown_from_mfe_pct_on_margin")), 2),
                "mfe_price_pct": round(safe_float(pos.get("mfe_price_pct")), 4),
                "mae_price_pct": round(safe_float(pos.get("mae_price_pct")), 4),
                "same_strategy_signal_count": int(pos.get("same_strategy_signal_count") or 0),
                "opposite_signal_count": int(pos.get("opposite_signal_count") or 0),
                "same_strategy_open_like_count": int(pos.get("same_strategy_open_like_count") or 0),
                "opposite_open_like_count": opposite_open_like,
                "latest_same_strategy_signal": pos.get("latest_same_strategy_signal") or {},
                "latest_opposite_signal": pos.get("latest_opposite_signal") or {},
                "signal_shadow_action": pos.get("signal_shadow_action") or "no_recent_same_strategy_signal",
                "strategy_exit_action": strategy_exit_action,
                "strategy_exit_triggers": strategy_exit_evidence.get("triggers") or [],
                "strategy_exit_evidence": strategy_exit_evidence,
                "recovery_replay_action": replay_action,
                "recovery_replay_status": replay_evidence.get("status") or "",
                "recovery_replay_exit_reason": replay_evidence.get("replay_exit_reason") or "",
                "recovery_replay_pnl_usdt": replay_evidence.get("replay_pnl_usdt"),
                "recovery_replay_delta_usdt": replay_evidence.get("pnl_delta_vs_current_usdt"),
                "recovery_replay_evidence": replay_evidence,
                "risk": risk,
                "shadow_action": action,
                "note": "只读审查；不自动平仓、不改变退出规则。",
            }
        )
    positions.sort(
        key=lambda item: (
            {"review": 0, "watch": 1, "none": 2}.get(str(item.get("risk")), 3),
            -abs(float(item.get("unrealized_pnl_pct_on_margin") or 0)),
            -float(item.get("age_hours") or 0),
        )
    )
    return {
        "count": len(recovery),
        "risk_counts": risk_counts,
        "signal_counts": {
            "same_strategy_signal_positions": sum(1 for pos in recovery if int(pos.get("same_strategy_signal_count") or 0) > 0),
            "opposite_signal_positions": sum(1 for pos in recovery if int(pos.get("opposite_signal_count") or 0) > 0),
            "same_strategy_reopen_supported": sum(1 for pos in recovery if int(pos.get("same_strategy_open_like_count") or 0) > 0),
            "opposite_signal_review": sum(1 for pos in recovery if int(pos.get("opposite_open_like_count") or 0) > 0),
        },
        "strategy_exit_counts": {
            action: sum(
                1
                for pos in recovery
                if (pos.get("strategy_exit_evidence") or {}).get("action") == action
            )
            for action in (
                "opposite_signal_manual_review",
                "mfe_drawdown_manual_review",
                "recovery_trailing_watch",
                "same_side_reopen_hold_bias",
                "keep_shadow_monitoring",
            )
        },
        "replay_counts": {
            action: sum(
                1
                for pos in recovery
                if (pos.get("recovery_replay_evidence") or {}).get("action") == action
            )
            for action in (
                "bar_replay_exit_manual_review",
                "bar_replay_hold_bias",
                "replay_data_gap",
            )
        },
        "replay_status_counts": {
            status: sum(
                1
                for pos in recovery
                if (pos.get("recovery_replay_evidence") or {}).get("status") == status
            )
            for status in (
                "complete",
                "missing_data",
                "missing_identity",
                "missing_time",
                "missing_entry_price",
                "missing_quantity",
            )
        },
        "oldest_age_hours": round(oldest_age, 2),
        "total_margin_usdt": round(total_margin, 2),
        "total_unrealized_pnl_usdt": round(total_upnl, 2),
        "path_metric_note": "report_only_snapshot_path_mfe_mae_signal_and_local_kline_replay_evidence",
        "positions": positions,
        "policy": "report_only_no_auto_exit",
    }


def compute_strategy_stats(trades: list[dict]) -> dict[str, dict]:
    """Compute per-strategy statistics."""
    stats: dict[str, dict] = {}
    for strategy in STRATEGY_MAP:
        strats = [t for t in trades if t["strategy"] == strategy]
        closed = [t for t in strats if not t["is_open"] and t["pnl_usd"] is not None]
        active = [t for t in strats if t["is_open"]]
        wins = [t for t in closed if t["pnl_usd"] > 0]
        losses = [t for t in closed if t["pnl_usd"] <= 0]

        total_pnl = sum(t["pnl_usd"] for t in closed)
        total_fee = sum(t["fee_estimate"] for t in closed)
        net_pnl = total_pnl - total_fee
        avg_win = sum(t["pnl_usd"] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t["pnl_usd"] for t in losses) / len(losses) if losses else 0
        payoff = abs(avg_win / avg_loss) if avg_loss != 0 else 0
        gross_profit = sum(t["pnl_usd"] for t in wins)
        gross_loss = abs(sum(t["pnl_usd"] for t in losses))
        pf = gross_profit / gross_loss if gross_loss > 0 else float("inf") if gross_profit > 0 else 0

        stats[strategy] = {
            "strategy": strategy,
            "account": STRATEGY_MAP[strategy]["account"],
            "name": STRATEGY_MAP[strategy]["name"],
            "total_trades": len(strats),
            "closed_trades": len(closed),
            "open_trades": len(active),
            "win_count": len(wins),
            "loss_count": len(losses),
            "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0,
            "total_pnl_usd": round(total_pnl, 2),
            "total_fee_usd": round(total_fee, 2),
            "net_pnl_usd": round(net_pnl, 2),
            "avg_win_usd": round(avg_win, 2),
            "avg_loss_usd": round(avg_loss, 2),
            "payoff_ratio": round(payoff, 2),
            "profit_factor": round(pf, 2) if pf != float("inf") else "inf",
            "hard_stop_count": len([t for t in closed if "hard" in (t.get("close_reason") or "").lower()]),
        }
    return stats


def compute_daily_facts(trades: list[dict]) -> dict[str, dict]:
    """Compute daily per-strategy facts."""
    daily: dict[tuple, list[dict]] = {}
    for t in trades:
        if t["is_open"] or t["pnl_usd"] is None:
            continue
        dt = parse_dt(t["entry_time"])
        if not dt:
            continue
        day = dt.strftime("%Y-%m-%d")
        key = (t["strategy"], day)
        daily.setdefault(key, []).append(t)

    facts = {}
    for (strategy, day), day_trades in daily.items():
        wins = [t for t in day_trades if t["pnl_usd"] > 0]
        losses = [t for t in day_trades if t["pnl_usd"] <= 0]
        total_pnl = sum(t["pnl_usd"] for t in day_trades)
        total_fee = sum(t["fee_estimate"] for t in day_trades)

        facts[f"{strategy}_{day}"] = {
            "strategy": strategy,
            "date": day,
            "trades": len(day_trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(day_trades) * 100, 1) if day_trades else 0,
            "total_pnl": round(total_pnl, 2),
            "total_fee": round(total_fee, 2),
            "net_pnl": round(total_pnl - total_fee, 2),
        }
    return facts


def build_output(
    trades: list[dict],
    recovery: list[dict],
    snapshots: list[dict],
) -> dict[str, Any]:
    """Build the complete truth ledger output."""
    now = datetime.now(CST)
    strategy_stats = compute_strategy_stats(trades)
    daily_facts = compute_daily_facts(trades)

    # Recovery stats per strategy with enhanced details
    recovery_stats: dict[str, dict] = {}
    for strategy in STRATEGY_MAP:
        recs = [r for r in recovery if r["strategy"] == strategy]
        total_upnl = sum(r["unrealized_pnl"] for r in recs)
        total_margin = sum(r["margin"] for r in recs)
        recovery_stats[strategy] = {
            "count": len(recs),
            "total_unrealized_pnl": round(total_upnl, 2),
            "total_margin": round(total_margin, 2),
            "symbols": [r["symbol"] for r in recs],
            "positions": [
                {
                    "symbol": r["symbol"],
                    "side": r["side"],
                    "entry_price": r["entry_price"],
                    "mark_price": r["mark_price"],
                    "unrealized_pnl": round(r["unrealized_pnl"], 2),
                    "margin": round(r["margin"], 2),
                    "leverage": r["leverage"],
                }
                for r in recs
            ],
        }

    # Recovery exit policy evaluation
    recovery_exit_policies = evaluate_recovery_exit_policies(recovery)
    recovery_bar_replay_evidence = evaluate_recovery_bar_replay_evidence(recovery)
    recovery_strategy_exit_evidence = evaluate_recovery_strategy_exit_evidence(recovery)
    recovery_review = review_recovery_positions(recovery)

    # Account summary
    account_summary = []
    for snap in snapshots:
        account_summary.append({
            "account": snap["account"],
            "wallet_usdt": round(snap["wallet_usdt"], 2),
            "unrealized_pnl_usdt": round(snap["unrealized_pnl_usdt"], 2),
            "open_positions": snap["open_positions"],
        })

    # Overall
    all_active = [t for t in trades if not t["is_open"] and t["pnl_usd"] is not None]
    total_active_pnl = sum(t["pnl_usd"] for t in all_active)
    total_recovery_upnl = sum(r["unrealized_pnl"] for r in recovery)

    return {
        "generated_at": now.isoformat(),
        "summary": {
            "total_active_trades": len([t for t in trades if t["is_active_trade"]]),
            "total_closed_trades": len([t for t in trades if not t["is_open"] and t["pnl_usd"] is not None]),
            "total_open_trades": len([t for t in trades if t["is_open"]]),
            "total_recovery_positions": len(recovery),
            "total_active_pnl_usd": round(total_active_pnl, 2),
            "total_recovery_unrealized_pnl_usd": round(total_recovery_upnl, 2),
        },
        "strategy_stats": strategy_stats,
        "recovery_stats": recovery_stats,
        "recovery_review": recovery_review,
        "recovery_exit_policies": recovery_exit_policies,
        "recovery_strategy_exit_evidence": recovery_strategy_exit_evidence,
        "recovery_bar_replay_evidence": recovery_bar_replay_evidence,
        "daily_facts": daily_facts,
        "account_summary": account_summary,
    }


def write_json(output: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def write_markdown(output: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 策略真相台账",
        "",
        f"- 生成时间: {output['generated_at']}",
        "",
        "## 总览",
        "",
        f"- 主动策略交易总数: {output['summary']['total_active_trades']}",
        f"- 已平仓交易: {output['summary']['total_closed_trades']}",
        f"- 当前持仓: {output['summary']['total_open_trades']}",
        f"- 恢复仓数量: {output['summary']['total_recovery_positions']}",
        f"- 主动策略累计 PnL: {output['summary']['total_active_pnl_usd']:.2f} USDT",
        f"- 恢复仓未实现 PnL: {output['summary']['total_recovery_unrealized_pnl_usd']:.2f} USDT",
        "",
        "## 各策略质量（主动策略，剔除恢复仓）",
        "",
        "| 策略 | 已平仓 | 胜率 | 净PnL | PF | 盈亏比 | 硬顶次数 | 恢复仓数 | 恢复仓浮盈 |",
        "|------|-------:|-----:|------:|---:|------:|--------:|--------:|----------:|",
    ]
    for strategy in ["A/v11", "B/v16", "C/v14"]:
        s = output["strategy_stats"].get(strategy, {})
        r = output["recovery_stats"].get(strategy, {})
        lines.append(
            f"| {strategy} | {s.get('closed_trades', 0)} | "
            f"{s.get('win_rate', 0):.1f}% | "
            f"{s.get('net_pnl_usd', 0):.2f} | "
            f"{s.get('profit_factor', 0)} | "
            f"{s.get('payoff_ratio', 0):.2f} | "
            f"{s.get('hard_stop_count', 0)} | "
            f"{r.get('count', 0)} | "
            f"{r.get('total_unrealized_pnl', 0):.2f} |"
        )

    lines.extend(["", "## 账户快照", ""])
    for acct in output.get("account_summary", []):
        lines.append(f"- **{acct['account']}**: 钱包 {acct['wallet_usdt']:.2f} USDT, 浮盈 {acct['unrealized_pnl_usdt']:.2f} USDT, {acct['open_positions']} 持仓")

    review = output.get("recovery_review") or {}
    lines.extend(["", "## 恢复仓独立审查", ""])
    lines.append(
        f"- 恢复仓: {int(review.get('count') or 0)} 个；"
        f"最老快照年龄: {float(review.get('oldest_age_hours') or 0):.2f}h；"
        f"保证金: {float(review.get('total_margin_usdt') or 0):.2f} USDT；"
        f"未实现 PnL: {float(review.get('total_unrealized_pnl_usdt') or 0):+.2f} USDT"
    )
    risk_counts = review.get("risk_counts") or {}
    signal_counts = review.get("signal_counts") or {}
    lines.append(
        f"- 风险分层: review={int(risk_counts.get('review') or 0)}, "
        f"watch={int(risk_counts.get('watch') or 0)}, none={int(risk_counts.get('none') or 0)}"
    )
    lines.append(
        f"- 信号证据: 同策略重开支持={int(signal_counts.get('same_strategy_reopen_supported') or 0)}, "
        f"反向信号需复核={int(signal_counts.get('opposite_signal_review') or 0)}"
    )
    strategy_exit_counts = review.get("strategy_exit_counts") or {}
    lines.append(
        f"- 策略专属退出证据: 反向信号复核={int(strategy_exit_counts.get('opposite_signal_manual_review') or 0)}, "
        f"MFE回撤复核={int(strategy_exit_counts.get('mfe_drawdown_manual_review') or 0)}, "
        f"trailing观察={int(strategy_exit_counts.get('recovery_trailing_watch') or 0)}, "
        f"同向持有倾向={int(strategy_exit_counts.get('same_side_reopen_hold_bias') or 0)}"
    )
    replay_counts = review.get("replay_counts") or {}
    lines.append(
        f"- 本地K线bar replay: 退出复核={int(replay_counts.get('bar_replay_exit_manual_review') or 0)}, "
        f"持有倾向={int(replay_counts.get('bar_replay_hold_bias') or 0)}, "
        f"数据缺口={int(replay_counts.get('replay_data_gap') or 0)}"
    )
    positions = review.get("positions") or []
    if positions:
        lines.append("")
        lines.append("| Strategy | Symbol | Side | Age h | Margin | UPNL | UPNL/Margin | MFE | MAE | MFE drawdown | Same re-open | Opposite signal | Signal action | Strategy exit | Bar replay | Risk | Shadow action |")
        lines.append("|------|------|------|------:|------:|------:|------------:|----:|----:|-------------:|------------:|---------------:|---------------|---------------|-----------|------|------------|")
        for pos in positions[:20]:
            lines.append(
                f"| {pos.get('strategy')} | {pos.get('symbol')} | {pos.get('side')} | "
                f"{float(pos.get('age_hours') or 0):.2f} | "
                f"{float(pos.get('margin_usdt') or 0):.2f} | "
                f"{float(pos.get('unrealized_pnl_usdt') or 0):+.2f} | "
                f"{float(pos.get('unrealized_pnl_pct_on_margin') or 0):+.2f}% | "
                f"{float(pos.get('mfe_pct_on_margin') or 0):+.2f}% | "
                f"{float(pos.get('mae_pct_on_margin') or 0):+.2f}% | "
                f"{float(pos.get('drawdown_from_mfe_pct_on_margin') or 0):+.2f}% | "
                f"{int(pos.get('same_strategy_open_like_count') or 0)} | "
                f"{int(pos.get('opposite_open_like_count') or 0)} | "
                f"{pos.get('signal_shadow_action')} | "
                f"{pos.get('strategy_exit_action')} | "
                f"{pos.get('recovery_replay_action')}:{pos.get('recovery_replay_exit_reason') or pos.get('recovery_replay_status')} | "
                f"{pos.get('risk')} | {pos.get('shadow_action')} |"
            )
    else:
        lines.append("- 当前无恢复仓；主动策略 alpha 未被恢复仓浮盈扭曲。")
    lines.append("- 本节只读审查，不自动平仓、不改变退出规则。")

    lines.extend(["", "## 每日明细", ""])
    daily = output.get("daily_facts", {})
    if daily:
        lines.append("| 策略 | 日期 | 交易数 | 胜率 | 净PnL |")
        lines.append("|------|------|-------:|-----:|------:|")
        for key in sorted(daily.keys(), reverse=True)[:30]:
            d = daily[key]
            lines.append(
                f"| {d['strategy']} | {d['date']} | {d['trades']} | "
                f"{d['win_rate']:.1f}% | {d['net_pnl']:.2f} |"
            )
    else:
        lines.append("暂无已平仓交易数据。")

    # Recovery exit policies
    policies = output.get("recovery_exit_policies", {})
    if policies:
        lines.extend(["", "## 恢复仓退出策略 Shadow 测试", ""])
        lines.append("| 退出策略 | 会退出 | 会持有 |")
        lines.append("|----------|-------:|-------:|")
        for key, pol in policies.items():
            lines.append(f"| {pol['label']} | {pol['would_exit']} | {pol['would_hold']} |")
        lines.append("")
        lines.append("注：以上为 shadow 评估，不自动执行。朴素时间退出可能截断大赢家，需结合证据决定。")

    strategy_exit = output.get("recovery_strategy_exit_evidence") or {}
    if strategy_exit:
        counts = strategy_exit.get("action_counts") or {}
        lines.extend(["", "## 恢复仓策略专属退出证据", ""])
        lines.append(
            f"- 策略: {strategy_exit.get('policy')}；自动化: {strategy_exit.get('automation')}；"
            f"人工复核={int(strategy_exit.get('manual_review_positions') or 0)}；"
            f"观察={int(strategy_exit.get('watch_positions') or 0)}；"
            f"同向持有倾向={int(strategy_exit.get('hold_bias_positions') or 0)}"
        )
        if counts:
            lines.append("| Action | Count |")
            lines.append("|--------|------:|")
            for action, count in counts.items():
                lines.append(f"| {action} | {int(count or 0)} |")
        lines.append("")
        lines.append("注：该层只合并策略信号、MFE/MAE、MFE回撤与持仓年龄，不自动平仓。")

    replay = output.get("recovery_bar_replay_evidence") or {}
    if replay:
        counts = replay.get("action_counts") or {}
        status_counts = replay.get("status_counts") or {}
        lines.extend(["", "## 恢复仓本地K线 Bar Replay 证据", ""])
        lines.append(
            f"- 策略: {replay.get('policy')}；自动化: {replay.get('automation')}；"
            f"退出复核={int(replay.get('manual_review_positions') or 0)}；"
            f"数据缺口={int(replay.get('data_gap_positions') or 0)}"
        )
        if counts:
            lines.append("| Action | Count |")
            lines.append("|--------|------:|")
            for action, count in counts.items():
                lines.append(f"| {action} | {int(count or 0)} |")
        if status_counts:
            lines.append("")
            lines.append("| Status | Count |")
            lines.append("|--------|------:|")
            for status, count in status_counts.items():
                lines.append(f"| {status} | {int(count or 0)} |")
        lines.append("")
        lines.append("注：该层只读取本地/镜像 `runtime/kline_cache`，不调用 Binance；恢复仓入场时间用 first-seen 快照近似。")

    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Strategy Truth Ledger")
    parser.add_argument("--db", default=None, help="Path to event_store.sqlite3")
    parser.add_argument("--runtime-dir", default=None, help="Runtime output directory")
    parser.add_argument("--reports-dir", default=None, help="Reports output directory")
    parser.add_argument("--days", type=int, default=30, help="Lookback days")
    args = parser.parse_args(argv)

    script_dir = Path(__file__).resolve().parent
    root = script_dir.parent if script_dir.name == "部署工具" else script_dir

    db_path = Path(args.db) if args.db else root / "runtime" / "event_store.sqlite3"
    runtime_dir = Path(args.runtime_dir) if args.runtime_dir else root / "runtime"
    reports_dir = Path(args.reports_dir) if args.reports_dir else root / "reports"

    if not db_path.exists():
        print(f"ERROR: Database not found: {db_path}")
        return 1

    con = sqlite3.connect(str(db_path))
    try:
        print(f"Loading data from {db_path} (last {args.days} days)...")
        open_events = load_open_events(con, days=args.days)
        close_events = load_close_events(con, days=args.days)
        snapshots = load_latest_snapshots(con)
        snapshot_history = load_snapshot_history(con, days=args.days)
        signal_events = load_recovery_signal_events(con, days=args.days)

        print(f"  OPEN events: {len(open_events)}")
        print(f"  CLOSE events: {len(close_events)}")
        print(f"  Signal/reject events: {len(signal_events)}")
        print(f"  Snapshots: {len(snapshots)} accounts")

        trades = match_trades(open_events, close_events)
        recovery = identify_recovery_positions(snapshots, open_events)
        recovery = enrich_recovery_path_metrics(recovery, snapshot_history)
        recovery = attach_recovery_signal_evidence(recovery, signal_events)

        print(f"  Matched trades: {len(trades)}")
        print(f"  Recovery positions: {len(recovery)}")

        output = build_output(trades, recovery, snapshots)

        json_path = runtime_dir / "strategy_truth_latest.json"
        md_path = reports_dir / "strategy_truth_latest.md"
        write_json(output, json_path)
        write_markdown(output, md_path)

        print(f"\nOutput:")
        print(f"  JSON: {json_path}")
        print(f"  MD:   {md_path}")

        # Print summary
        s = output["summary"]
        print(f"\n=== Summary ===")
        print(f"Active trades: {s['total_active_trades']} (closed: {s['total_closed_trades']}, open: {s['total_open_trades']})")
        print(f"Recovery positions: {s['total_recovery_positions']}")
        print(f"Active PnL: {s['total_active_pnl_usd']:.2f} USDT")
        print(f"Recovery unrealized PnL: {s['total_recovery_unrealized_pnl_usd']:.2f} USDT")
        print()
        for strategy in ["A/v11", "B/v16", "C/v14"]:
            st = output["strategy_stats"].get(strategy, {})
            rec = output["recovery_stats"].get(strategy, {})
            print(f"  {strategy}: closed={st.get('closed_trades',0)} win_rate={st.get('win_rate',0):.1f}% "
                  f"net_pnl={st.get('net_pnl_usd',0):.2f} PF={st.get('profit_factor',0)} "
                  f"recovery={rec.get('count',0)} rec_upnl={rec.get('total_unrealized_pnl',0):.2f}")

        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
