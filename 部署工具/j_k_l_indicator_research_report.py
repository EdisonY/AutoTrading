"""J/K/L indicator historical research report.

Read-only local research runner for three widely used OHLCV indicators:

- J/rsi_mean_reversion: RSI overbought/oversold mean reversion.
- K/bollinger_reversion: Bollinger Band re-entry with RSI confirmation.
- L/supertrend_adx: SuperTrend direction flips confirmed by ADX/DMI.

The runner uses the local historical Kline warehouse by default. It never
changes live config, restarts scanners, calls Binance, submits orders, or
enables automatic tuning/rollback/upgrade.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from html import escape
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

import backtest_engine
import d_e_f_historical_research_report as shared
from core.replay_fill import ReplayFillRequest, simulate_replay_fill


CST = timezone(timedelta(hours=8))
DEFAULT_MAX_VARIANTS = 8
MAX_REPORT_TRADES = 80


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def runtime_dir(root: Path = ROOT) -> Path:
    return root / "runtime"


def reports_dir(root: Path = ROOT) -> Path:
    return root / "reports"


def latest_json_path(root: Path = ROOT) -> Path:
    return runtime_dir(root) / "indicator_research_latest.json"


def latest_html_path(root: Path = ROOT) -> Path:
    return reports_dir(root) / "indicator_research_latest.html"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def safe_float(value: Any, default: float = 0.0) -> float:
    return backtest_engine.safe_float(value, default)


def safe_int(value: Any, default: int = 0) -> int:
    return backtest_engine.safe_int(value, default)


def close_price(row: dict[str, Any]) -> float:
    return safe_float(row.get("close"))


def open_price(row: dict[str, Any]) -> float:
    return safe_float(row.get("open"), close_price(row))


def rsi_series(closes: list[float], length: int) -> list[float]:
    length = max(2, int(length))
    out = [50.0 for _ in closes]
    if len(closes) <= length:
        return out
    gains: list[float] = []
    losses: list[float] = []
    for idx in range(1, length + 1):
        diff = closes[idx] - closes[idx - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains) / length
    avg_loss = sum(losses) / length
    out[length] = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
    for idx in range(length + 1, len(closes)):
        diff = closes[idx] - closes[idx - 1]
        gain = max(diff, 0.0)
        loss = max(-diff, 0.0)
        avg_gain = (avg_gain * (length - 1) + gain) / length
        avg_loss = (avg_loss * (length - 1) + loss) / length
        out[idx] = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
    return out


def atr_series(bars: list[dict[str, Any]], length: int) -> list[float]:
    length = max(2, int(length))
    out = [0.0 for _ in bars]
    trs = [0.0 for _ in bars]
    for idx in range(1, len(bars)):
        high = safe_float(bars[idx].get("high"))
        low = safe_float(bars[idx].get("low"))
        prev_close = safe_float(bars[idx - 1].get("close"))
        trs[idx] = max(high - low, abs(high - prev_close), abs(low - prev_close))
    if len(bars) <= length:
        return out
    atr = sum(trs[1 : length + 1]) / length
    out[length] = atr
    for idx in range(length + 1, len(bars)):
        atr = (atr * (length - 1) + trs[idx]) / length
        out[idx] = atr
    return out


def adx_series(bars: list[dict[str, Any]], length: int = 14) -> dict[str, list[float]]:
    length = max(2, int(length))
    n = len(bars)
    adx = [0.0 for _ in bars]
    pdi = [0.0 for _ in bars]
    mdi = [0.0 for _ in bars]
    if n <= length * 2:
        return {"adx": adx, "pdi": pdi, "mdi": mdi}
    tr = [0.0 for _ in bars]
    plus_dm = [0.0 for _ in bars]
    minus_dm = [0.0 for _ in bars]
    for idx in range(1, n):
        high = safe_float(bars[idx].get("high"))
        low = safe_float(bars[idx].get("low"))
        prev_high = safe_float(bars[idx - 1].get("high"))
        prev_low = safe_float(bars[idx - 1].get("low"))
        prev_close = safe_float(bars[idx - 1].get("close"))
        up_move = high - prev_high
        down_move = prev_low - low
        plus_dm[idx] = up_move if up_move > down_move and up_move > 0 else 0.0
        minus_dm[idx] = down_move if down_move > up_move and down_move > 0 else 0.0
        tr[idx] = max(high - low, abs(high - prev_close), abs(low - prev_close))
    sm_tr = sum(tr[1 : length + 1])
    sm_plus = sum(plus_dm[1 : length + 1])
    sm_minus = sum(minus_dm[1 : length + 1])
    dx = [0.0 for _ in bars]
    for idx in range(length, n):
        if idx > length:
            sm_tr = sm_tr - sm_tr / length + tr[idx]
            sm_plus = sm_plus - sm_plus / length + plus_dm[idx]
            sm_minus = sm_minus - sm_minus / length + minus_dm[idx]
        if sm_tr <= 0:
            continue
        pdi[idx] = 100.0 * sm_plus / sm_tr
        mdi[idx] = 100.0 * sm_minus / sm_tr
        denom = pdi[idx] + mdi[idx]
        dx[idx] = 100.0 * abs(pdi[idx] - mdi[idx]) / denom if denom > 0 else 0.0
    first = length * 2 - 1
    adx[first] = sum(dx[length:first + 1]) / length
    for idx in range(first + 1, n):
        adx[idx] = (adx[idx - 1] * (length - 1) + dx[idx]) / length
    return {"adx": adx, "pdi": pdi, "mdi": mdi}


def bollinger_at(closes: list[float], idx: int, length: int, dev_mult: float) -> tuple[float, float, float, float]:
    if idx + 1 < length:
        return 0.0, 0.0, 0.0, 0.0
    window = closes[idx - length + 1 : idx + 1]
    mid = sum(window) / len(window)
    sd = shared.stddev(window)
    upper = mid + sd * dev_mult
    lower = mid - sd * dev_mult
    width_pct = (upper - lower) / mid * 100.0 if mid > 0 else 0.0
    return mid, upper, lower, width_pct


def supertrend_direction(bars: list[dict[str, Any]], atr_len: int, mult: float) -> list[int]:
    atrs = atr_series(bars, atr_len)
    dirs = [0 for _ in bars]
    final_upper = [0.0 for _ in bars]
    final_lower = [0.0 for _ in bars]
    for idx, row in enumerate(bars):
        high = safe_float(row.get("high"))
        low = safe_float(row.get("low"))
        close = safe_float(row.get("close"))
        hl2 = (high + low) / 2.0
        upper = hl2 + mult * atrs[idx]
        lower = hl2 - mult * atrs[idx]
        if idx == 0:
            final_upper[idx] = upper
            final_lower[idx] = lower
            dirs[idx] = 1
            continue
        prev_close = close_price(bars[idx - 1])
        final_upper[idx] = upper if upper < final_upper[idx - 1] or prev_close > final_upper[idx - 1] else final_upper[idx - 1]
        final_lower[idx] = lower if lower > final_lower[idx - 1] or prev_close < final_lower[idx - 1] else final_lower[idx - 1]
        if dirs[idx - 1] <= 0:
            dirs[idx] = 1 if close > final_upper[idx] else -1
        else:
            dirs[idx] = -1 if close < final_lower[idx] else 1
    return dirs


def simulate_indicator_trade(
    *,
    strategy: str,
    adapter: str,
    symbol: str,
    interval: str,
    bars: list[dict[str, Any]],
    signal_idx: int,
    side: str,
    params: dict[str, Any],
    extra: dict[str, Any],
) -> dict[str, Any] | None:
    entry_idx = signal_idx + 1
    if entry_idx >= len(bars) - 1:
        return None
    entry = open_price(bars[entry_idx])
    if entry <= 0:
        return None
    atr_value = max(backtest_engine.atr(bars, signal_idx), entry * 0.001)
    max_hold = max(2, safe_int(params.get("max_hold_bars"), 32))
    stop_mult = safe_float(params.get("atr_stop_multiplier"), 2.0)
    tp_mult = safe_float(params.get("take_profit_atr"), 3.0)
    if side == "long":
        stop_loss = entry - atr_value * stop_mult
        take_profit = entry + atr_value * tp_mult
    else:
        stop_loss = entry + atr_value * stop_mult
        take_profit = entry - atr_value * tp_mult
    forward = bars[entry_idx : min(len(bars), entry_idx + max_hold)]
    if len(forward) < 2:
        return None
    qty = safe_float(params.get("trade_size_usdt"), 100.0) / entry
    try:
        fill = simulate_replay_fill(
            ReplayFillRequest(
                symbol=symbol,
                side=side,
                entry_price=entry,
                quantity=qty,
                stop_loss=max(0.0, stop_loss),
                take_profit=max(0.0, take_profit),
                trailing_stop_atr=safe_float(params.get("trailing_pullback_atr"), 1.0),
                trailing_activation_atr=safe_float(params.get("trailing_activation_atr"), 0.8),
                atr=atr_value,
                leverage=safe_float(params.get("leverage"), 2.0),
                fee_bps=shared.FEE_BPS,
                slippage_bps=0.0,
                conservative_intrabar=True,
            ),
            forward,
        )
    except Exception:
        return None
    row = fill.to_dict()
    row.update(
        {
            "strategy": strategy,
            "symbol": symbol,
            "interval": interval,
            "side": side,
            "entry_signal_ts": bars[signal_idx].get("ts"),
            "entry_ts": bars[entry_idx].get("ts"),
            "adapter": adapter,
            "atr_pct": round(atr_value / entry * 100.0, 6),
        }
    )
    row.update(extra)
    return row


def j_variants() -> list[dict[str, Any]]:
    out = []
    for length in (7, 14):
        for low, high in ((25.0, 75.0), (30.0, 70.0)):
            for hold in (16, 32):
                out.append(
                    {
                        "name": f"rsi={length},bands={low:g}/{high:g},hold={hold}",
                        "params": {
                            "rsi_length": length,
                            "rsi_low": low,
                            "rsi_high": high,
                            "adx_max": 32.0,
                            "atr_stop_multiplier": 1.8,
                            "take_profit_atr": 2.8,
                            "trailing_pullback_atr": 0.9,
                            "trailing_activation_atr": 0.7,
                            "max_hold_bars": hold,
                            "trade_size_usdt": 100.0,
                            "leverage": 2.0,
                        },
                    }
                )
    return out


def run_j_interval(interval: str, loaded: dict[str, list[dict[str, Any]]], variant: dict[str, Any], split: str | None = None) -> list[dict[str, Any]]:
    params = variant["params"]
    trades: list[dict[str, Any]] = []
    rsi_len = safe_int(params.get("rsi_length"), 14)
    for symbol, bars in loaded.items():
        symbol_trades = 0
        use_bars = shared.split_sequence(bars, split)
        if len(use_bars) < shared.MIN_BARS:
            continue
        closes = [close_price(row) for row in use_bars]
        rsis = rsi_series(closes, rsi_len)
        adx = adx_series(use_bars, 14)["adx"]
        idx = max(rsi_len * 2, 32)
        while idx < len(use_bars) - 2 and symbol_trades < shared.MAX_TRADES_PER_SYMBOL:
            if adx[idx] > safe_float(params.get("adx_max"), 32.0):
                idx += 1
                continue
            side = ""
            if rsis[idx] <= safe_float(params.get("rsi_low"), 30.0):
                side = "long"
            elif rsis[idx] >= safe_float(params.get("rsi_high"), 70.0):
                side = "short"
            if not side:
                idx += 1
                continue
            trade = simulate_indicator_trade(
                strategy="J/rsi_mean_reversion",
                adapter="historical_research_j",
                symbol=symbol,
                interval=interval,
                bars=use_bars,
                signal_idx=idx,
                side=side,
                params=params,
                extra={"rsi": round(rsis[idx], 4), "adx": round(adx[idx], 4)},
            )
            if trade:
                trades.append(trade)
                symbol_trades += 1
                idx += max(1, safe_int(trade.get("bars_held"), 1))
            idx += 1
    return trades


def k_variants() -> list[dict[str, Any]]:
    out = []
    for length in (20, 30):
        for dev in (2.0, 2.5):
            for low, high in ((30.0, 70.0), (35.0, 65.0)):
                out.append(
                    {
                        "name": f"bb={length},dev={dev:g},rsi={low:g}/{high:g}",
                        "params": {
                            "bb_length": length,
                            "bb_dev": dev,
                            "rsi_length": 14,
                            "rsi_low": low,
                            "rsi_high": high,
                            "band_width_min_pct": 0.4,
                            "band_width_max_pct": 16.0,
                            "atr_stop_multiplier": 1.9,
                            "take_profit_atr": 3.0,
                            "trailing_pullback_atr": 1.0,
                            "trailing_activation_atr": 0.8,
                            "max_hold_bars": 32,
                            "trade_size_usdt": 100.0,
                            "leverage": 2.0,
                        },
                    }
                )
    return out


def run_k_interval(interval: str, loaded: dict[str, list[dict[str, Any]]], variant: dict[str, Any], split: str | None = None) -> list[dict[str, Any]]:
    params = variant["params"]
    trades: list[dict[str, Any]] = []
    bb_len = safe_int(params.get("bb_length"), 20)
    dev = safe_float(params.get("bb_dev"), 2.0)
    rsi_len = safe_int(params.get("rsi_length"), 14)
    for symbol, bars in loaded.items():
        symbol_trades = 0
        use_bars = shared.split_sequence(bars, split)
        if len(use_bars) < shared.MIN_BARS:
            continue
        closes = [close_price(row) for row in use_bars]
        rsis = rsi_series(closes, rsi_len)
        idx = max(bb_len + 2, rsi_len * 2)
        while idx < len(use_bars) - 2 and symbol_trades < shared.MAX_TRADES_PER_SYMBOL:
            prev_mid, prev_upper, prev_lower, _prev_width = bollinger_at(closes, idx - 1, bb_len, dev)
            mid, upper, lower, width = bollinger_at(closes, idx, bb_len, dev)
            if mid <= 0 or prev_mid <= 0:
                idx += 1
                continue
            if not (safe_float(params.get("band_width_min_pct"), 0.4) <= width <= safe_float(params.get("band_width_max_pct"), 16.0)):
                idx += 1
                continue
            prev_close = closes[idx - 1]
            close = closes[idx]
            side = ""
            if prev_close < prev_lower and close >= lower and rsis[idx] <= safe_float(params.get("rsi_low"), 35.0):
                side = "long"
            elif prev_close > prev_upper and close <= upper and rsis[idx] >= safe_float(params.get("rsi_high"), 65.0):
                side = "short"
            if not side:
                idx += 1
                continue
            trade = simulate_indicator_trade(
                strategy="K/bollinger_reversion",
                adapter="historical_research_k",
                symbol=symbol,
                interval=interval,
                bars=use_bars,
                signal_idx=idx,
                side=side,
                params=params,
                extra={
                    "rsi": round(rsis[idx], 4),
                    "bb_mid": round(mid, 8),
                    "bb_upper": round(upper, 8),
                    "bb_lower": round(lower, 8),
                    "band_width_pct": round(width, 6),
                },
            )
            if trade:
                trades.append(trade)
                symbol_trades += 1
                idx += max(1, safe_int(trade.get("bars_held"), 1))
            idx += 1
    return trades


def l_variants() -> list[dict[str, Any]]:
    out = []
    for atr_len in (10, 14):
        for mult in (2.5, 3.5):
            for adx_min in (20.0, 25.0):
                out.append(
                    {
                        "name": f"st_atr={atr_len},mult={mult:g},adx={adx_min:g}",
                        "params": {
                            "supertrend_atr_length": atr_len,
                            "supertrend_multiplier": mult,
                            "adx_length": 14,
                            "adx_min": adx_min,
                            "atr_stop_multiplier": 2.4,
                            "take_profit_atr": 5.0,
                            "trailing_pullback_atr": 1.5,
                            "trailing_activation_atr": 1.0,
                            "max_hold_bars": 72,
                            "trade_size_usdt": 100.0,
                            "leverage": 2.0,
                        },
                    }
                )
    return out


def run_l_interval(interval: str, loaded: dict[str, list[dict[str, Any]]], variant: dict[str, Any], split: str | None = None) -> list[dict[str, Any]]:
    params = variant["params"]
    trades: list[dict[str, Any]] = []
    atr_len = safe_int(params.get("supertrend_atr_length"), 10)
    mult = safe_float(params.get("supertrend_multiplier"), 3.0)
    adx_len = safe_int(params.get("adx_length"), 14)
    for symbol, bars in loaded.items():
        symbol_trades = 0
        use_bars = shared.split_sequence(bars, split)
        if len(use_bars) < shared.MIN_BARS:
            continue
        dirs = supertrend_direction(use_bars, atr_len, mult)
        dmi = adx_series(use_bars, adx_len)
        idx = max(atr_len * 3, adx_len * 3)
        while idx < len(use_bars) - 2 and symbol_trades < shared.MAX_TRADES_PER_SYMBOL:
            if dirs[idx] == dirs[idx - 1] or dmi["adx"][idx] < safe_float(params.get("adx_min"), 20.0):
                idx += 1
                continue
            side = "long" if dirs[idx] > 0 else "short"
            if side == "long" and dmi["pdi"][idx] <= dmi["mdi"][idx]:
                idx += 1
                continue
            if side == "short" and dmi["mdi"][idx] <= dmi["pdi"][idx]:
                idx += 1
                continue
            trade = simulate_indicator_trade(
                strategy="L/supertrend_adx",
                adapter="historical_research_l",
                symbol=symbol,
                interval=interval,
                bars=use_bars,
                signal_idx=idx,
                side=side,
                params=params,
                extra={
                    "supertrend_direction": dirs[idx],
                    "adx": round(dmi["adx"][idx], 4),
                    "plus_di": round(dmi["pdi"][idx], 4),
                    "minus_di": round(dmi["mdi"][idx], 4),
                },
            )
            if trade:
                trades.append(trade)
                symbol_trades += 1
                idx += max(1, safe_int(trade.get("bars_held"), 1))
            idx += 1
    return trades


STRATEGIES = {
    "j_rsi_mean_reversion": {
        "strategy": "J/rsi_mean_reversion",
        "title": "J RSI 超买超卖均值回归",
        "description": "用 RSI 捕捉过度上涨/下跌后的回归机会，并用 ADX 上限过滤强趋势行情。",
        "variants": j_variants,
        "runner": run_j_interval,
    },
    "k_bollinger_reversion": {
        "strategy": "K/bollinger_reversion",
        "title": "K Bollinger Bands 回归",
        "description": "价格越界后重新回到布林带内，配合 RSI 确认，避免单纯追突破。",
        "variants": k_variants,
        "runner": run_k_interval,
    },
    "l_supertrend_adx": {
        "strategy": "L/supertrend_adx",
        "title": "L SuperTrend + ADX 趋势",
        "description": "SuperTrend 方向翻转作为信号，ADX/DMI 确认趋势强度和方向。",
        "variants": l_variants,
        "runner": run_l_interval,
    },
}


def compact_trade(row: dict[str, Any]) -> dict[str, Any]:
    keep = [
        "strategy",
        "symbol",
        "interval",
        "side",
        "entry_ts",
        "exit_ts",
        "net_pnl_usdt",
        "fee_usdt",
        "bars_held",
        "exit_reason",
        "adapter",
        "rsi",
        "adx",
        "band_width_pct",
    ]
    return {key: row.get(key) for key in keep if key in row}


def compact_variant(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": row.get("name"),
        "params": row.get("params"),
        "full": row.get("full") or {},
        "train": row.get("train") or {},
        "validation": row.get("validation") or {},
        "test": row.get("test") or {},
        "robust_score": safe_float(row.get("robust_score")),
        "anti_fit_pass": bool(row.get("anti_fit_pass")),
        "anti_fit_reasons": list(row.get("anti_fit_reasons") or []),
        "trades": [compact_trade(item) for item in (row.get("trades") or [])[-MAX_REPORT_TRADES:]],
    }


def run_strategy(
    *,
    strategy_id: str,
    symbols: list[str],
    intervals: list[str],
    loaded_by_interval: dict[str, dict[str, list[dict[str, Any]]]],
    max_variants: int,
) -> dict[str, Any]:
    spec = STRATEGIES[strategy_id]
    interval_results: dict[str, Any] = {}
    portfolio_net = 0.0
    portfolio_trades = 0
    robust_candidate_intervals = 0
    lines: list[str] = []
    research_candidates: list[dict[str, Any]] = []
    for interval in intervals:
        print(f"[{now_iso()}] {strategy_id} {interval} start", file=sys.stderr, flush=True)
        loaded = {symbol: bars for symbol, bars in loaded_by_interval.get(interval, {}).items() if len(bars) >= shared.MIN_BARS}
        variants = spec["variants"]()[: max(1, max_variants)]
        rows = [shared.evaluate_variant(interval, loaded, spec, variant) for variant in variants]
        rows.sort(key=lambda item: safe_float((item.get("full") or {}).get("net_profit_usdt")), reverse=True)
        best_full = rows[0] if rows else {}
        best_robust = sorted(rows, key=lambda item: safe_float(item.get("robust_score")), reverse=True)[0] if rows else {}
        passed = [row for row in rows if row.get("anti_fit_pass")]
        if passed:
            robust_candidate_intervals += 1
            research_candidates.append(
                {
                    "interval": interval,
                    "name": best_robust.get("name"),
                    "params": best_robust.get("params"),
                    "full": best_robust.get("full"),
                    "test": best_robust.get("test"),
                }
            )
        best_net = safe_float((best_robust.get("full") or {}).get("net_profit_usdt"))
        best_trades = safe_int((best_robust.get("full") or {}).get("trades"))
        portfolio_net += best_net
        portfolio_trades += best_trades
        lines.append(
            f"{interval}: best_robust {best_net:+.2f} USDT; trades {best_trades}; "
            f"{'OOS passed' if passed else 'OOS failed'}"
        )
        interval_results[interval] = {
            "interval": interval,
            "usable_symbol_count": len(loaded),
            "target_symbol_count": len(symbols),
            "variant_count": len(rows),
            "best_full": compact_variant(best_full) if best_full else {},
            "best_robust": compact_variant(best_robust) if best_robust else {},
            "variants": [compact_variant(row) for row in rows],
            "research_decision": {
                "action": "paper_shadow_review_candidate_requires_operator_approval" if passed else "research_only_reject_for_now",
                "auto_apply_allowed": False,
                "automatic_upgrade_allowed": False,
                "paper_shadow_review_candidate": bool(passed),
                "paper_shadow_allowed": False,
                "reason": "oos_gate_passed_but_operator_approval_required" if passed else "oos_or_risk_gate_failed",
            },
        }
        print(f"[{now_iso()}] {strategy_id} {interval} done net={best_net:+.2f} trades={best_trades}", file=sys.stderr, flush=True)
    return {
        "strategy": spec["strategy"],
        "strategy_id": strategy_id,
        "title": spec["title"],
        "description": spec["description"],
        "interval_results": interval_results,
        "portfolio_summary": {
            "candidate_net_profit_usdt": round(portfolio_net, 6),
            "candidate_return_pct_on_interval_capital": round(portfolio_net / (shared.CAPITAL_USDT * max(1, len(intervals))) * 100.0, 6),
            "candidate_trades": portfolio_trades,
            "intervals": len(intervals),
            "robust_candidate_intervals": robust_candidate_intervals,
            "paper_shadow_review_candidate": robust_candidate_intervals > 0,
            "paper_shadow_allowed": False,
            "auto_apply_allowed": False,
        },
        "operator_summary": {
            "overall_action": "paper_shadow_review_candidate_requires_operator_approval" if robust_candidate_intervals else "research_only_reject_for_now",
            "lines": lines,
            "research_candidates": research_candidates,
            "plain_advice": "通过 OOS 也只代表可人工复核；未经明确批准，不进入 paper shadow，更不进入实盘。",
            "auto_apply_allowed": False,
            "automatic_upgrade_allowed": False,
        },
    }


def safety_payload() -> dict[str, Any]:
    return {
        "binance_requests_enabled": False,
        "strategy_frequency_change": False,
        "live_scanner_impact": "none",
        "paper_or_real_orders": False,
        "live_config_mutation": False,
        "auto_apply_allowed": False,
        "automatic_tuning_allowed": False,
        "automatic_rollback_allowed": False,
        "automatic_upgrade_allowed": False,
    }


def operator_summary(strategies: dict[str, dict[str, Any]]) -> dict[str, Any]:
    lines: list[str] = []
    research_candidates: list[dict[str, Any]] = []
    for payload in strategies.values():
        portfolio = payload.get("portfolio_summary") or {}
        action = (payload.get("operator_summary") or {}).get("overall_action")
        lines.append(
            f"{payload.get('strategy')}: net {safe_float(portfolio.get('candidate_net_profit_usdt')):+.2f} USDT; "
            f"trades {safe_int(portfolio.get('candidate_trades'))}; "
            f"robust intervals {safe_int(portfolio.get('robust_candidate_intervals'))}; {action}"
        )
        research_candidates.extend((payload.get("operator_summary") or {}).get("research_candidates") or [])
    return {
        "overall_action": "paper_shadow_review_candidate_requires_operator_approval" if research_candidates else "research_only_wait_for_indicator_evidence",
        "lines": lines,
        "research_candidates": research_candidates,
        "plain_advice": "J/K/L 是指标研究线；只有跨周期/OOS/反拟合过关后，才能进入人工复核。不能自动进入 paper/live。",
        "auto_apply_allowed": False,
        "automatic_upgrade_allowed": False,
    }


def metric_cell(row: dict[str, Any], key: str) -> str:
    value = (row.get("full") or {}).get(key)
    if value is None:
        return "-"
    if isinstance(value, (int, float)):
        return f"{value:.2f}" if key != "trades" else str(int(value))
    return escape(str(value))


def render_strategy_cards(strategies: dict[str, dict[str, Any]]) -> str:
    rows: list[str] = []
    for payload in strategies.values():
        summary = payload.get("portfolio_summary") or {}
        rows.append(
            "<div class='card'>"
            f"<span>{escape(str(payload.get('title')))}</span>"
            f"<b>{safe_float(summary.get('candidate_net_profit_usdt')):+.2f} USDT</b>"
            f"<p>交易 {safe_int(summary.get('candidate_trades'))}；通过周期 {safe_int(summary.get('robust_candidate_intervals'))}/{safe_int(summary.get('intervals'))}</p>"
            "</div>"
        )
    return "".join(rows)


def render_strategy_sections(strategies: dict[str, dict[str, Any]]) -> str:
    sections: list[str] = []
    for payload in strategies.values():
        interval_rows: list[str] = []
        variant_sections: list[str] = []
        trade_rows: list[str] = []
        for interval, result in (payload.get("interval_results") or {}).items():
            best = result.get("best_robust") or {}
            interval_rows.append(
                "<tr>"
                f"<td>{escape(str(interval))}</td>"
                f"<td>{escape(str(best.get('name') or '-'))}</td>"
                f"<td>{metric_cell(best, 'net_profit_usdt')}</td>"
                f"<td>{metric_cell(best, 'profit_factor')}</td>"
                f"<td>{metric_cell(best, 'max_drawdown_pct')}</td>"
                f"<td>{metric_cell(best, 'trades')}</td>"
                f"<td>{'通过' if best.get('anti_fit_pass') else '未通过'}</td>"
                f"<td>{escape(', '.join(best.get('anti_fit_reasons') or [])[:220])}</td>"
                "</tr>"
            )
            variants = []
            for row in (result.get("variants") or [])[:24]:
                variants.append(
                    "<tr>"
                    f"<td>{escape(str(row.get('name') or '-'))}</td>"
                    f"<td>{metric_cell(row, 'net_profit_usdt')}</td>"
                    f"<td>{metric_cell(row, 'profit_factor')}</td>"
                    f"<td>{metric_cell(row, 'max_drawdown_pct')}</td>"
                    f"<td>{metric_cell(row, 'trades')}</td>"
                    f"<td>{safe_float(row.get('robust_score')):.2f}</td>"
                    f"<td>{'通过' if row.get('anti_fit_pass') else '未通过'}</td>"
                    "</tr>"
                )
            variant_sections.append(
                f"<details><summary>{escape(str(interval))} 参数组</summary>"
                "<table><thead><tr><th>参数</th><th>净收益</th><th>PF</th><th>回撤%</th><th>交易数</th><th>稳健分</th><th>OOS</th></tr></thead>"
                f"<tbody>{''.join(variants)}</tbody></table></details>"
            )
            for trade in (best.get("trades") or [])[-40:]:
                trade_rows.append(
                    "<tr>"
                    f"<td>{escape(str(interval))}</td>"
                    f"<td>{escape(str(trade.get('symbol') or '-'))}</td>"
                    f"<td>{escape(str(trade.get('side') or '-'))}</td>"
                    f"<td>{escape(str(trade.get('entry_ts') or '-'))}</td>"
                    f"<td>{escape(str(trade.get('exit_ts') or '-'))}</td>"
                    f"<td>{safe_float(trade.get('net_pnl_usdt')):.4f}</td>"
                    f"<td>{escape(str(trade.get('exit_reason') or trade.get('adapter') or '-'))}</td>"
                    "</tr>"
                )
        sections.append(
            f"<section><h2>{escape(str(payload.get('title')))}</h2>"
            f"<p>{escape(str(payload.get('description')))}</p>"
            "<table><thead><tr><th>周期</th><th>最佳稳健候选</th><th>净收益</th><th>PF</th><th>回撤%</th><th>交易数</th><th>OOS</th><th>失败原因</th></tr></thead>"
            f"<tbody>{''.join(interval_rows)}</tbody></table>"
            f"{''.join(variant_sections)}"
            "<div class='scroll'><table><thead><tr><th>周期</th><th>标的</th><th>方向</th><th>入场</th><th>出场</th><th>净收益</th><th>原因</th></tr></thead>"
            f"<tbody>{''.join(trade_rows)}</tbody></table></div></section>"
        )
    return "".join(sections)


def render_html(payload: dict[str, Any]) -> str:
    operator = payload.get("operator_summary") or {}
    lines = "".join(f"<li>{escape(str(line))}</li>" for line in operator.get("lines") or [])
    source_rows: list[str] = []
    for source in payload.get("indicator_sources") or []:
        if isinstance(source, dict):
            label = escape(str(source.get("label") or "source"))
            url = escape(str(source.get("url") or ""))
            note = escape(str(source.get("note") or ""))
            source_rows.append(f"<li><a href='{url}'>{label}</a> - {note}</li>")
        else:
            source_rows.append(f"<li>{escape(str(source))}</li>")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>J/K/L 指标研究报告</title>
<style>
body{{margin:0;background:#0b1118;color:#e7eef7;font-family:Arial,'Microsoft YaHei',sans-serif}}
.wrap{{max-width:1320px;margin:0 auto;padding:28px}}
.hero{{border:1px solid #1d344a;background:#101a25;padding:22px;border-radius:8px}}
h1{{margin:0 0 8px;font-size:28px}} h2{{margin-top:30px}}
.grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-top:16px}}
.card{{background:#0f1722;border:1px solid #22364a;border-radius:8px;padding:14px}}
.card span{{color:#8ea2b7;font-size:12px}} .card b{{display:block;margin-top:6px;font-size:22px}}
table{{width:100%;border-collapse:collapse;background:#0f1722;border:1px solid #22364a;margin:10px 0}}
th,td{{padding:9px;border-bottom:1px solid #203246;text-align:left;font-size:13px;vertical-align:top}}
th{{color:#9db4ca;background:#132031}} details{{margin:10px 0;background:#0f1722;border:1px solid #22364a;border-radius:8px;padding:10px}}
.scroll{{max-height:360px;overflow:auto;border:1px solid #22364a;margin-top:10px}}
code{{color:#9bdcff}}
</style>
</head>
<body><main class="wrap">
<section class="hero">
<h1>J/K/L 指标研究报告</h1>
<p>RSI、Bollinger Bands、SuperTrend/ADX 三条公开指标线，本地一年期 OHLCV 只读回测。</p>
<p>生成时间：{escape(str(payload.get('generated_at')))}；动作：<b>{escape(str(operator.get('overall_action')))}</b>；本地仓：<code>{escape(str(payload.get('local_store_path')))}</code></p>
</section>
<section class="grid">{render_strategy_cards(payload.get('strategies') or {})}</section>
<h2>操作结论</h2>
<div class="card"><ul>{lines}</ul><p>{escape(str(operator.get('plain_advice') or ''))}</p></div>
<h2>J/K/L 分项结果</h2>
{render_strategy_sections(payload.get('strategies') or {})}
<h2>资料与安全边界</h2>
<div class="card"><ul>{''.join(source_rows)}</ul><p>所有参数预注册小范围搜索；不调用 Binance，不改扫描频率，不改 live config，不开 paper/real order，不启用自动调参/回滚/升级。</p></div>
</main></body></html>"""


