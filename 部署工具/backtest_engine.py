"""Read-only historical backtest engine.

This engine is intentionally a research adapter. It uses the historical Kline
warehouse and shared replay fill primitives, but it does not import or run live
scanner loops. Results are useful for screening and parameter comparison, not
automatic strategy promotion.
"""

from __future__ import annotations

import json
import math
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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from core.replay_fill import ReplayFillRequest, simulate_replay_fill


CST = timezone(timedelta(hours=8))
INTERVAL_MS = {
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
}
MIN_BARS = 80
MAX_TRADES_PER_RUN = 4000
TRAIN_RATIO = 0.60
VALIDATION_RATIO = 0.20
MIN_OOS_TRADES = 3


def parse_dt(value: Any) -> datetime:
    text = str(value or "").replace("Z", "+00:00")
    if not text:
        return datetime.now(CST)
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt.astimezone(CST)


def ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(int(ms) / 1000.0, CST).isoformat(timespec="seconds")


def iso_to_ms(value: Any) -> int:
    return int(parse_dt(value).timestamp() * 1000)


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except Exception:
        return default
    return result if math.isfinite(result) else default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def date_range(start: datetime, end: datetime) -> list[str]:
    day = start.date()
    end_day = end.date()
    out: list[str] = []
    while day <= end_day:
        out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def historical_store(root: Path = ROOT) -> Path:
    return root / "research_store" / "historical_klines"


def row_to_bar(row: dict[str, Any]) -> dict[str, Any]:
    open_ms = safe_int(row.get("open_time_ms"))
    return {
        "ts": row.get("open_time") or ms_to_iso(open_ms),
        "open_time_ms": open_ms,
        "open": safe_float(row.get("open")),
        "high": safe_float(row.get("high")),
        "low": safe_float(row.get("low")),
        "close": safe_float(row.get("close")),
        "volume": safe_float(row.get("volume")),
        "quote_volume": safe_float(row.get("quote_volume")),
        "source_file": str(row.get("source_file") or ""),
    }


