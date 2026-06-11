"""Alpha discovery historical research report.

Read-only local/Tencent research runner. It does three things:

- records the post-D/E/F lifecycle decision;
- measures early/middle/late mover forward returns;
- evaluates G/H/I research-only strategy families.

It never mutates live config, restarts scanners, calls Binance, submits orders,
or enables automatic tuning/rollback/upgrade.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
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
FORWARD_HORIZONS = (1, 3, 6, 12)
MAX_REPORT_TRADES = 80
MAX_VARIANTS_DEFAULT = 8

INTERVAL_IMPULSE_MIN = {
    "15m": 0.25,
    "30m": 0.35,
    "1h": 0.50,
    "4h": 1.00,
}

PHASE_LABELS = {
    "early": {"long": "起涨", "short": "起跌"},
    "middle": {"long": "中段上涨", "short": "中段下跌"},
    "late": {"long": "末尾上涨", "short": "末尾下跌"},
}


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def runtime_dir(root: Path = ROOT) -> Path:
    return root / "runtime"


def reports_dir(root: Path = ROOT) -> Path:
    return root / "reports"


def latest_json_path(root: Path = ROOT) -> Path:
    return runtime_dir(root) / "alpha_discovery_latest.json"


def latest_html_path(root: Path = ROOT) -> Path:
    return reports_dir(root) / "alpha_discovery_latest.html"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def safe_float(value: Any, default: float = 0.0) -> float:
    return backtest_engine.safe_float(value, default)


def safe_int(value: Any, default: int = 0) -> int:
    return backtest_engine.safe_int(value, default)


def close_price(row: dict[str, Any]) -> float:
    return safe_float(row.get("close"))


def open_price(row: dict[str, Any]) -> float:
    return safe_float(row.get("open"), close_price(row))


def pct_change(a: float, b: float) -> float:
    return shared.pct_change(a, b)


def directional_pct(a: float, b: float, side: str) -> float:
    raw = pct_change(a, b)
    return raw if side == "long" else -raw


def phase_for_bar(
    bars: list[dict[str, Any]],
    idx: int,
    *,
    lookback: int = 8,
    early_same_max_pct: float = 0.8,
    middle_same_max_pct: float = 2.5,
) -> dict[str, Any] | None:
    if idx <= lookback or idx >= len(bars):
        return None
    prev_close = close_price(bars[idx - 1])
    current_close = close_price(bars[idx])
    prior_close = close_price(bars[idx - lookback])
    if prev_close <= 0 or current_close <= 0 or prior_close <= 0:
        return None
    one_bar_pct = pct_change(prev_close, current_close)
    side = "long" if one_bar_pct > 0 else "short" if one_bar_pct < 0 else ""
    if not side:
        return None
    prior_same_pct = max(0.0, directional_pct(prior_close, prev_close, side))
    if prior_same_pct <= early_same_max_pct:
        phase = "early"
    elif prior_same_pct <= middle_same_max_pct:
        phase = "middle"
    else:
        phase = "late"
    return {
        "side": side,
        "phase": phase,
        "phase_label": PHASE_LABELS[phase][side],
        "one_bar_pct": abs(one_bar_pct),
        "signed_one_bar_pct": one_bar_pct,
        "prior_same_pct": prior_same_pct,
    }


def forward_return_pct(bars: list[dict[str, Any]], idx: int, horizon: int, side: str) -> float | None:
    if idx + horizon >= len(bars):
        return None
    entry = close_price(bars[idx])
    exit_price = close_price(bars[idx + horizon])
    if entry <= 0 or exit_price <= 0:
        return None
    return directional_pct(entry, exit_price, side)


def add_trade_from_fill(
    out: list[dict[str, Any]],
    *,
    strategy: str,
    adapter: str,
    symbol: str,
    interval: str,
    side: str,
    signal_bar: dict[str, Any],
    entry_bar: dict[str, Any],
    fill: Any,
    extra: dict[str, Any],
) -> None:
    row = fill.to_dict()
    row.update(
        {
            "strategy": strategy,
            "symbol": symbol,
            "interval": interval,
            "side": side,
            "entry_signal_ts": signal_bar.get("ts"),
            "entry_ts": entry_bar.get("ts"),
            "adapter": adapter,
        }
    )
    row.update(extra)
    out.append(row)


def simulate_atr_trade(
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
    max_hold = max(2, safe_int(params.get("max_hold_bars"), 24))
    stop_mult = safe_float(params.get("atr_stop_multiplier"), 1.8)
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


def g_variants() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for impulse_mult in (1.0, 1.35):
        for volume_min in (1.15, 1.45):
            for hold in (4, 8):
                out.append(
                    {
                        "name": f"impulse*x{impulse_mult:g},vol={volume_min:g},hold={hold}",
                        "params": {
                            "impulse_min_mult": impulse_mult,
                            "impulse_max_pct": 6.0,
                            "prior_same_max_pct": 0.9,
                            "volume_ratio_min": volume_min,
                            "atr_stop_multiplier": 1.6,
                            "take_profit_atr": 2.8,
                            "trailing_pullback_atr": 1.0,
                            "trailing_activation_atr": 0.7,
                            "max_hold_bars": hold,
                            "trade_size_usdt": 100.0,
                            "leverage": 2.0,
                        },
                    }
                )
    return out


def run_g_interval(interval: str, loaded: dict[str, list[dict[str, Any]]], variant: dict[str, Any], split: str | None = None) -> list[dict[str, Any]]:
    params = variant["params"]
    trades: list[dict[str, Any]] = []
    base_impulse = INTERVAL_IMPULSE_MIN.get(interval, 0.5) * safe_float(params.get("impulse_min_mult"), 1.0)
    for symbol, bars in loaded.items():
        use_bars = shared.split_sequence(bars, split)
        idx = 24
        while idx < len(use_bars) - 3 and len(trades) < shared.MAX_TRADES_PER_SYMBOL:
            phase = phase_for_bar(use_bars, idx, early_same_max_pct=safe_float(params.get("prior_same_max_pct"), 0.9))
            if not phase or phase["phase"] != "early":
                idx += 1
                continue
            vol_ratio = shared.volume_ratio(use_bars, idx)
            impulse = safe_float(phase.get("one_bar_pct"))
            if impulse < base_impulse or impulse > safe_float(params.get("impulse_max_pct"), 6.0):
                idx += 1
                continue
            if vol_ratio < safe_float(params.get("volume_ratio_min"), 1.2):
                idx += 1
                continue
            trade = simulate_atr_trade(
                strategy="G/early_momentum",
                adapter="historical_research_g",
                symbol=symbol,
                interval=interval,
                bars=use_bars,
                signal_idx=idx,
                side=str(phase["side"]),
                params=params,
                extra={
                    "signal_phase": phase["phase_label"],
                    "impulse_pct": round(impulse, 6),
                    "prior_same_pct": round(safe_float(phase.get("prior_same_pct")), 6),
                    "volume_ratio": round(vol_ratio, 6),
                },
            )
            if trade:
                trades.append(trade)
                idx += max(1, safe_int(trade.get("bars_held"), 1))
            idx += 1
    return trades


def h_variants() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for lookback in (18, 30):
        for range_max in (2.2, 3.5):
            for volume_min in (1.05, 1.35):
                out.append(
                    {
                        "name": f"compress={lookback},range={range_max:g},vol={volume_min:g}",
                        "params": {
                            "breakout_lookback": lookback,
                            "compression_range_pct_max": range_max,
                            "volume_ratio_min": volume_min,
                            "atr_stop_multiplier": 1.9,
                            "take_profit_atr": 3.6,
                            "trailing_pullback_atr": 1.2,
                            "trailing_activation_atr": 0.9,
                            "max_hold_bars": 24,
                            "trade_size_usdt": 100.0,
                            "leverage": 2.0,
                        },
                    }
                )
    return out


def compression_range_pct(bars: list[dict[str, Any]], idx: int, lookback: int) -> float:
    if idx <= lookback:
        return 999.0
    window = bars[idx - lookback : idx]
    highs = [safe_float(row.get("high")) for row in window]
    lows = [safe_float(row.get("low")) for row in window]
    close = close_price(bars[idx - 1])
    if close <= 0 or not highs or not lows:
        return 999.0
    return (max(highs) - min(lows)) / close * 100.0


def run_h_interval(interval: str, loaded: dict[str, list[dict[str, Any]]], variant: dict[str, Any], split: str | None = None) -> list[dict[str, Any]]:
    params = variant["params"]
    lookback = max(8, safe_int(params.get("breakout_lookback"), 20))
    trades: list[dict[str, Any]] = []
    for symbol, bars in loaded.items():
        use_bars = shared.split_sequence(bars, split)
        idx = lookback + 2
        while idx < len(use_bars) - 3 and len(trades) < shared.MAX_TRADES_PER_SYMBOL:
            window = use_bars[idx - lookback : idx]
            channel_high = max(safe_float(row.get("high")) for row in window)
            channel_low = min(safe_float(row.get("low")) for row in window)
            close = close_price(use_bars[idx])
            if close <= 0:
                idx += 1
                continue
            range_pct = compression_range_pct(use_bars, idx, lookback)
            vol_ratio = shared.volume_ratio(use_bars, idx)
            if range_pct > safe_float(params.get("compression_range_pct_max"), 3.0) or vol_ratio < safe_float(params.get("volume_ratio_min"), 1.1):
                idx += 1
                continue
            side = "long" if close > channel_high else "short" if close < channel_low else ""
            if not side:
                idx += 1
                continue
            trade = simulate_atr_trade(
                strategy="H/compression_breakout",
                adapter="historical_research_h",
                symbol=symbol,
                interval=interval,
                bars=use_bars,
                signal_idx=idx,
                side=side,
                params=params,
                extra={
                    "compression_range_pct": round(range_pct, 6),
                    "volume_ratio": round(vol_ratio, 6),
                    "channel_high": round(channel_high, 8),
                    "channel_low": round(channel_low, 8),
                },
            )
            if trade:
                trades.append(trade)
                idx += max(1, safe_int(trade.get("bars_held"), 1))
            idx += 1
    return trades


def i_variants() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for threshold_delta in (0.0, 8.0):
        for volume_min in (1.0, 1.25):
            for trailing in (0.8, 1.2):
                out.append(
                    {
                        "name": f"a_delta={threshold_delta:g},vol={volume_min:g},trail={trailing:g}",
                        "params": {
                            "entry_threshold_delta": threshold_delta,
                            "allowed_phases": ["early", "middle"],
                            "volume_ratio_min": volume_min,
                            "atr_pct_min": 0.10,
                            "atr_pct_max": 6.0,
                            "trailing_pullback_atr": trailing,
                            "trailing_activation_atr": 0.8,
                            "take_profit_atr": 4.0,
                            "max_hold_bars": 72,
                            "trade_size_usdt": 100.0,
                            "leverage": 2.0,
                        },
                    }
                )
    return out


def a_base_threshold(interval: str) -> float:
    return 105.0 if interval == "15m" else 90.0


def run_i_interval(interval: str, loaded: dict[str, list[dict[str, Any]]], variant: dict[str, Any], split: str | None = None) -> list[dict[str, Any]]:
    params = variant["params"]
    allowed_phases = set(params.get("allowed_phases") or ["early", "middle"])
    runtime_params = dict(params)
    runtime_params["entry_threshold"] = a_base_threshold(interval) + safe_float(params.get("entry_threshold_delta"), 0.0)
    trades: list[dict[str, Any]] = []
    spec = {"strategy": "A/v11", "interval": interval, "direction": "strategy_default", "capital_usdt": shared.CAPITAL_USDT, "fee_bps": shared.FEE_BPS}
    for symbol, bars in loaded.items():
        use_bars = shared.split_sequence(bars, split)
        closes = [close_price(row) for row in use_bars]
        idx = 30
        while idx < len(use_bars) - 3 and len(trades) < shared.MAX_TRADES_PER_SYMBOL:
            signal = backtest_engine.signal_for_bar("A/v11", use_bars, idx, runtime_params, closes=closes)
            if not signal:
                idx += 1
                continue
            phase = phase_for_bar(use_bars, idx)
            if not phase or str(phase.get("side")) != str(signal.get("side")) or phase.get("phase") not in allowed_phases:
                idx += 1
                continue
            vol_ratio = shared.volume_ratio(use_bars, idx)
            atr_pct = safe_float(signal.get("atr_pct"))
            if vol_ratio < safe_float(params.get("volume_ratio_min"), 1.0):
                idx += 1
                continue
            if not (safe_float(params.get("atr_pct_min"), 0.1) <= atr_pct <= safe_float(params.get("atr_pct_max"), 6.0)):
                idx += 1
                continue
            entry_idx = idx + 1
            entry = open_price(use_bars[entry_idx])
            if entry <= 0:
                idx += 1
                continue
            forward = use_bars[entry_idx : min(len(use_bars), entry_idx + max(4, safe_int(params.get("max_hold_bars"), 72)))]
            if len(forward) < 2:
                break
            profile = backtest_engine.exit_profile("A/v11", interval, runtime_params, signal["side"], entry, safe_float(signal.get("atr")))
            qty = safe_float(params.get("trade_size_usdt"), 100.0) / entry
            try:
                fill = simulate_replay_fill(
                    ReplayFillRequest(
                        symbol=symbol,
                        side=signal["side"],
                        entry_price=entry,
                        quantity=qty,
                        stop_loss=profile["stop_loss"],
                        take_profit=profile["take_profit"],
                        trailing_stop_atr=profile["trailing_stop_atr"],
                        trailing_activation_atr=profile["trailing_activation_atr"],
                        atr=max(safe_float(signal.get("atr")), entry * 0.001),
                        leverage=safe_float(params.get("leverage"), 2.0),
                        fee_bps=shared.FEE_BPS,
                        slippage_bps=0.0,
                        conservative_intrabar=True,
                    ),
                    forward,
                )
            except Exception:
                idx += 1
                continue
            row = fill.to_dict()
            row.update(
                {
                    "strategy": "I/a_v11_filtered_derivative",
                    "symbol": symbol,
                    "interval": interval,
                    "side": signal["side"],
                    "entry_signal_ts": use_bars[idx].get("ts"),
                    "entry_ts": use_bars[entry_idx].get("ts"),
                    "score": signal.get("score"),
                    "threshold": runtime_params["entry_threshold"],
                    "atr_pct": atr_pct,
                    "volume_ratio": round(vol_ratio, 6),
                    "signal_phase": phase["phase_label"],
                    "adapter": "historical_research_i",
                }
            )
            trades.append(row)
            idx = entry_idx + max(1, safe_int(fill.bars_held, 1))
    return trades


STRATEGIES = {
    "g_early_momentum": {
        "strategy": "G/early_momentum",
        "title": "G 起涨/起跌早段动量",
        "description": "只吃早段涨跌榜信号：一根K出现脉冲、前段同向涨幅低、成交放量；末尾追涨追跌直接挡掉。",
        "variants": g_variants,
        "runner": run_g_interval,
    },
    "h_compression_breakout": {
        "strategy": "H/compression_breakout",
        "title": "H 波动收缩后突破",
        "description": "先找窄幅压缩，再等放量突破通道，避免在无压缩的噪声区追单。",
        "variants": h_variants,
        "runner": run_h_interval,
    },
    "i_a_v11_filtered": {
        "strategy": "I/a_v11_filtered_derivative",
        "title": "I A/v11 派生过滤版",
        "description": "保留 A/v11 信号骨架，只过滤末段信号并微调入场阈值/ATR退出；不改 A/v11 实盘配置。",
        "variants": i_variants,
        "runner": run_i_interval,
    },
}


def compact_metric(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "net_profit_usdt": safe_float(row.get("net_profit_usdt")),
        "return_pct": safe_float(row.get("return_pct")),
        "max_drawdown_pct": safe_float(row.get("max_drawdown_pct")),
        "profit_factor": safe_float(row.get("profit_factor")),
        "win_rate_pct": safe_float(row.get("win_rate_pct")),
        "trades": safe_int(row.get("trades")),
        "avg_trade_usdt": safe_float(row.get("avg_trade_usdt")),
        "fees_usdt": safe_float(row.get("fees_usdt")),
    }


def compact_trade(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": row.get("symbol"),
        "side": row.get("side"),
        "interval": row.get("interval"),
        "entry_ts": row.get("entry_ts"),
        "exit_ts": row.get("exit_ts"),
        "net_pnl_usdt": safe_float(row.get("net_pnl_usdt")),
        "exit_reason": row.get("exit_reason"),
        "signal_phase": row.get("signal_phase"),
    }


def compact_variant(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": row.get("name"),
        "params": row.get("params"),
        "full": compact_metric(row.get("full") or {}),
        "train": compact_metric(row.get("train") or {}),
        "validation": compact_metric(row.get("validation") or {}),
        "test": compact_metric(row.get("test") or {}),
        "robust_score": safe_float(row.get("robust_score")),
        "anti_fit_pass": bool(row.get("anti_fit_pass")),
        "anti_fit_reasons": list(row.get("anti_fit_reasons") or []),
        "trades": [compact_trade(item) for item in (row.get("trades") or [])[-MAX_REPORT_TRADES:]],
    }


def run_strategy(
    *,
    strategy_id: str,
    intervals: list[str],
    symbols: list[str],
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
        loaded = {sym: bars for sym, bars in loaded_by_interval.get(interval, {}).items() if len(bars) >= shared.MIN_BARS}
        variants = spec["variants"]()[:max(1, max_variants)]
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
                    "full": compact_metric(best_robust.get("full") or {}),
                    "test": compact_metric(best_robust.get("test") or {}),
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
            "plain_advice": "过 OOS 也只代表可人工复核；未经明确批准，不进入 paper shadow，更不进入实盘。",
            "auto_apply_allowed": False,
            "automatic_upgrade_allowed": False,
        },
    }


def alpha_diagnostics(loaded_by_interval: dict[str, dict[str, list[dict[str, Any]]]]) -> dict[str, Any]:
    buckets: dict[tuple[str, str, str], dict[str, Any]] = {}
    by_symbol: dict[str, dict[str, Any]] = {}
    for interval, by_symbol_bars in loaded_by_interval.items():
        min_impulse = INTERVAL_IMPULSE_MIN.get(interval, 0.5)
        for symbol, bars in by_symbol_bars.items():
            if len(bars) < shared.MIN_BARS:
                continue
            symbol_hit = by_symbol.setdefault(symbol, {"symbol": symbol, "signals": 0, "avg_6bar_pct_sum": 0.0, "avg_6bar_count": 0})
            for idx in range(24, len(bars) - max(FORWARD_HORIZONS) - 1):
                phase = phase_for_bar(bars, idx)
                if not phase:
                    continue
                if safe_float(phase.get("one_bar_pct")) < min_impulse:
                    continue
                vol_ratio = shared.volume_ratio(bars, idx)
                if vol_ratio < 1.05:
                    continue
                key = (interval, str(phase["side"]), str(phase["phase"]))
                item = buckets.setdefault(
                    key,
                    {
                        "interval": interval,
                        "side": phase["side"],
                        "phase": phase["phase"],
                        "phase_label": phase["phase_label"],
                        "samples": 0,
                        "avg_impulse_pct_sum": 0.0,
                        "avg_prior_same_pct_sum": 0.0,
                        "avg_volume_ratio_sum": 0.0,
                        "horizons": {str(h): {"sum": 0.0, "wins": 0, "count": 0} for h in FORWARD_HORIZONS},
                    },
                )
                item["samples"] += 1
                item["avg_impulse_pct_sum"] += safe_float(phase.get("one_bar_pct"))
                item["avg_prior_same_pct_sum"] += safe_float(phase.get("prior_same_pct"))
                item["avg_volume_ratio_sum"] += vol_ratio
                symbol_hit["signals"] += 1
                f6 = forward_return_pct(bars, idx, 6, str(phase["side"]))
                if f6 is not None:
                    symbol_hit["avg_6bar_pct_sum"] += f6
                    symbol_hit["avg_6bar_count"] += 1
                for horizon in FORWARD_HORIZONS:
                    fwd = forward_return_pct(bars, idx, horizon, str(phase["side"]))
                    if fwd is None:
                        continue
                    hrow = item["horizons"][str(horizon)]
                    hrow["sum"] += fwd
                    hrow["wins"] += 1 if fwd > 0 else 0
                    hrow["count"] += 1
    rows: list[dict[str, Any]] = []
    for item in buckets.values():
        samples = max(1, safe_int(item.get("samples")))
        horizons: dict[str, Any] = {}
        for horizon, hrow in (item.get("horizons") or {}).items():
            count = max(0, safe_int(hrow.get("count")))
            horizons[horizon] = {
                "avg_directional_pct": round(safe_float(hrow.get("sum")) / count, 6) if count else 0.0,
                "win_rate_pct": round(safe_int(hrow.get("wins")) / count * 100.0, 6) if count else 0.0,
                "samples": count,
            }
        rows.append(
            {
                "interval": item["interval"],
                "side": item["side"],
                "phase": item["phase"],
                "phase_label": item["phase_label"],
                "samples": samples,
                "avg_impulse_pct": round(safe_float(item.get("avg_impulse_pct_sum")) / samples, 6),
                "avg_prior_same_pct": round(safe_float(item.get("avg_prior_same_pct_sum")) / samples, 6),
                "avg_volume_ratio": round(safe_float(item.get("avg_volume_ratio_sum")) / samples, 6),
                "horizons": horizons,
            }
        )
    rows.sort(key=lambda row: (str(row["interval"]), str(row["side"]), str(row["phase"])))
    symbol_rows = []
    for row in by_symbol.values():
        count = safe_int(row.get("avg_6bar_count"))
        symbol_rows.append(
            {
                "symbol": row["symbol"],
                "signals": safe_int(row.get("signals")),
                "avg_6bar_directional_pct": round(safe_float(row.get("avg_6bar_pct_sum")) / count, 6) if count else 0.0,
            }
        )
    symbol_rows.sort(key=lambda row: (safe_int(row.get("signals")), safe_float(row.get("avg_6bar_directional_pct"))), reverse=True)
    return {
        "method": "one_bar_impulse_with_prior_same_direction_phase_and_forward_returns",
        "horizons_bars": list(FORWARD_HORIZONS),
        "rows": rows,
        "top_symbols": symbol_rows[:20],
        "plain_takeaway": diagnostic_takeaway(rows),
    }


def diagnostic_takeaway(rows: list[dict[str, Any]]) -> str:
    early = [row for row in rows if row.get("phase") == "early" and safe_int(row.get("samples")) >= 20]
    late = [row for row in rows if row.get("phase") == "late" and safe_int(row.get("samples")) >= 20]
    early_avg = sum(safe_float(((row.get("horizons") or {}).get("6") or {}).get("avg_directional_pct")) for row in early) / len(early) if early else 0.0
    late_avg = sum(safe_float(((row.get("horizons") or {}).get("6") or {}).get("avg_directional_pct")) for row in late) / len(late) if late else 0.0
    if early and early_avg > late_avg:
        return f"早段 6-bar 均值 {early_avg:+.4f}% 好于末段 {late_avg:+.4f}%，后续研究应优先抓起涨/起跌。"
    if late and late_avg > early_avg:
        return f"末段 6-bar 均值 {late_avg:+.4f}% 不弱于早段 {early_avg:+.4f}%，说明当前早段定义仍需收紧。"
    return "样本不足或早/末段差异不明显，先看分周期表，不直接上线。"


def lifecycle_rows(root: Path) -> list[dict[str, Any]]:
    d_e_f = read_json(runtime_dir(root) / "d_e_f_historical_research_latest.json")
    strategies = d_e_f.get("strategies") if isinstance(d_e_f.get("strategies"), dict) else {}
    rows = [
        {"name": "A/v11", "state": "active_reference", "decision": "保留现役，小仓/模拟继续观察；不扩仓，不自动升级。"},
        {"name": "B/v16", "state": "frozen_observe", "decision": "冻结为对照组和微结构观察；不进入升级候选。"},
        {"name": "C/v14", "state": "rejected_retired", "decision": "已退役；历史报告只作审计，不再补新仓。"},
    ]
    for key, name, decision in (
        ("d_trend", "D/trend_breakout", "拒绝：趋势突破线 aggregate/OOS 不足。"),
        ("e_cross_section", "E/cross_sectional_momentum", "仅保留 4h 人工复核候选；paper_shadow_allowed=false。"),
        ("f_pairs", "F/pairs_mean_reversion", "拒绝：配对均值回归 aggregate/OOS 不足。"),
    ):
        summary = strategies.get(key) if isinstance(strategies.get(key), dict) else {}
        rows.append(
            {
                "name": name,
                "state": "manual_review_4h_only" if key == "e_cross_section" else "rejected",
                "decision": decision,
                "net_profit_usdt": summary.get("candidate_net_profit_usdt"),
                "robust_candidate_intervals": summary.get("robust_candidate_intervals"),
            }
        )
    rows.extend(
        [
            {"name": "G/early_momentum", "state": "research_only", "decision": "新研究线：只验证起涨/起跌早段动量。"},
            {"name": "H/compression_breakout", "state": "research_only", "decision": "新研究线：只验证压缩后突破。"},
            {"name": "I/a_v11_filtered_derivative", "state": "research_only", "decision": "新研究线：A/v11 派生过滤，不改现役 A。"},
        ]
    )
    return rows


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


def operator_summary(strategy_payloads: dict[str, dict[str, Any]]) -> dict[str, Any]:
    candidates = []
    lines = []
    for sid, payload in strategy_payloads.items():
        summary = payload.get("portfolio_summary") or {}
        action = (payload.get("operator_summary") or {}).get("overall_action") or "unknown"
        lines.append(
            f"{payload.get('strategy')}: net {safe_float(summary.get('candidate_net_profit_usdt')):+.2f} USDT; "
            f"trades {safe_int(summary.get('candidate_trades'))}; robust intervals {safe_int(summary.get('robust_candidate_intervals'))}; {action}"
        )
        candidates.extend((payload.get("operator_summary") or {}).get("research_candidates") or [])
    return {
        "overall_action": "research_only_wait_for_alpha_evidence" if not candidates else "manual_review_candidates_only",
        "lines": lines,
        "research_candidates": candidates,
        "plain_advice": "先用 alpha 诊断找结构，再让 G/H/I 过 walk-forward/OOS；任何候选都不能自动进入 paper/live。",
        "auto_apply_allowed": False,
        "automatic_upgrade_allowed": False,
    }


def run_all(
    root: Path,
    *,
    symbols: list[str],
    intervals: list[str],
    start: datetime,
    end: datetime,
    max_variants: int = MAX_VARIANTS_DEFAULT,
) -> dict[str, Any]:
    loaded_by_interval: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for interval in intervals:
        loaded_by_interval[interval] = shared.load_interval_bars(root=root, symbols=symbols, interval=interval, start=start, end=end)
    coverage = shared.coverage_rows(loaded_by_interval)
    diagnostics = alpha_diagnostics(loaded_by_interval)
    strategies: dict[str, dict[str, Any]] = {}
    for strategy_id in STRATEGIES:
        strategies[strategy_id] = run_strategy(
            strategy_id=strategy_id,
            intervals=intervals,
            symbols=symbols,
            loaded_by_interval=loaded_by_interval,
            max_variants=max_variants,
        )
    payload = {
        "generated_at": now_iso(),
        "status": "completed",
        "module": "alpha_discovery",
        "title": "Alpha 发现与 G/H/I 研究报告",
        "period": {"start": start.isoformat(timespec="seconds"), "end": end.isoformat(timespec="seconds"), "days": (end - start).days},
        "engine_parity": "historical_research_adapter",
        "coverage": {
            "target_symbols": len(symbols),
            "target_symbol_intervals": len(symbols) * len(intervals),
            "usable_symbols": len({row["symbol"] for row in coverage if row.get("usable")}),
            "usable_symbol_intervals": sum(1 for row in coverage if row.get("usable")),
            "rows": coverage,
        },
        "lifecycle": lifecycle_rows(root),
        "diagnostics": diagnostics,
        "strategies": strategies,
        "operator_summary": operator_summary(strategies),
        "historical_quality": shared.historical_payload(root).get("quality", {}),
        "config": {
            "symbols": symbols,
            "intervals": intervals,
            "max_variants_per_strategy_interval": max_variants,
            "fee_bps": shared.FEE_BPS,
            "capital_usdt_per_interval": shared.CAPITAL_USDT,
            "oos_rules": {
                "min_split_trades": shared.MIN_SPLIT_TRADES,
                "min_profit_factor": shared.MIN_PROFIT_FACTOR,
                "max_drawdown_pct": shared.MAX_DRAWDOWN_PCT,
                "all_splits_must_be_profitable": True,
            },
        },
        "safety": safety_payload(),
        "report_path": str(latest_html_path(root)),
    }
    write_json(latest_json_path(root), payload)
    html_path = latest_html_path(root)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(render_html(payload), encoding="utf-8")
    return payload


def fmt(value: Any, digits: int = 2, signed: bool = False) -> str:
    try:
        number = float(value)
    except Exception:
        return "-"
    return f"{number:+.{digits}f}" if signed else f"{number:.{digits}f}"


def metric_cell(row: dict[str, Any], key: str) -> str:
    full = row.get("full") if isinstance(row.get("full"), dict) else {}
    value = full.get(key)
    if key == "trades":
        return str(safe_int(value))
    return fmt(value, 2, signed=(key == "net_profit_usdt"))


def render_lifecycle(rows: list[dict[str, Any]]) -> str:
    body = []
    for row in rows:
        body.append(
            "<tr>"
            f"<td>{escape(str(row.get('name') or '-'))}</td>"
            f"<td>{escape(str(row.get('state') or '-'))}</td>"
            f"<td>{fmt(row.get('net_profit_usdt'), 2, signed=True) if row.get('net_profit_usdt') is not None else '-'}</td>"
            f"<td>{escape(str(row.get('robust_candidate_intervals') if row.get('robust_candidate_intervals') is not None else '-'))}</td>"
            f"<td>{escape(str(row.get('decision') or '-'))}</td>"
            "</tr>"
        )
    return "<table><thead><tr><th>策略</th><th>状态</th><th>历史净收益</th><th>稳健周期</th><th>决策</th></tr></thead><tbody>" + "".join(body) + "</tbody></table>"


def render_diagnostics(rows: list[dict[str, Any]]) -> str:
    body = []
    for row in rows:
        horizons = row.get("horizons") if isinstance(row.get("horizons"), dict) else {}
        h6 = horizons.get("6") if isinstance(horizons.get("6"), dict) else {}
        h12 = horizons.get("12") if isinstance(horizons.get("12"), dict) else {}
        body.append(
            "<tr>"
            f"<td>{escape(str(row.get('interval')))}</td>"
            f"<td>{escape(str(row.get('phase_label')))}</td>"
            f"<td>{safe_int(row.get('samples'))}</td>"
            f"<td>{fmt(row.get('avg_impulse_pct'), 3)}</td>"
            f"<td>{fmt(row.get('avg_prior_same_pct'), 3)}</td>"
            f"<td>{fmt(row.get('avg_volume_ratio'), 2)}</td>"
            f"<td>{fmt(h6.get('avg_directional_pct'), 4, signed=True)} / {fmt(h6.get('win_rate_pct'), 1)}%</td>"
            f"<td>{fmt(h12.get('avg_directional_pct'), 4, signed=True)} / {fmt(h12.get('win_rate_pct'), 1)}%</td>"
            "</tr>"
        )
    return "<table><thead><tr><th>周期</th><th>阶段</th><th>样本</th><th>脉冲%</th><th>前段同向%</th><th>量比</th><th>6根后均值/胜率</th><th>12根后均值/胜率</th></tr></thead><tbody>" + "".join(body) + "</tbody></table>"


def render_strategy_cards(strategies: dict[str, dict[str, Any]]) -> str:
    cards = []
    for payload in strategies.values():
        summary = payload.get("portfolio_summary") or {}
        op = payload.get("operator_summary") or {}
        cards.append(
            "<div class=\"card\">"
            f"<span>{escape(str(payload.get('title')))}</span>"
            f"<b>{fmt(summary.get('candidate_net_profit_usdt'), 2, signed=True)} USDT</b>"
            f"<p>交易 {safe_int(summary.get('candidate_trades'))}；稳健周期 {safe_int(summary.get('robust_candidate_intervals'))}/{safe_int(summary.get('intervals'))}；{escape(str(op.get('overall_action')))}</p>"
            "</div>"
        )
    return "".join(cards)


def render_strategy_sections(strategies: dict[str, dict[str, Any]]) -> str:
    sections = []
    for payload in strategies.values():
        rows = []
        details = []
        for interval, result in (payload.get("interval_results") or {}).items():
            best = result.get("best_robust") or {}
            rows.append(
                "<tr>"
                f"<td>{escape(str(interval))}</td>"
                f"<td>{escape(str(best.get('name') or '-'))}</td>"
                f"<td>{metric_cell(best, 'net_profit_usdt')}</td>"
                f"<td>{metric_cell(best, 'profit_factor')}</td>"
                f"<td>{metric_cell(best, 'max_drawdown_pct')}</td>"
                f"<td>{metric_cell(best, 'win_rate_pct')}</td>"
                f"<td>{metric_cell(best, 'trades')}</td>"
                f"<td>{'通过' if best.get('anti_fit_pass') else '未通过'}</td>"
                f"<td>{escape(', '.join(best.get('anti_fit_reasons') or [])[:220])}</td>"
                "</tr>"
            )
            trade_rows = []
            for trade in (best.get("trades") or [])[-MAX_REPORT_TRADES:]:
                trade_rows.append(
                    "<tr>"
                    f"<td>{escape(str(trade.get('symbol') or '-'))}</td>"
                    f"<td>{escape(str(trade.get('side') or '-'))}</td>"
                    f"<td>{escape(str(trade.get('signal_phase') or '-'))}</td>"
                    f"<td>{escape(str(trade.get('entry_ts') or '-'))}</td>"
                    f"<td>{escape(str(trade.get('exit_ts') or '-'))}</td>"
                    f"<td>{fmt(trade.get('net_pnl_usdt'), 4, signed=True)}</td>"
                    f"<td>{escape(str(trade.get('exit_reason') or '-'))}</td>"
                    "</tr>"
                )
            details.append(
                f"<details><summary>{escape(str(interval))} 最佳稳健样本交易</summary>"
                "<div class=\"scroll\"><table><thead><tr><th>币种</th><th>方向</th><th>阶段</th><th>入场</th><th>出场</th><th>净收益</th><th>原因</th></tr></thead>"
                f"<tbody>{''.join(trade_rows)}</tbody></table></div></details>"
            )
        sections.append(
            f"<section><h2>{escape(str(payload.get('title')))}</h2><p>{escape(str(payload.get('description')))}</p>"
            "<table><thead><tr><th>周期</th><th>参数组</th><th>净收益</th><th>PF</th><th>回撤%</th><th>胜率%</th><th>交易数</th><th>OOS</th><th>原因</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>{''.join(details)}</section>"
        )
    return "".join(sections)


def render_html(payload: dict[str, Any]) -> str:
    op = payload.get("operator_summary") or {}
    summary_lines = "".join(f"<li>{escape(str(line))}</li>" for line in op.get("lines") or [])
    diagnostics = payload.get("diagnostics") if isinstance(payload.get("diagnostics"), dict) else {}
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Alpha 发现与 G/H/I 研究报告</title>
<style>
body{{margin:0;background:#0b1118;color:#e7eef7;font-family:Arial,'Microsoft YaHei',sans-serif}}
.wrap{{max-width:1320px;margin:0 auto;padding:28px}}
.hero{{border:1px solid #1d344a;background:#101a25;padding:22px;border-radius:8px}}
h1{{margin:0 0 8px;font-size:28px}} h2{{margin-top:28px}}
.grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-top:16px}}
.card{{background:#0f1722;border:1px solid #22364a;border-radius:8px;padding:14px}}
.card span{{color:#8ea2b7;font-size:12px}} .card b{{display:block;margin-top:6px;font-size:22px}}
table{{width:100%;border-collapse:collapse;background:#0f1722;border:1px solid #22364a;margin-top:10px}}
th,td{{padding:9px;border-bottom:1px solid #203246;text-align:left;font-size:13px;vertical-align:top}}
th{{color:#9db4ca;background:#132031}} code{{color:#9bdcff}}
details{{margin:10px 0;background:#0f1722;border:1px solid #22364a;border-radius:8px;padding:10px}}
.scroll{{max-height:380px;overflow:auto;border:1px solid #22364a}}
@media(max-width:900px){{.grid{{grid-template-columns:1fr}} .wrap{{padding:18px}}}}
</style>
</head>
<body><main class="wrap">
<section class="hero">
<h1>Alpha 发现与 G/H/I 研究报告</h1>
<p>生成时间：{escape(str(payload.get('generated_at')))}；周期：{escape(str((payload.get('period') or {}).get('start')))} 到 {escape(str((payload.get('period') or {}).get('end')))}。</p>
<p>结论：<b>{escape(str(op.get('overall_action')))}</b>。{escape(str(op.get('plain_advice') or ''))}</p>
</section>
<section class="grid">{render_strategy_cards(payload.get('strategies') or {})}</section>
<h2>当前生命周期</h2>
{render_lifecycle(payload.get('lifecycle') or [])}
<h2>Alpha 诊断</h2>
<div class="card"><b>{escape(str(diagnostics.get('plain_takeaway') or ''))}</b><p>看每个涨跌阶段的 6/12 根 K 后方向收益。它只用于发现结构，不是入场信号。</p></div>
{render_diagnostics(diagnostics.get('rows') or [])}
<h2>G/H/I 回测结果</h2>
<div class="card"><ul>{summary_lines}</ul></div>
{render_strategy_sections(payload.get('strategies') or {})}
<h2>安全边界</h2>
<div class="card"><p>只读历史研究；不调用 Binance，不改扫描频率，不改 live config，不开 paper/real order，不启用自动调参/回滚/升级。</p></div>
</main></body></html>"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run alpha discovery and G/H/I historical research report")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--intervals", default=",".join(shared.DEFAULT_INTERVALS))
    parser.add_argument("--symbols", default="")
    parser.add_argument("--max-variants", type=int, default=MAX_VARIANTS_DEFAULT)
    args = parser.parse_args(argv)
    root = args.root
    intervals = shared.parse_csv(args.intervals, shared.DEFAULT_INTERVALS)
    symbols = shared.universe_symbols(root, shared.parse_csv(args.symbols, []) if args.symbols else None)
    end = datetime.now(CST)
    start = end - timedelta(days=max(30, int(args.days)))
    payload = run_all(root, symbols=symbols, intervals=intervals, start=start, end=end, max_variants=max(1, int(args.max_variants)))
    print(json.dumps({"status": payload.get("status"), "report_path": payload.get("report_path"), "operator_summary": payload.get("operator_summary")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