def run_all(
    root: Path,
    symbols: list[str],
    intervals: list[str],
    start: datetime,
    end: datetime,
    *,
    max_variants: int = DEFAULT_MAX_VARIANTS,
) -> dict[str, Any]:
    loaded_by_interval: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for interval in intervals:
        print(f"[{now_iso()}] load {interval}", file=sys.stderr, flush=True)
        loaded_by_interval[interval] = shared.load_interval_bars(root=root, symbols=symbols, interval=interval, start=start, end=end)
    coverage = shared.coverage_rows(loaded_by_interval)
    strategies: dict[str, dict[str, Any]] = {}
    for strategy_id in STRATEGIES:
        strategies[strategy_id] = run_strategy(
            strategy_id=strategy_id,
            symbols=symbols,
            intervals=intervals,
            loaded_by_interval=loaded_by_interval,
            max_variants=max_variants,
        )
    payload = {
        "generated_at": now_iso(),
        "status": "completed",
        "module": "indicator_research",
        "title": "J/K/L 指标研究报告",
        "engine_parity": "historical_research_adapter",
        "local_store_path": str(root / "research_store" / "historical_klines"),
        "period": {"start": start.isoformat(timespec="seconds"), "end": end.isoformat(timespec="seconds"), "days": (end - start).days},
        "coverage": {
            "target_symbols": len(symbols),
            "target_symbol_intervals": len(symbols) * len(intervals),
            "usable_symbols": len({row["symbol"] for row in coverage if row.get("usable")}),
            "usable_symbol_intervals": sum(1 for row in coverage if row.get("usable")),
            "rows": coverage,
        },
        "config": {
            "symbols": symbols,
            "intervals": intervals,
            "max_variants_per_strategy_interval": max_variants,
            "capital_usdt_per_interval": shared.CAPITAL_USDT,
            "fee_bps": shared.FEE_BPS,
            "oos_rules": {
                "min_split_trades": shared.MIN_SPLIT_TRADES,
                "min_profit_factor": shared.MIN_PROFIT_FACTOR,
                "max_drawdown_pct": shared.MAX_DRAWDOWN_PCT,
                "all_splits_must_be_profitable": True,
            },
        },
        "indicator_sources": [
            {
                "label": "RSI",
                "url": "https://www.investopedia.com/articles/active-trading/042114/overbought-or-oversold-use-relative-strength-index-find-out.asp",
                "note": "momentum oscillator; common 30/70 oversold/overbought levels.",
            },
            {
                "label": "Bollinger Bands",
                "url": "https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-overlays/bollinger-bands",
                "note": "moving-average bands based on standard deviation and volatility.",
            },
            {
                "label": "ADX/DMI",
                "url": "https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/average-directional-index-adx",
                "note": "trend-strength and directional-movement confirmation.",
            },
            {
                "label": "SuperTrend",
                "url": "https://www.tradingview.com/support/solutions/43000634738-supertrend/",
                "note": "trend-following line based on ATR and volatility.",
            },
        ],
        "historical_quality": shared.historical_payload(root).get("quality", {}),
        "strategies": strategies,
        "operator_summary": operator_summary(strategies),
        "safety": safety_payload(),
        "report_path": str(latest_html_path(root)),
    }
    write_json(latest_json_path(root), payload)
    html = render_html(payload)
    latest_html_path(root).parent.mkdir(parents=True, exist_ok=True)
    latest_html_path(root).write_text(html, encoding="utf-8")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run local J/K/L indicator historical research")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--symbols", default="")
    parser.add_argument("--intervals", default=",".join(shared.DEFAULT_INTERVALS))
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--max-variants", type=int, default=DEFAULT_MAX_VARIANTS)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    intervals = shared.parse_csv(args.intervals, shared.DEFAULT_INTERVALS)
    symbols = shared.universe_symbols(root, shared.parse_csv(args.symbols, []) if args.symbols else None)
    end = datetime.now(CST)
    start = end - timedelta(days=max(30, args.days))
    payload = run_all(root, symbols, intervals, start, end, max_variants=max(1, args.max_variants))
    print(json.dumps(payload["operator_summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