def load_bars(
    *,
    root: Path,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> list[dict[str, Any]]:
    table = historical_store(root)
    rows: list[dict[str, Any]] = []
    start_dt = datetime.fromtimestamp(start_ms / 1000.0, CST)
    end_dt = datetime.fromtimestamp(end_ms / 1000.0, CST)
    for day in date_range(start_dt, end_dt):
        path = table / f"date={day}" / "data.jsonl"
        for row in read_jsonl(path):
            if str(row.get("symbol") or "").upper() != symbol.upper():
                continue
            if str(row.get("interval") or "") != interval:
                continue
            open_ms = safe_int(row.get("open_time_ms"))
            if start_ms <= open_ms <= end_ms:
                bar = row_to_bar(row)
                if bar["open"] > 0 and bar["high"] > 0 and bar["low"] > 0 and bar["close"] > 0:
                    rows.append(bar)
    rows.sort(key=lambda item: int(item["open_time_ms"]))
    deduped: list[dict[str, Any]] = []
    seen: set[int] = set()
    for row in rows:
        key = int(row["open_time_ms"])
        if key not in seen:
            deduped.append(row)
            seen.add(key)
    return deduped


def sma(values: list[float], idx: int, length: int) -> float:
    if idx + 1 < length:
        return 0.0
    window = values[idx - length + 1 : idx + 1]
    return sum(window) / len(window)


def atr(bars: list[dict[str, Any]], idx: int, length: int = 14) -> float:
    if idx <= 0:
        return 0.0
    start = max(1, idx - length + 1)
    trs: list[float] = []
    for pos in range(start, idx + 1):
        high = safe_float(bars[pos].get("high"))
        low = safe_float(bars[pos].get("low"))
        prev_close = safe_float(bars[pos - 1].get("close"))
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(trs) / len(trs) if trs else 0.0


def volume_ratio(bars: list[dict[str, Any]], idx: int, length: int = 20) -> float:
    if idx + 1 < length:
        return 1.0
    current = safe_float(bars[idx].get("quote_volume"), safe_float(bars[idx].get("volume")))
    vals = [safe_float(row.get("quote_volume"), safe_float(row.get("volume"))) for row in bars[idx - length + 1 : idx + 1]]
    avg = sum(vals) / len(vals) if vals else 0.0
    return current / avg if avg > 0 else 1.0


def pct_change(a: float, b: float) -> float:
    return (b - a) / a * 100.0 if a > 0 else 0.0


def strategy_threshold(strategy: str, interval: str, params: dict[str, Any]) -> float:
    if strategy == "A/v11":
        if "entry_threshold" in params:
            return safe_float(params.get("entry_threshold"), 105.0)
        return 105.0 if interval == "15m" else 90.0
    if strategy == "B/v16":
        if "score_threshold" in params:
            return safe_float(params.get("score_threshold"), 38.0)
        return 55.0 if interval == "15m" else 38.0
    if strategy == "C/v14":
        if "long_score_threshold" in params:
            return safe_float(params.get("long_score_threshold"), 50.0)
        return 60.0 if interval == "15m" else 50.0
    return 60.0


def signal_for_bar(strategy: str, bars: list[dict[str, Any]], idx: int, params: dict[str, Any]) -> dict[str, Any] | None:
    closes = [safe_float(row.get("close")) for row in bars]
    close = closes[idx]
    if close <= 0 or idx < 30:
        return None
    fast = sma(closes, idx, 8)
    slow = sma(closes, idx, 21)
    trend = pct_change(slow, fast)
    momentum = pct_change(closes[idx - 6], close) if idx >= 6 else 0.0
    vol_ratio = volume_ratio(bars, idx)
    body = (close - safe_float(bars[idx].get("open"))) / close * 100.0 if close > 0 else 0.0
    atr_pct = atr(bars, idx) / close * 100.0 if close > 0 else 0.0

    if strategy == "A/v11":
        raw = abs(momentum) * 18.0 + abs(trend) * 24.0 + max(0.0, vol_ratio - 1.0) * 18.0
        score = min(180.0, raw)
        side = "long" if momentum + trend >= 0 else "short"
    elif strategy == "B/v16":
        order_flow_proxy = body * vol_ratio
        raw = abs(momentum) * 10.0 + abs(order_flow_proxy) * 18.0 + max(0.0, vol_ratio - 0.8) * 12.0
        score = min(120.0, raw)
        side = "long" if order_flow_proxy + momentum >= 0 else "short"
    else:
        raw = abs(trend) * 18.0 + abs(momentum) * 12.0 + max(0.0, 4.0 - atr_pct) * 4.0
        score = min(100.0, raw)
        side = "long" if trend + momentum >= 0 else "short"

    threshold = strategy_threshold(strategy, str(params.get("interval") or ""), params)
    if score < threshold:
        return None
    score_max = safe_float(params.get("overheat_cap", params.get("score_max", 999.0)), 999.0)
    if score > score_max:
        return None
    return {
        "side": side,
        "score": round(score, 4),
        "threshold": threshold,
        "momentum_pct": round(momentum, 6),
        "trend_pct": round(trend, 6),
        "volume_ratio": round(vol_ratio, 6),
        "atr": atr(bars, idx),
        "atr_pct": round(atr_pct, 6),
    }


def side_allowed(side: str, direction: str) -> bool:
    if direction in {"both", "strategy_default", ""}:
        return True
    return side == direction


def exit_profile(strategy: str, interval: str, params: dict[str, Any], side: str, entry_price: float, atr_value: float) -> dict[str, Any]:
    atr_value = max(0.0, float(atr_value))
    if atr_value <= 0:
        atr_value = entry_price * 0.01
    if strategy == "A/v11":
        sl_mult = safe_float(params.get("atr_stop_multiplier", params.get("sl_mult", 1.5 if interval == "15m" else 2.0)), 1.5)
        tp_mult = safe_float(params.get("tp_mult", 5.5 if interval == "15m" else 6.5), 5.5)
        trail = safe_float(params.get("trailing_pullback_atr", 1.0 if interval == "15m" else 0.8), 1.0)
    elif strategy == "B/v16":
        sl_mult = safe_float(params.get("atr_stop_multiplier", params.get("sl_mult", 2.0)), 2.0)
        tp_mult = safe_float(params.get("tp_mult", 4.0), 4.0)
        trail = safe_float(params.get("trailing_pullback_atr", 1.0), 1.0)
    else:
        sl_mult = safe_float(params.get("atr_stop_multiplier", 2.0), 2.0)
        tp_mult = safe_float(params.get("tp_mult", 6.0), 6.0)
        trail = safe_float(params.get("trailing_pullback_atr", 1.2), 1.2)
    if side == "short":
        stop_loss = entry_price + atr_value * sl_mult
        take_profit = entry_price - atr_value * tp_mult
    else:
        stop_loss = entry_price - atr_value * sl_mult
        take_profit = entry_price + atr_value * tp_mult
    return {
        "stop_loss": max(0.0, stop_loss),
        "take_profit": max(0.0, take_profit),
        "trailing_stop_atr": trail,
        "trailing_activation_atr": safe_float(params.get("trailing_activation_atr", 1.0), 1.0),
    }


def simulate_symbol(
    *,
    strategy: str,
    symbol: str,
    interval: str,
    bars: list[dict[str, Any]],
    spec: dict[str, Any],
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    params = {**params, "interval": interval}
    direction = str(spec.get("direction") or "strategy_default").lower()
    capital = safe_float(spec.get("capital_usdt"), 10_000.0)
    trade_notional = safe_float(params.get("trade_size_usdt"), min(1000.0, max(100.0, capital * 0.02)))
    leverage = max(1.0, safe_float(params.get("leverage"), 4.0))
    fee_bps = safe_float(spec.get("fee_bps"), 4.0)
    slippage_bps = safe_float(spec.get("slippage_bps"), 0.0)
    max_hold_bars = max(4, safe_int(params.get("max_hold_bars"), 96))
    trades: list[dict[str, Any]] = []
    idx = 30
    while idx < len(bars) - 2 and len(trades) < MAX_TRADES_PER_RUN:
        signal = signal_for_bar(strategy, bars, idx, params)
        if not signal or not side_allowed(signal["side"], direction):
            idx += 1
            continue
        entry_idx = idx + 1
        entry_bar = bars[entry_idx]
        entry_price = safe_float(entry_bar.get("open"), safe_float(entry_bar.get("close")))
        if entry_price <= 0:
            idx += 1
            continue
        forward = bars[entry_idx : min(len(bars), entry_idx + max_hold_bars)]
        if len(forward) < 2:
            break
        profile = exit_profile(strategy, interval, params, signal["side"], entry_price, signal["atr"])
        qty = trade_notional / entry_price
        try:
            result = simulate_replay_fill(
                ReplayFillRequest(
                    symbol=symbol,
                    side=signal["side"],
                    entry_price=entry_price,
                    quantity=qty,
                    stop_loss=profile["stop_loss"],
                    take_profit=profile["take_profit"],
                    trailing_stop_atr=profile["trailing_stop_atr"],
                    trailing_activation_atr=profile["trailing_activation_atr"],
                    atr=max(signal["atr"], entry_price * 0.001),
                    leverage=leverage,
                    fee_bps=fee_bps,
                    slippage_bps=slippage_bps,
                    conservative_intrabar=True,
                ),
                forward,
            )
        except Exception:
            idx += 1
            continue
        row = result.to_dict()
        row.update(
            {
                "strategy": strategy,
                "symbol": symbol,
                "interval": interval,
                "entry_ts": entry_bar.get("ts"),
                "entry_signal_ts": bars[idx].get("ts"),
                "score": signal["score"],
                "threshold": signal["threshold"],
                "atr_pct": signal["atr_pct"],
                "volume_ratio": signal["volume_ratio"],
                "adapter": "research_adapter",
            }
        )
        trades.append(row)
        idx = entry_idx + max(1, int(result.bars_held))
    return trades


def equity_curve(trades: list[dict[str, Any]], capital: float) -> list[dict[str, Any]]:
    equity = float(capital)
    out = [{"ts": "", "equity": round(equity, 6), "net_pnl": 0.0}]
    for trade in sorted(trades, key=lambda item: str(item.get("exit_ts") or item.get("entry_ts") or "")):
        pnl = safe_float(trade.get("net_pnl_usdt"))
        equity += pnl
        out.append({"ts": str(trade.get("exit_ts") or ""), "equity": round(equity, 6), "net_pnl": round(pnl, 6)})
    return out


def max_drawdown(curve: list[dict[str, Any]]) -> tuple[float, list[dict[str, Any]]]:
    peak = 0.0
    max_dd = 0.0
    rows: list[dict[str, Any]] = []
    for point in curve:
        equity = safe_float(point.get("equity"))
        peak = max(peak, equity)
        dd = (peak - equity) / peak * 100.0 if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
        rows.append({"ts": point.get("ts") or "", "drawdown_pct": round(dd, 6)})
    return round(max_dd, 6), rows


def monthly_returns(curve: list[dict[str, Any]], capital: float) -> list[dict[str, Any]]:
    by_month: dict[str, tuple[float, float]] = {}
    previous = float(capital)
    for point in curve[1:]:
        ts = str(point.get("ts") or "")
        month = ts[:7] if len(ts) >= 7 else "unknown"
        equity = safe_float(point.get("equity"), previous)
        start, _end = by_month.get(month, (previous, previous))
        by_month[month] = (start, equity)
        previous = equity
    return [
        {"month": month, "return_pct": round((end - start) / start * 100.0, 6) if start else 0.0}
        for month, (start, end) in sorted(by_month.items())
    ]


def metrics(trades: list[dict[str, Any]], capital: float) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    curve = equity_curve(trades, capital)
    max_dd, dd_rows = max_drawdown(curve)
    wins = [row for row in trades if safe_float(row.get("net_pnl_usdt")) > 0]
    losses = [row for row in trades if safe_float(row.get("net_pnl_usdt")) < 0]
    gross_profit = sum(safe_float(row.get("net_pnl_usdt")) for row in wins)
    gross_loss = abs(sum(safe_float(row.get("net_pnl_usdt")) for row in losses))
    net = sum(safe_float(row.get("net_pnl_usdt")) for row in trades)
    fees = sum(safe_float(row.get("fee_usdt")) for row in trades)
    slip = sum(safe_float(row.get("slippage_usdt")) + safe_float(row.get("depth_slippage_usdt")) + safe_float(row.get("market_impact_usdt")) for row in trades)
    summary = {
        "net_profit_usdt": round(net, 6),
        "return_pct": round(net / capital * 100.0, 6) if capital else 0.0,
        "max_drawdown_pct": max_dd,
        "profit_factor": round(gross_profit / gross_loss, 6) if gross_loss > 0 else (round(gross_profit, 6) if gross_profit > 0 else 0.0),
        "win_rate_pct": round(len(wins) / len(trades) * 100.0, 6) if trades else 0.0,
        "trades": len(trades),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "gross_profit_usdt": round(gross_profit, 6),
        "gross_loss_usdt": round(gross_loss, 6),
        "fees_usdt": round(fees, 6),
        "slippage_usdt": round(slip, 6),
        "avg_trade_usdt": round(net / len(trades), 6) if trades else 0.0,
    }
    return summary, {
        "equity_curve": curve,
        "drawdown": dd_rows,
        "monthly_returns": monthly_returns(curve, capital),
    }


def benchmark(symbol_bars: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    returns: list[dict[str, Any]] = []
    for symbol, bars in symbol_bars.items():
        if len(bars) < 2:
            continue
        first = safe_float(bars[0].get("open"), safe_float(bars[0].get("close")))
        last = safe_float(bars[-1].get("close"))
        ret = pct_change(first, last) if first > 0 else 0.0
        returns.append({"symbol": symbol, "buy_hold_return_pct": round(ret, 6), "bars": len(bars)})
    avg = sum(row["buy_hold_return_pct"] for row in returns) / len(returns) if returns else 0.0
    return {"status": "ok" if returns else "missing", "buy_hold_return_pct": round(avg, 6), "by_symbol": returns[:30]}


def split_bars(bars: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    n = len(bars)
    train_end = max(0, int(n * TRAIN_RATIO))
    validation_end = max(train_end, int(n * (TRAIN_RATIO + VALIDATION_RATIO)))
    return {
        "train": bars[:train_end],
        "validation": bars[train_end:validation_end],
        "test": bars[validation_end:],
    }


def run_for_params(
    *,
    spec: dict[str, Any],
    symbol_bars: dict[str, list[dict[str, Any]]],
    params: dict[str, Any],
    split: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, list[dict[str, Any]]]]:
    strategy = str(spec.get("strategy") or "")
    interval = str(spec.get("interval") or "")
    capital = safe_float(spec.get("capital_usdt"), 10_000.0)
    trades: list[dict[str, Any]] = []
    for symbol, bars in symbol_bars.items():
        use_bars = split_bars(bars).get(split, bars) if split else bars
        if len(use_bars) < MIN_BARS:
            continue
        trades.extend(
            simulate_symbol(
                strategy=strategy,
                symbol=symbol,
                interval=interval,
                bars=use_bars,
                spec=spec,
                params=params,
            )
        )
    summary, charts = metrics(trades, capital)
    return trades, summary, charts


def numeric_param_keys(params: dict[str, Any]) -> list[str]:
    return [key for key, value in sorted(params.items()) if isinstance(value, (int, float)) and not isinstance(value, bool)]


def build_variants(spec: dict[str, Any], count: int) -> list[dict[str, Any]]:
    base = dict(spec.get("params") or {})
    keys = numeric_param_keys(base)
    if not keys or count <= 1:
        return [{"name": "base", "params": base}]
    multipliers = [0.9, 0.95, 1.0, 1.05, 1.1]
    variants = [{"name": "base", "params": base}]
    for key in keys:
        original = safe_float(base.get(key))
        for mult in multipliers:
            if len(variants) >= count:
                return variants
            if abs(mult - 1.0) < 1e-12:
                continue
            item = dict(base)
            item[key] = round(original * mult, 8)
            variants.append({"name": f"{key}*{mult:g}", "params": item})
    return variants[:count]


def anti_overfit_review(variant_rows: list[dict[str, Any]]) -> dict[str, Any]:
    passed: list[dict[str, Any]] = []
    for row in variant_rows:
        train = row.get("train") or {}
        validation = row.get("validation") or {}
        test = row.get("test") or {}
        reasons: list[str] = []
        if safe_int(train.get("trades")) < MIN_OOS_TRADES:
            reasons.append("train_trade_count_low")
        if safe_int(validation.get("trades")) < MIN_OOS_TRADES:
            reasons.append("validation_trade_count_low")
        if safe_int(test.get("trades")) < MIN_OOS_TRADES:
            reasons.append("test_trade_count_low")
        if safe_float(train.get("net_profit_usdt")) <= 0:
            reasons.append("train_not_profitable")
        if safe_float(validation.get("net_profit_usdt")) <= 0:
            reasons.append("validation_not_profitable")
        if safe_float(test.get("net_profit_usdt")) <= 0:
            reasons.append("test_not_profitable")
        row["anti_overfit_reasons"] = reasons
        row["anti_overfit_pass"] = not reasons
        if not reasons:
            passed.append(row)
    ranked = sorted(variant_rows, key=lambda item: (bool(item.get("anti_overfit_pass")), safe_float((item.get("test") or {}).get("net_profit_usdt"))), reverse=True)
    return {
        "status": "passed_research_only" if passed else "no_variant_passed",
        "passed_variants": len(passed),
        "evaluated_variants": len(variant_rows),
        "min_oos_trades": MIN_OOS_TRADES,
        "best_variant": {
            "name": ranked[0].get("name"),
            "params": ranked[0].get("params"),
            "anti_overfit_pass": ranked[0].get("anti_overfit_pass"),
            "test": ranked[0].get("test"),
        } if ranked else {},
        "auto_apply_allowed": False,
        "automatic_upgrade_allowed": False,
    }


def parameter_sweep(spec: dict[str, Any], symbol_bars: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    count = max(1, safe_int(spec.get("parameter_variants"), 1))
    variants = build_variants(spec, count)
    rows: list[dict[str, Any]] = []
    for variant in variants:
        params = dict(variant["params"])
        _trades, full, _charts = run_for_params(spec=spec, symbol_bars=symbol_bars, params=params)
        _tr, train, _ch = run_for_params(spec=spec, symbol_bars=symbol_bars, params=params, split="train")
        _va, validation, _vc = run_for_params(spec=spec, symbol_bars=symbol_bars, params=params, split="validation")
        _te, test, _tc = run_for_params(spec=spec, symbol_bars=symbol_bars, params=params, split="test")
        rows.append({
            "name": variant["name"],
            "params": params,
            "full": full,
            "train": train,
            "validation": validation,
            "test": test,
        })
    review = anti_overfit_review(rows)
    return {"enabled": count > 1, "variants": rows, "anti_overfit_review": review}


def load_symbol_bars(spec: dict[str, Any], root: Path) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    start_ms = iso_to_ms(spec.get("start"))
    end_ms = iso_to_ms(spec.get("end"))
    interval = str(spec.get("interval") or "")
    loaded: dict[str, list[dict[str, Any]]] = {}
    coverage: list[dict[str, Any]] = []
    for symbol in spec.get("symbols") or []:
        sym = str(symbol).upper()
        bars = load_bars(root=root, symbol=sym, interval=interval, start_ms=start_ms, end_ms=end_ms)
        loaded[sym] = bars
        coverage.append({
            "symbol": sym,
            "interval": interval,
            "bars": len(bars),
            "first": bars[0]["ts"] if bars else "",
            "last": bars[-1]["ts"] if bars else "",
            "usable": len(bars) >= MIN_BARS,
        })
    return loaded, coverage


def recommendation(summary: dict[str, Any], sweep: dict[str, Any]) -> dict[str, Any]:
    review = sweep.get("anti_overfit_review") if isinstance(sweep.get("anti_overfit_review"), dict) else {}
    if safe_int(summary.get("trades")) < MIN_OOS_TRADES:
        reason = "trade_count_too_low"
    elif review.get("status") == "passed_research_only":
        reason = "research_candidate_only_requires_human_review_and_live_sample_gate"
    elif safe_float(summary.get("net_profit_usdt")) > 0:
        reason = "profitable_full_window_but_oos_gate_not_passed"
    else:
        reason = "not_profitable_or_oos_gate_failed"
    return {
        "action": "research_review_only",
        "reason": reason,
        "auto_apply_allowed": False,
        "automatic_upgrade_allowed": False,
    }


def run_backtest(spec: dict[str, Any], *, root: Path = ROOT) -> dict[str, Any]:
    symbol_bars, coverage = load_symbol_bars(spec, root)
    usable = {symbol: bars for symbol, bars in symbol_bars.items() if len(bars) >= MIN_BARS}
    if not usable:
        return {
            "status": "data_unavailable",
            "engine_parity": "research_adapter",
            "summary": {
                "net_profit_usdt": 0.0,
                "return_pct": 0.0,
                "max_drawdown_pct": 0.0,
                "profit_factor": 0.0,
                "win_rate_pct": 0.0,
                "trades": 0,
                "fees_usdt": 0.0,
                "slippage_usdt": 0.0,
            },
            "charts": {"equity_curve": [], "drawdown": [], "monthly_returns": []},
            "trades": [],
            "benchmark": benchmark(symbol_bars),
            "coverage": coverage,
            "recommendation": {
                "action": "no_parameter_change",
                "reason": "historical_bars_missing_or_too_short",
                "auto_apply_allowed": False,
                "automatic_upgrade_allowed": False,
            },
            "parameter_sweep": {"enabled": False, "variants": [], "anti_overfit_review": {"status": "not_run"}},
            "safety": safety_payload(),
        }
    params = dict(spec.get("params") or {})
    trades, summary, charts = run_for_params(spec=spec, symbol_bars=usable, params=params)
    sweep = parameter_sweep(spec, usable)
    return {
        "status": "completed",
        "engine_parity": "research_adapter",
        "adapter_note": "Uses historical Kline research adapter, not live scanner loop byte-for-byte replay.",
        "summary": summary,
        "charts": {
            "equity_curve": charts["equity_curve"][-500:],
            "drawdown": charts["drawdown"][-500:],
            "monthly_returns": charts["monthly_returns"],
        },
        "trades": sorted(trades, key=lambda item: str(item.get("exit_ts") or ""))[-300:],
        "benchmark": benchmark(usable),
        "coverage": coverage,
        "recommendation": recommendation(summary, sweep),
        "parameter_sweep": sweep,
        "safety": safety_payload(),
    }


def safety_payload() -> dict[str, Any]:
    return {
        "binance_requests_enabled": False,
        "strategy_frequency_change": False,
        "live_scanner_impact": "none",
        "paper_or_real_orders": False,
        "auto_apply_allowed": False,
        "automatic_tuning_allowed": False,
        "automatic_rollback_allowed": False,
        "automatic_upgrade_allowed": False,
    }
