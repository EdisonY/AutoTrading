"""Local candidate x A/B match research.

This runner tests the current research clues before any paper-ledger hookup:

- L1 negative-jump bounce on 1h.
- E cross-sectional momentum on 4h.
- J3 compression breakout on 4h.

For each clue it runs standalone replay, then tests whether the clue improves
A/v11 or B/v16 as a same-interval filter:

- baseline: A/B research adapter without candidate filter.
- and_same_side: keep A/B entries only when candidate agrees on symbol/side.
- veto_opposite: keep A/B entries unless candidate is active opposite side.

It is local-only. It reads ``research_store/historical_klines`` and writes
ignored runtime/report artifacts. It never touches cloud, Binance, live config,
paper/real orders, or automatic tuning/rollback/upgrade.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
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
import b_v16_historical_research_report as b_research
import d_e_f_historical_research_report as shared
import j3_v2_strategy_research as j3_research
import k_alpha_research
import l1_edge_strategy_reconstruction as l1_research
import signal_edge_lab
import v11_historical_research_report as a_research
from core.replay_fill import ReplayFillRequest, simulate_replay_fill


CST = timezone(timedelta(hours=8))
RUNTIME_JSON = ROOT / "runtime" / "candidate_ab_match_research_latest.json"
REPORT_HTML = ROOT / "reports" / "candidate_ab_match_research_latest.html"
DEFAULT_INTERVALS = ["1h", "4h"]
CAPITAL_USDT = 10_000.0
FEE_BPS = 4.0
MIN_STANDALONE_TRADES = 80
MIN_MATCH_TRADES = 40
MAX_AB_TRADES_PER_SYMBOL = 800


SideMap = dict[str, dict[int, str]]


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def safe_float(value: Any, default: float = 0.0) -> float:
    return backtest_engine.safe_float(value, default)


def safe_int(value: Any, default: int = 0) -> int:
    return backtest_engine.safe_int(value, default)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def safety_payload() -> dict[str, bool]:
    return {
        "local_only": True,
        "binance_requests_enabled": False,
        "cloud_compute": False,
        "live_config_mutation": False,
        "paper_or_real_orders": False,
        "automatic_tuning_allowed": False,
        "automatic_rollback_allowed": False,
        "automatic_upgrade_allowed": False,
    }


def parse_csv(value: str | None, default: list[str]) -> list[str]:
    if not value:
        return list(default)
    rows = [item.strip() for item in value.split(",") if item.strip()]
    return rows or list(default)


def candidate_variants() -> list[dict[str, Any]]:
    l1_variants = {row["name"]: row for row in l1_research.variants()}
    e_variants = {row["name"]: row for row in shared.e_variants()}
    j3_rules = j3_research.load_no_trade_rules(ROOT)
    j3_variants = {row["name"]: row for row in j3_research.variants(j3_rules)}
    wanted: list[dict[str, Any]] = []

    for name in ["l1_neg_jump_12bar_time", "l1_neg_jump_12bar_balanced", "l1_neg_jump_24bar_slow_time"]:
        if name in l1_variants:
            wanted.append(
                {
                    **l1_variants[name],
                    "candidate_id": "L1_negative_jump_bounce",
                    "candidate_title": "L1 负跳反弹",
                    "family": "l1",
                    "interval": "1h",
                    "source_note": "高波动下跌跳变后的反弹结构，旧完整重建为 watch/near_miss。",
                }
            )

    for name in ["lookback=48,top=3,hold=8", "lookback=48,top=5,hold=8"]:
        if name in e_variants:
            wanted.append(
                {
                    **e_variants[name],
                    "candidate_id": "E_4h_cross_section",
                    "candidate_title": "E/4h 横截面强弱",
                    "family": "e",
                    "interval": "4h",
                    "source_note": "Top30 强弱组合，旧 4h OOS 曾最好，但未获 paper/live 许可。",
                }
            )

    for name in ["j3v2_range_chop_1bar_matched_gate", "j3v2_range_chop_3bar_matched_gate"]:
        if name in j3_variants:
            wanted.append(
                {
                    **j3_variants[name],
                    "candidate_id": "J3_compression_breakout",
                    "candidate_title": "压缩突破",
                    "family": "j3",
                    "interval": "4h",
                    "source_note": "J3 matched-baseline 结构线索，旧完整策略未过 validation/test。",
                }
            )
    return wanted


def with_extra_cost(trades: list[dict[str, Any]], extra_bps: float) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for trade in trades:
        item = dict(trade)
        qty = abs(safe_float(item.get("quantity") or item.get("requested_quantity")))
        entry = abs(safe_float(item.get("entry_price")))
        exit_price = abs(safe_float(item.get("exit_price")))
        extra = qty * (entry + exit_price) * extra_bps / 10_000.0
        item["net_pnl_usdt"] = safe_float(item.get("net_pnl_usdt")) - extra
        item["extra_cost_usdt"] = round(extra, 8)
        out.append(item)
    return out


def summarize(trades: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    return shared.summarize_trades(sorted(trades, key=lambda item: str(item.get("exit_ts") or item.get("entry_ts") or "")))


def cost_stress(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for bps in [0.0, 5.0, 10.0, 20.0]:
        summary, _ = summarize(with_extra_cost(trades, bps))
        rows.append({"extra_cost_bps": bps, **summary})
    return rows


def group_summary(trades: list[dict[str, Any]], key_name: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        key = str(trade.get(key_name) or "unknown")
        if key_name == "month":
            key = str(trade.get("exit_ts") or trade.get("entry_ts") or "")[:7] or "unknown"
        groups[key].append(trade)
    rows = []
    for key, items in groups.items():
        summary, _ = summarize(items)
        rows.append({"key": key, **summary})
    rows.sort(key=lambda item: safe_float(item.get("net_profit_usdt")), reverse=True)
    return rows


def l1_prepared_from_cache(cache: dict[str, Any], loaded: dict[str, list[dict[str, Any]]]) -> l1_research.PreparedSeries:
    key = f"l1_prepared:{id(loaded)}"
    if key not in cache:
        cache[key] = l1_research.prepare_series(loaded)
    return cache[key]


def standalone_trades(
    loaded: dict[str, list[dict[str, Any]]],
    variant: dict[str, Any],
    split: str | None,
    cache: dict[str, Any],
) -> list[dict[str, Any]]:
    family = str(variant.get("family"))
    if family == "l1":
        prepared = l1_prepared_from_cache(cache, loaded)
        return l1_research.run_variant(prepared, variant, split)
    if family == "e":
        return shared.run_e_interval(str(variant.get("interval")), loaded, variant, split)
    if family == "j3":
        return j3_research.run_variant(loaded, variant, split)
    return []


def evaluate_standalone(loaded: dict[str, list[dict[str, Any]]], variant: dict[str, Any], cache: dict[str, Any]) -> dict[str, Any]:
    full_trades = standalone_trades(loaded, variant, None, cache)
    train_trades = standalone_trades(loaded, variant, "train", cache)
    validation_trades = standalone_trades(loaded, variant, "validation", cache)
    test_trades = standalone_trades(loaded, variant, "test", cache)
    full, charts = summarize(full_trades)
    train, _ = summarize(train_trades)
    validation, _ = summarize(validation_trades)
    test, _ = summarize(test_trades)
    row = {
        "mode": "standalone",
        "candidate_id": variant.get("candidate_id"),
        "candidate_title": variant.get("candidate_title"),
        "variant": variant.get("name"),
        "interval": variant.get("interval"),
        "family": variant.get("family"),
        "params": variant.get("params"),
        "source_note": variant.get("source_note"),
        "full": full,
        "train": train,
        "validation": validation,
        "test": test,
        "cost_stress": cost_stress(full_trades),
        "charts": {"equity_curve": charts.get("equity_curve", [])[-300:], "monthly_returns": charts.get("monthly_returns", [])},
        "breakdowns": {
            "by_symbol": group_summary(full_trades, "symbol")[:30],
            "by_month": group_summary(full_trades, "month")[:30],
            "by_side": group_summary(full_trades, "side")[:10],
            "by_exit_reason": group_summary(full_trades, "exit_reason")[:20],
        },
        "sample_trades": sorted(full_trades, key=lambda item: str(item.get("exit_ts") or ""))[-120:],
    }
    decision, reasons = standalone_decision(row)
    row["decision"] = decision
    row["decision_reasons"] = reasons
    return row


def standalone_decision(row: dict[str, Any]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    full = row.get("full") or {}
    validation = row.get("validation") or {}
    test = row.get("test") or {}
    if safe_int(full.get("trades")) < MIN_STANDALONE_TRADES:
        reasons.append("full_trade_count_low")
    if safe_float(full.get("net_profit_usdt")) <= 0:
        reasons.append("full_net_not_positive")
    if safe_float(full.get("profit_factor")) < 1.15:
        reasons.append("full_pf_below_1.15")
    for split_name, metrics in [("validation", validation), ("test", test)]:
        if safe_int(metrics.get("trades")) < 20:
            reasons.append(f"{split_name}_trade_count_low")
        if safe_float(metrics.get("net_profit_usdt")) <= 0:
            reasons.append(f"{split_name}_net_not_positive")
        if safe_float(metrics.get("profit_factor")) < 1.10:
            reasons.append(f"{split_name}_pf_below_1.10")
    cost10 = next((item for item in row.get("cost_stress") or [] if safe_float(item.get("extra_cost_bps")) == 10.0), {})
    if safe_float(cost10.get("net_profit_usdt")) <= 0:
        reasons.append("cost_10bps_net_not_positive")
    if not reasons:
        return "standalone_candidate_manual_review_only", []
    if safe_float(full.get("net_profit_usdt")) > 0 and safe_float(test.get("net_profit_usdt")) > 0:
        return "watch_only_oos_or_cost_gap", sorted(set(reasons))
    return "rejected", sorted(set(reasons))


def put_side(side_map: SideMap, symbol: str, open_time_ms: int, side: str) -> None:
    if not side:
        return
    side_map.setdefault(symbol, {})[open_time_ms] = side


def build_l1_side_map(loaded: dict[str, list[dict[str, Any]]], variant: dict[str, Any], cache: dict[str, Any]) -> SideMap:
    side_map: SideMap = {}
    prepared = l1_prepared_from_cache(cache, loaded)
    params = variant["params"]
    for symbol, series in prepared.items():
        bars = series["bars"]
        features = series["features"]
        for idx in range(l1_research.MIN_FEATURE_IDX, len(bars) - 2):
            if l1_research.matches_signal(features, idx, params):
                put_side(side_map, symbol, safe_int(bars[idx].get("open_time_ms")), str(params.get("side") or ""))
    return side_map


def build_e_side_map(loaded: dict[str, list[dict[str, Any]]], variant: dict[str, Any]) -> SideMap:
    side_map: SideMap = {}
    params = variant["params"]
    idx_by_time = shared.time_index(loaded)
    times = sorted(idx_by_time)
    lookback = max(4, safe_int(params.get("lookback_bars"), 48))
    hold = max(1, safe_int(params.get("hold_bars"), 4))
    top_n = max(1, safe_int(params.get("top_n"), 3))
    min_symbols = max(top_n * 2, safe_int(params.get("min_available_symbols"), 16))
    pos = lookback
    while pos + hold < len(times):
        entry_time = times[pos]
        lookback_time = times[pos - lookback]
        entry_rows = idx_by_time.get(entry_time, {})
        lookback_rows = idx_by_time.get(lookback_time, {})
        ranks: list[tuple[str, float]] = []
        for symbol, row in entry_rows.items():
            if symbol not in lookback_rows:
                continue
            past = safe_float(lookback_rows[symbol].get("close"))
            entry = safe_float(row.get("close"))
            if past > 0 and entry > 0:
                ranks.append((symbol, shared.pct_change(past, entry)))
        if len(ranks) < min_symbols:
            pos += hold
            continue
        ranks.sort(key=lambda item: item[1], reverse=True)
        long_symbols = [symbol for symbol, _mom in ranks[:top_n]]
        short_symbols = [symbol for symbol, _mom in ranks[-top_n:]]
        for active_time in times[pos : pos + hold]:
            for symbol in long_symbols:
                put_side(side_map, symbol, active_time, "long")
            for symbol in short_symbols:
                put_side(side_map, symbol, active_time, "short")
        pos += hold
    return side_map


def build_j3_side_map(loaded: dict[str, list[dict[str, Any]]], variant: dict[str, Any]) -> SideMap:
    side_map: SideMap = {}
    params = variant["params"]
    no_trade = set(params.get("no_trade_regimes") or []) if variant.get("use_no_trade") else set()
    allowed_regimes = set(params.get("allowed_regimes") or [])
    for symbol, bars in loaded.items():
        if len(bars) < 260:
            continue
        features = signal_edge_lab.build_edge_features(bars)
        for idx in range(240, len(bars) - 14):
            signal = j3_research.j3_signal(bars, features, idx)
            if not signal:
                continue
            side, extra = signal
            if allowed_regimes and extra.get("regime") not in allowed_regimes:
                continue
            if extra.get("regime") in no_trade:
                continue
            put_side(side_map, symbol, safe_int(bars[idx].get("open_time_ms")), side)
    return side_map


def build_candidate_side_map(loaded: dict[str, list[dict[str, Any]]], variant: dict[str, Any], cache: dict[str, Any]) -> SideMap:
    family = str(variant.get("family"))
    if family == "l1":
        return build_l1_side_map(loaded, variant, cache)
    if family == "e":
        return build_e_side_map(loaded, variant)
    if family == "j3":
        return build_j3_side_map(loaded, variant)
    return {}


def ab_params(strategy: str, interval: str) -> dict[str, Any]:
    if strategy == "A/v11":
        entry, trail = a_research.default_a_v11_params(interval)
        return {
            "entry_threshold": entry,
            "trailing_pullback_atr": trail,
            "trailing_activation_atr": 1.0,
            "trade_size_usdt": 100.0,
            "leverage": 4.0,
        }
    if strategy == "B/v16":
        return dict(b_research.default_b_v16_params(interval))
    return {}


def ab_exit_profile(strategy: str, interval: str, params: dict[str, Any], side: str, entry_price: float, atr_value: float) -> dict[str, Any]:
    return backtest_engine.exit_profile(strategy, interval, params, side, entry_price, atr_value)


def passes_mode(signal_side: str, candidate_side: str | None, mode: str) -> bool:
    if mode == "baseline":
        return True
    if mode == "and_same_side":
        return candidate_side == signal_side
    if mode == "veto_opposite":
        return not candidate_side or candidate_side == signal_side
    return False


def simulate_ab_symbol(
    *,
    strategy: str,
    symbol: str,
    interval: str,
    bars: list[dict[str, Any]],
    params: dict[str, Any],
    side_map: SideMap,
    mode: str,
) -> list[dict[str, Any]]:
    if len(bars) < backtest_engine.MIN_BARS:
        return []
    spec = {
        "strategy": strategy,
        "interval": interval,
        "capital_usdt": CAPITAL_USDT,
        "fee_bps": FEE_BPS,
        "slippage_bps": 0.0,
        "direction": "both",
    }
    local_params = {**params, "interval": interval}
    trade_notional = safe_float(local_params.get("trade_size_usdt"), 100.0)
    leverage = max(1.0, safe_float(local_params.get("leverage"), 4.0))
    max_hold_bars = max(4, safe_int(local_params.get("max_hold_bars"), 96))
    closes = [safe_float(row.get("close")) for row in bars]
    trades: list[dict[str, Any]] = []
    idx = 30
    while idx < len(bars) - 2 and len(trades) < MAX_AB_TRADES_PER_SYMBOL:
        signal = backtest_engine.signal_for_bar(strategy, bars, idx, local_params, closes=closes)
        if not signal:
            idx += 1
            continue
        ts_key = safe_int(bars[idx].get("open_time_ms"))
        candidate_side = side_map.get(symbol, {}).get(ts_key)
        if not passes_mode(str(signal.get("side")), candidate_side, mode):
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
        profile = ab_exit_profile(strategy, interval, local_params, str(signal["side"]), entry_price, safe_float(signal.get("atr")))
        qty = trade_notional / entry_price
        try:
            result = simulate_replay_fill(
                ReplayFillRequest(
                    symbol=symbol,
                    side=str(signal["side"]),
                    entry_price=entry_price,
                    quantity=qty,
                    stop_loss=profile["stop_loss"],
                    take_profit=profile["take_profit"],
                    trailing_stop_atr=profile["trailing_stop_atr"],
                    trailing_activation_atr=profile["trailing_activation_atr"],
                    atr=max(safe_float(signal.get("atr")), entry_price * 0.001),
                    leverage=leverage,
                    fee_bps=FEE_BPS,
                    slippage_bps=0.0,
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
                "side": signal.get("side"),
                "entry_signal_ts": bars[idx].get("ts"),
                "entry_ts": entry_bar.get("ts"),
                "score": signal.get("score"),
                "threshold": signal.get("threshold"),
                "atr_pct": signal.get("atr_pct"),
                "volume_ratio": signal.get("volume_ratio"),
                "match_mode": mode,
                "candidate_side": candidate_side or "",
                "adapter": "candidate_ab_match_research_adapter",
            }
        )
        trades.append(row)
        idx = entry_idx + max(1, safe_int(result.bars_held, 1))
    return trades


def run_ab_mode(
    *,
    strategy: str,
    interval: str,
    loaded: dict[str, list[dict[str, Any]]],
    side_map: SideMap,
    mode: str,
    split: str | None,
) -> list[dict[str, Any]]:
    params = ab_params(strategy, interval)
    trades: list[dict[str, Any]] = []
    for symbol, bars in loaded.items():
        use_bars = shared.split_sequence(bars, split)
        trades.extend(
            simulate_ab_symbol(
                strategy=strategy,
                symbol=symbol,
                interval=interval,
                bars=use_bars,
                params=params,
                side_map=side_map,
                mode=mode,
            )
        )
    return sorted(trades, key=lambda item: str(item.get("exit_ts") or ""))


def evaluate_ab_mode(
    *,
    strategy: str,
    interval: str,
    loaded: dict[str, list[dict[str, Any]]],
    side_map: SideMap,
    candidate: dict[str, Any],
    mode: str,
    baseline: dict[str, Any] | None,
) -> dict[str, Any]:
    full_trades = run_ab_mode(strategy=strategy, interval=interval, loaded=loaded, side_map=side_map, mode=mode, split=None)
    train_trades = run_ab_mode(strategy=strategy, interval=interval, loaded=loaded, side_map=side_map, mode=mode, split="train")
    validation_trades = run_ab_mode(strategy=strategy, interval=interval, loaded=loaded, side_map=side_map, mode=mode, split="validation")
    test_trades = run_ab_mode(strategy=strategy, interval=interval, loaded=loaded, side_map=side_map, mode=mode, split="test")
    full, charts = summarize(full_trades)
    train, _ = summarize(train_trades)
    validation, _ = summarize(validation_trades)
    test, _ = summarize(test_trades)
    row = {
        "mode": mode,
        "strategy": strategy,
        "candidate_id": candidate.get("candidate_id"),
        "candidate_title": candidate.get("candidate_title"),
        "variant": candidate.get("name"),
        "interval": interval,
        "params": candidate.get("params"),
        "full": full,
        "train": train,
        "validation": validation,
        "test": test,
        "cost_stress": cost_stress(full_trades),
        "charts": {"equity_curve": charts.get("equity_curve", [])[-300:], "monthly_returns": charts.get("monthly_returns", [])},
        "breakdowns": {
            "by_symbol": group_summary(full_trades, "symbol")[:30],
            "by_month": group_summary(full_trades, "month")[:30],
            "by_side": group_summary(full_trades, "side")[:10],
            "by_exit_reason": group_summary(full_trades, "exit_reason")[:20],
        },
        "sample_trades": sorted(full_trades, key=lambda item: str(item.get("exit_ts") or ""))[-120:],
    }
    if baseline:
        row["delta_vs_baseline"] = metric_delta(full, baseline.get("full") or {})
        decision, reasons = match_decision(row, baseline)
        row["decision"] = decision
        row["decision_reasons"] = reasons
    else:
        row["decision"] = "baseline_reference"
        row["decision_reasons"] = []
    return row


def metric_delta(current: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    return {
        "net_profit_usdt": round(safe_float(current.get("net_profit_usdt")) - safe_float(baseline.get("net_profit_usdt")), 6),
        "profit_factor": round(safe_float(current.get("profit_factor")) - safe_float(baseline.get("profit_factor")), 6),
        "max_drawdown_pct": round(safe_float(current.get("max_drawdown_pct")) - safe_float(baseline.get("max_drawdown_pct")), 6),
        "trades": safe_int(current.get("trades")) - safe_int(baseline.get("trades")),
        "win_rate_pct": round(safe_float(current.get("win_rate_pct")) - safe_float(baseline.get("win_rate_pct")), 6),
    }


def match_decision(row: dict[str, Any], baseline: dict[str, Any]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    full = row.get("full") or {}
    validation = row.get("validation") or {}
    test = row.get("test") or {}
    base_full = baseline.get("full") or {}
    if safe_int(full.get("trades")) < MIN_MATCH_TRADES:
        reasons.append("match_trade_count_low")
    if safe_float(full.get("net_profit_usdt")) <= safe_float(base_full.get("net_profit_usdt")):
        reasons.append("full_net_not_better_than_baseline")
    if safe_float(full.get("profit_factor")) < safe_float(base_full.get("profit_factor")) + 0.05:
        reasons.append("full_pf_uplift_below_0.05")
    if safe_float(full.get("max_drawdown_pct")) > safe_float(base_full.get("max_drawdown_pct")):
        reasons.append("drawdown_worse_than_baseline")
    for split_name, metrics in [("validation", validation), ("test", test)]:
        if safe_int(metrics.get("trades")) < 10:
            reasons.append(f"{split_name}_trade_count_low")
        if safe_float(metrics.get("net_profit_usdt")) <= 0:
            reasons.append(f"{split_name}_net_not_positive")
        if safe_float(metrics.get("profit_factor")) < 1.0:
            reasons.append(f"{split_name}_pf_below_1.0")
    cost10 = next((item for item in row.get("cost_stress") or [] if safe_float(item.get("extra_cost_bps")) == 10.0), {})
    if safe_float(cost10.get("net_profit_usdt")) <= 0:
        reasons.append("cost_10bps_net_not_positive")
    if not reasons:
        return "ab_filter_candidate_manual_review_only", []
    if (
        safe_float(full.get("net_profit_usdt")) > safe_float(base_full.get("net_profit_usdt"))
        and safe_float(full.get("profit_factor")) > safe_float(base_full.get("profit_factor"))
    ):
        return "watch_only_filter_hint", sorted(set(reasons))
    return "rejected", sorted(set(reasons))


def evaluate_candidate(
    *,
    candidate: dict[str, Any],
    loaded: dict[str, list[dict[str, Any]]],
    cache: dict[str, Any],
) -> dict[str, Any]:
    print(f"[{now_iso()}] candidate {candidate['candidate_id']} {candidate['name']} standalone", file=sys.stderr, flush=True)
    standalone = evaluate_standalone(loaded, candidate, cache)
    print(f"[{now_iso()}] candidate {candidate['candidate_id']} {candidate['name']} side-map", file=sys.stderr, flush=True)
    side_map = build_candidate_side_map(loaded, candidate, cache)
    side_stats = {
        "symbols": len(side_map),
        "active_marks": sum(len(rows) for rows in side_map.values()),
        "by_side": dict(Counter(side for rows in side_map.values() for side in rows.values())),
    }
    matrix: list[dict[str, Any]] = []
    for strategy in ["A/v11", "B/v16"]:
        print(f"[{now_iso()}] {candidate['candidate_id']} {candidate['name']} {strategy} baseline", file=sys.stderr, flush=True)
        baseline = evaluate_ab_mode(
            strategy=strategy,
            interval=str(candidate.get("interval")),
            loaded=loaded,
            side_map=side_map,
            candidate=candidate,
            mode="baseline",
            baseline=None,
        )
        matrix.append(baseline)
        for mode in ["and_same_side", "veto_opposite"]:
            print(f"[{now_iso()}] {candidate['candidate_id']} {candidate['name']} {strategy} {mode}", file=sys.stderr, flush=True)
            matrix.append(
                evaluate_ab_mode(
                    strategy=strategy,
                    interval=str(candidate.get("interval")),
                    loaded=loaded,
                    side_map=side_map,
                    candidate=candidate,
                    mode=mode,
                    baseline=baseline,
                )
            )
    decisions = Counter([standalone.get("decision")] + [row.get("decision") for row in matrix])
    return {
        "candidate_id": candidate.get("candidate_id"),
        "candidate_title": candidate.get("candidate_title"),
        "variant": candidate.get("name"),
        "interval": candidate.get("interval"),
        "family": candidate.get("family"),
        "source_note": candidate.get("source_note"),
        "side_map_stats": side_stats,
        "standalone": standalone,
        "ab_matrix": matrix,
        "decision_counts": dict(decisions),
    }


def candidate_recommendation(candidate_result: dict[str, Any]) -> dict[str, Any]:
    standalone = candidate_result.get("standalone") or {}
    matrix = candidate_result.get("ab_matrix") or []
    filter_candidates = [row for row in matrix if row.get("decision") == "ab_filter_candidate_manual_review_only"]
    filter_hints = [row for row in matrix if row.get("decision") == "watch_only_filter_hint"]
    if standalone.get("decision") == "standalone_candidate_manual_review_only":
        return {
            "action": "manual_review_for_independent_strategy_id",
            "reason": "standalone_gate_passed_but_no_auto_paper",
            "paper_ledger_change_allowed": False,
        }
    if filter_candidates:
        best = sorted(
            filter_candidates,
            key=lambda row: safe_float((row.get("delta_vs_baseline") or {}).get("net_profit_usdt")),
            reverse=True,
        )[0]
        return {
            "action": "manual_review_for_ab_filter",
            "reason": f"{best.get('strategy')} {best.get('mode')} improves baseline in-sample and OOS gate",
            "paper_ledger_change_allowed": False,
        }
    if filter_hints:
        best = sorted(
            filter_hints,
            key=lambda row: safe_float((row.get("delta_vs_baseline") or {}).get("net_profit_usdt")),
            reverse=True,
        )[0]
        return {
            "action": "watch_only_more_sampling_or_reconstruction",
            "reason": f"{best.get('strategy')} {best.get('mode')} has full-sample uplift but fails at least one gate",
            "paper_ledger_change_allowed": False,
        }
    return {
        "action": "reject_for_now",
        "reason": "no standalone or A/B filter gate passed",
        "paper_ledger_change_allowed": False,
    }


def build_payload(root: Path, days: int, intervals: list[str], symbols: list[str] | None) -> dict[str, Any]:
    end = datetime.now(CST)
    start = end - timedelta(days=max(30, days))
    universe = shared.universe_symbols(root, symbols)
    variants = [row for row in candidate_variants() if row.get("interval") in set(intervals)]
    loaded_by_interval = {
        interval: shared.load_interval_bars(root=root, symbols=universe, interval=interval, start=start, end=end)
        for interval in sorted(set(str(row.get("interval")) for row in variants))
    }
    coverage = shared.coverage_rows(loaded_by_interval)
    usable_by_interval = {
        interval: {sym: bars for sym, bars in loaded.items() if len(bars) >= shared.MIN_BARS}
        for interval, loaded in loaded_by_interval.items()
    }
    results: list[dict[str, Any]] = []
    cache: dict[str, Any] = {}
    for idx, variant in enumerate(variants, start=1):
        interval = str(variant.get("interval"))
        loaded = usable_by_interval.get(interval, {})
        print(
            f"[{now_iso()}] ({idx}/{len(variants)}) {variant['candidate_id']} {variant['name']} interval={interval} symbols={len(loaded)}",
            file=sys.stderr,
            flush=True,
        )
        result = evaluate_candidate(candidate=variant, loaded=loaded, cache=cache)
        result["recommendation"] = candidate_recommendation(result)
        results.append(result)
    summary = summarize_payload(results)
    return {
        "generated_at": now_iso(),
        "module": "candidate_ab_match_research",
        "status": "completed",
        "days": days,
        "period": {"start": start.isoformat(timespec="seconds"), "end": end.isoformat(timespec="seconds")},
        "symbols": universe,
        "intervals": intervals,
        "coverage": coverage,
        "summary": summary,
        "results": results,
        "safety": safety_payload(),
        "paths": {"json": str(RUNTIME_JSON), "html": str(REPORT_HTML)},
        "operator_note": (
            "No paper ledger hookup is allowed from this run alone. "
            "A/B entries are research adapters; B/v16 OFI/CVD/depth is not modeled here."
        ),
    }


def summarize_payload(results: list[dict[str, Any]]) -> dict[str, Any]:
    recommendation_counts = Counter((row.get("recommendation") or {}).get("action") for row in results)
    decision_counts = Counter()
    best_rows: list[dict[str, Any]] = []
    for result in results:
        standalone = result.get("standalone") or {}
        decision_counts[standalone.get("decision")] += 1
        for row in result.get("ab_matrix") or []:
            decision_counts[row.get("decision")] += 1
            if row.get("mode") != "baseline":
                best_rows.append(row)
    best_rows.sort(
        key=lambda row: (
            safe_float((row.get("delta_vs_baseline") or {}).get("net_profit_usdt")),
            safe_float((row.get("full") or {}).get("profit_factor")),
        ),
        reverse=True,
    )
    return {
        "candidate_variants": len(results),
        "recommendation_counts": dict(recommendation_counts),
        "decision_counts": dict(decision_counts),
        "top_filter_uplifts": compact_rows(best_rows[:12]),
        "manual_paper_change_allowed": False,
        "automatic_tuning_allowed": False,
        "automatic_rollback_allowed": False,
        "automatic_upgrade_allowed": False,
    }


def compact_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        out.append(
            {
                "candidate_id": row.get("candidate_id"),
                "variant": row.get("variant"),
                "strategy": row.get("strategy"),
                "mode": row.get("mode"),
                "interval": row.get("interval"),
                "decision": row.get("decision"),
                "delta_vs_baseline": row.get("delta_vs_baseline"),
                "full": row.get("full"),
                "validation": row.get("validation"),
                "test": row.get("test"),
                "decision_reasons": row.get("decision_reasons"),
            }
        )
    return out


def fmt(value: Any) -> str:
    if isinstance(value, float):
        if math.isfinite(value):
            return f"{value:.4f}"
        return "0"
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)[:260]
    return str(value)


def nested(row: dict[str, Any], key: str) -> Any:
    value: Any = row
    for part in key.split("."):
        value = value.get(part, {}) if isinstance(value, dict) else ""
    return value


def table(rows: list[dict[str, Any]], columns: list[tuple[str, str]], limit: int | None = 200) -> str:
    use = rows[:limit] if limit is not None else rows
    head = "".join(f"<th>{escape(label)}</th>" for _key, label in columns)
    body = []
    for row in use:
        cells = [f"<td>{escape(fmt(nested(row, key)))}</td>" for key, _label in columns]
        body.append("<tr>" + "".join(cells) + "</tr>")
    if not body:
        body.append(f"<tr><td colspan='{len(columns)}'>empty</td></tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def pill(text: Any, cls: str = "") -> str:
    return f"<span class='pill {escape(cls)}'>{escape(fmt(text))}</span>"


def render_candidate_card(result: dict[str, Any]) -> str:
    rec = result.get("recommendation") or {}
    standalone = result.get("standalone") or {}
    matrix = result.get("ab_matrix") or []
    non_baseline = [row for row in matrix if row.get("mode") != "baseline"]
    cols = [
        ("strategy", "策略"),
        ("mode", "模式"),
        ("decision", "决策"),
        ("full.net_profit_usdt", "full净利"),
        ("full.profit_factor", "PF"),
        ("full.max_drawdown_pct", "回撤%"),
        ("full.trades", "交易"),
        ("delta_vs_baseline.net_profit_usdt", "相对基线"),
        ("validation.net_profit_usdt", "验证"),
        ("test.net_profit_usdt", "测试"),
        ("decision_reasons", "原因"),
    ]
    return f"""
    <section class="card">
      <div class="card-head">
        <div>
          <h2>{escape(str(result.get('candidate_title') or result.get('candidate_id')))}</h2>
          <p>{escape(str(result.get('variant') or ''))} · {escape(str(result.get('interval') or ''))} · {escape(str(result.get('source_note') or ''))}</p>
        </div>
        <div class="rec">{pill(rec.get('action'), 'warn')}<small>{escape(str(rec.get('reason') or ''))}</small></div>
      </div>
      <div class="stats">
        <div><span>独立决策</span><b>{escape(str(standalone.get('decision') or ''))}</b></div>
        <div><span>独立净利</span><b>{fmt(nested(standalone, 'full.net_profit_usdt'))}</b></div>
        <div><span>独立PF</span><b>{fmt(nested(standalone, 'full.profit_factor'))}</b></div>
        <div><span>独立交易</span><b>{fmt(nested(standalone, 'full.trades'))}</b></div>
        <div><span>活跃标记</span><b>{fmt(nested(result, 'side_map_stats.active_marks'))}</b></div>
      </div>
      <h3>A/B 匹配矩阵</h3>
      <div class="scroll">{table(non_baseline, cols, None)}</div>
    </section>
    """


def render_html(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    safety = payload.get("safety") or {}
    top_cols = [
        ("candidate_id", "候选"),
        ("variant", "变体"),
        ("strategy", "策略"),
        ("mode", "模式"),
        ("decision", "决策"),
        ("delta_vs_baseline.net_profit_usdt", "净利改善"),
        ("delta_vs_baseline.profit_factor", "PF改善"),
        ("delta_vs_baseline.max_drawdown_pct", "回撤变化"),
        ("full.net_profit_usdt", "full净利"),
        ("validation.net_profit_usdt", "验证"),
        ("test.net_profit_usdt", "测试"),
        ("decision_reasons", "失败原因"),
    ]
    cards = "".join(render_candidate_card(row) for row in payload.get("results") or [])
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>候选策略 x A/B 匹配矩阵</title>
<style>
:root{{--bg:#0b1020;--panel:#11182c;--panel2:#151f36;--text:#e5edf8;--muted:#8fa1ba;--line:#24314d;--up:#22c55e;--down:#ef4444;--warn:#f59e0b;--blue:#38bdf8}}
*{{box-sizing:border-box}}body{{margin:0;background:linear-gradient(180deg,#08101f,#0b1020 42%,#0e1424);color:var(--text);font-family:Segoe UI,Microsoft YaHei,Arial,sans-serif}}
.wrap{{max-width:1440px;margin:0 auto;padding:28px 20px 40px}}
.hero{{display:grid;grid-template-columns:1.3fr .7fr;gap:18px;align-items:stretch;margin-bottom:18px}}
.hero-main,.guard,.card{{background:rgba(17,24,44,.92);border:1px solid var(--line);border-radius:10px;box-shadow:0 14px 40px rgba(0,0,0,.22)}}
.hero-main{{padding:24px}}h1{{margin:0 0 8px;font-size:30px;letter-spacing:0}}p{{color:var(--muted);margin:0;line-height:1.55}}.guard{{padding:18px}}
.kpis,.stats{{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin-top:18px}}
.kpis div,.stats div{{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:12px;min-height:72px}}
span{{display:block;color:var(--muted);font-size:12px}}b{{font-size:20px}}small{{display:block;color:var(--muted);margin-top:6px;line-height:1.45}}
.pill{{display:inline-block;padding:4px 9px;border-radius:999px;background:#1f2b46;color:#dce8fb;font-size:12px;white-space:nowrap}}.pill.warn{{background:rgba(245,158,11,.16);color:#facc15}}
.card{{padding:18px;margin-top:18px}}.card-head{{display:flex;justify-content:space-between;gap:18px;align-items:flex-start}}h2{{font-size:21px;margin:0 0 6px}}h3{{font-size:16px;margin:18px 0 10px;color:#d8e5f5}}
.rec{{text-align:right;min-width:260px}}.scroll{{max-height:420px;overflow:auto;border:1px solid var(--line);border-radius:8px}}
table{{width:100%;border-collapse:collapse;font-size:12px}}th,td{{border-bottom:1px solid var(--line);padding:9px 10px;text-align:left;vertical-align:top}}th{{position:sticky;top:0;background:#16213a;color:#aebfda;z-index:1}}td{{color:#dce6f5}}tr:hover td{{background:#13203a}}
.guard ul{{padding-left:18px;margin:10px 0 0;color:#c9d6ea;line-height:1.7}}
@media(max-width:900px){{.hero{{grid-template-columns:1fr}}.kpis,.stats{{grid-template-columns:repeat(2,minmax(0,1fr))}}.card-head{{display:block}}.rec{{text-align:left;margin-top:10px}}}}
</style>
</head>
<body><main class="wrap">
<section class="hero">
  <div class="hero-main">
    <h1>候选策略 x A/B 匹配矩阵</h1>
    <p>先本地证据，后账本接入。当前只看 L1 负跳反弹、E/4h 横截面强弱、压缩突破三条线索是否能独立成立，或是否能作为 A/B 过滤器改善结果。</p>
    <div class="kpis">
      <div><span>候选变体</span><b>{fmt(summary.get('candidate_variants'))}</b></div>
      <div><span>manual paper change</span><b>{fmt(summary.get('manual_paper_change_allowed'))}</b></div>
      <div><span>auto tuning</span><b>{fmt(summary.get('automatic_tuning_allowed'))}</b></div>
      <div><span>auto rollback</span><b>{fmt(summary.get('automatic_rollback_allowed'))}</b></div>
      <div><span>auto upgrade</span><b>{fmt(summary.get('automatic_upgrade_allowed'))}</b></div>
    </div>
  </div>
  <aside class="guard">
    <h2>硬约束</h2>
    <ul>
      <li>不接 paper，不改 A/B/C，不改 config。</li>
      <li>B/v16 仍是 research adapter：OFI/CVD/depth 未在此矩阵建模。</li>
      <li>任何通过项也只是 manual review，不允许自动升级/回滚/调参。</li>
      <li>生成时间：{escape(str(payload.get('generated_at') or ''))}</li>
    </ul>
  </aside>
</section>
<section class="card">
  <h2>最佳过滤改善排行</h2>
  <div class="scroll">{table(summary.get('top_filter_uplifts') or [], top_cols, None)}</div>
</section>
{cards}
<section class="card">
  <h2>安全状态</h2>
  <div class="scroll">{table([safety], [(k,k) for k in safety.keys()], None)}</div>
</section>
</main></body></html>"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local candidate x A/B match research.")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--intervals", default="1h,4h")
    parser.add_argument("--symbols", default="")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root = Path(args.root).resolve()
    intervals = parse_csv(args.intervals, DEFAULT_INTERVALS)
    symbols = parse_csv(args.symbols, []) if args.symbols else None
    payload = build_payload(root, args.days, intervals, symbols)
    write_json(RUNTIME_JSON, payload)
    REPORT_HTML.parent.mkdir(parents=True, exist_ok=True)
    REPORT_HTML.write_text(render_html(payload), encoding="utf-8")
    print(json.dumps({"status": payload["status"], "json": str(RUNTIME_JSON), "html": str(REPORT_HTML)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
