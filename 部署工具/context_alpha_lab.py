"""Local context alpha lab.

Research-only context discovery runner. It evaluates market context hypotheses
from public research directions: cross-sectional momentum, market breadth,
relative BTC/ETH strength, range-breakout context, and compression-breakout
context. Each context must pass multiple gates:

1. raw forward-return discovery;
2. matched random baseline under similar symbol/month/regime/rank/volume/breadth;
3. train/validation/test split stability;
4. anti-concentration checks by symbol and month.

The output is evidence for future strategy rebuilds, not a strategy. This tool
never calls Binance, mutates live config, restarts services, places orders, or
enables automatic tuning/rollback/upgrade.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import statistics
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
import d_e_f_historical_research_report as shared
import signal_edge_lab


CST = timezone(timedelta(hours=8))
DEFAULT_INTERVALS = ["15m", "30m", "1h", "4h"]
HORIZONS = [1, 3, 6, 12]
MIN_FEATURE_IDX = 240
RUNTIME_JSON = ROOT / "runtime" / "context_alpha_lab_latest.json"
PROGRESS_JSON = ROOT / "runtime" / "context_alpha_lab_progress_latest.json"
REPORT_HTML = ROOT / "reports" / "context_alpha_lab_latest.html"


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def safe_float(value: Any, default: float = 0.0) -> float:
    return backtest_engine.safe_float(value, default)


def safe_int(value: Any, default: int = 0) -> int:
    return backtest_engine.safe_int(value, default)


def pct(a: float, b: float) -> float:
    return (b - a) / a * 100.0 if a > 0 else 0.0


def directional_return(a: float, b: float, side: str) -> float:
    raw = pct(a, b)
    return raw if side == "long" else -raw


def month_from_ts(value: Any) -> str:
    return str(value or "")[:7] or "unknown"


def bucket(value: float, size: float = 20.0) -> int:
    if not math.isfinite(value):
        return 0
    return int(max(0.0, min(100.0, value)) // size)


def stable_seed(*parts: Any) -> int:
    text = "|".join(str(part) for part in parts)
    return int(hashlib.sha1(text.encode("utf-8")).hexdigest()[:12], 16)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def write_progress(status: str, completed: int, total: int, latest: str = "", events: int = 0) -> None:
    write_json(
        PROGRESS_JSON,
        {
            "generated_at": now_iso(),
            "module": "context_alpha_lab",
            "status": status,
            "completed_intervals": completed,
            "total_intervals": total,
            "progress_pct": round(completed / max(1, total) * 100.0, 3),
            "latest": latest,
            "events": events,
            "safety": safety_payload(),
        },
    )


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


def add_returns(features: dict[str, list[float]], windows: list[int]) -> None:
    closes = features["closes"]
    for window in windows:
        out = [0.0 for _ in closes]
        for idx in range(window, len(closes)):
            out[idx] = pct(closes[idx - window], closes[idx])
        features[f"ret{window}"] = out


def build_features(bars: list[dict[str, Any]]) -> dict[str, list[float]]:
    features = signal_edge_lab.build_edge_features(bars)
    add_returns(features, [1, 3, 6, 12, 24, 48, 72])
    return features


def split_label(open_ms: int, start_ms: int, end_ms: int) -> str:
    if end_ms <= start_ms:
        return "train"
    frac = (open_ms - start_ms) / (end_ms - start_ms)
    if frac < 0.60:
        return "train"
    if frac < 0.80:
        return "validation"
    return "test"


def forward_ret(features: dict[str, list[float]], idx: int, horizon: int, side: str) -> float | None:
    if idx + horizon >= len(features["closes"]):
        return None
    entry = features["closes"][idx]
    exit_price = features["closes"][idx + horizon]
    if entry <= 0 or exit_price <= 0:
        return None
    return directional_return(entry, exit_price, side)


def rolling_high(bars: list[dict[str, Any]], idx: int, lookback: int) -> float:
    if idx <= 0:
        return 0.0
    rows = bars[max(0, idx - lookback) : idx]
    return max((safe_float(row.get("high")) for row in rows), default=0.0)


def rolling_low(bars: list[dict[str, Any]], idx: int, lookback: int) -> float:
    if idx <= 0:
        return 0.0
    rows = bars[max(0, idx - lookback) : idx]
    return min((safe_float(row.get("low")) for row in rows), default=0.0)


def summarize(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "avg_pct": 0.0, "median_pct": 0.0, "win_rate_pct": 0.0, "p10_pct": 0.0, "p90_pct": 0.0}
    ordered = sorted(values)
    def q(frac: float) -> float:
        pos = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * frac))))
        return ordered[pos]
    return {
        "count": len(values),
        "avg_pct": round(sum(values) / len(values), 6),
        "median_pct": round(statistics.median(values), 6),
        "win_rate_pct": round(sum(1 for value in values if value > 0) / len(values) * 100.0, 3),
        "p10_pct": round(q(0.10), 6),
        "p90_pct": round(q(0.90), 6),
    }


def choose_baselines(index: dict[tuple[Any, ...], list[int]], records: list[dict[str, Any]], event: dict[str, Any], controls: int) -> list[int]:
    rank_bucket = bucket(safe_float(event.get("rank_pct")))
    volume_bucket = bucket(safe_float(event.get("volume_pctile")))
    breadth_bucket = bucket(safe_float(event.get("breadth_above_ma50_pct")))
    base = (
        event.get("symbol"),
        event.get("interval"),
        month_from_ts(event.get("ts")),
        event.get("regime"),
    )
    pool: list[int] = []
    for dr in (0, -1, 1):
        for dv in (0, -1, 1):
            for db in (0, -1, 1):
                key = base + (
                    max(0, min(5, rank_bucket + dr)),
                    max(0, min(5, volume_bucket + dv)),
                    max(0, min(5, breadth_bucket + db)),
                )
                pool.extend(index.get(key, []))
    event_idx = safe_int(event.get("idx"))
    pool = [
        rec_id
        for rec_id in sorted(set(pool))
        if records[rec_id]["symbol"] == event["symbol"] and abs(safe_int(records[rec_id]["idx"]) - event_idx) > 12
    ]
    if not pool:
        return []
    rng = random.Random(stable_seed(event.get("context"), event.get("symbol"), event.get("interval"), event.get("ts"), event.get("side")))
    if len(pool) <= controls:
        return pool
    return rng.sample(pool, controls)


def append_event(events: list[dict[str, Any]], context: str, family: str, side: str, rec: dict[str, Any], extra: dict[str, Any] | None = None) -> None:
    item = {
        "context": context,
        "family": family,
        "side": side,
        "symbol": rec["symbol"],
        "interval": rec["interval"],
        "ts": rec["ts"],
        "open_time_ms": rec["open_time_ms"],
        "idx": rec["idx"],
        "record_id": rec["record_id"],
        "split": rec["split"],
        "regime": rec["regime"],
        "rank_pct": rec["rank_pct"],
        "volume_pctile": rec["volume_pctile"],
        "atr_pctile": rec["atr_pctile"],
        "bb_width_pctile": rec["bb_width_pctile"],
        "breadth_above_ma50_pct": rec["breadth_above_ma50_pct"],
        "breadth_ret12_pos_pct": rec["breadth_ret12_pos_pct"],
        "market_median_ret12_pct": rec["market_median_ret12_pct"],
        "mom12_pct": rec["mom12_pct"],
        "rel_btc_12_pct": rec["rel_btc_12_pct"],
        "rel_eth_12_pct": rec["rel_eth_12_pct"],
    }
    if extra:
        item.update(extra)
    events.append(item)


def generate_interval_events(
    *,
    interval: str,
    loaded: dict[str, list[dict[str, Any]]],
    start_ms: int,
    end_ms: int,
    controls: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[tuple[Any, ...], list[int]], dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    features_by_symbol: dict[str, dict[str, list[float]]] = {}
    bars_by_symbol: dict[str, list[dict[str, Any]]] = {}
    by_time: dict[int, dict[str, int]] = defaultdict(dict)
    for symbol, bars in loaded.items():
        if len(bars) < MIN_FEATURE_IDX + max(HORIZONS) + 2:
            continue
        features = build_features(bars)
        features_by_symbol[symbol] = features
        bars_by_symbol[symbol] = bars
        for idx in range(MIN_FEATURE_IDX, len(bars) - max(HORIZONS) - 1):
            by_time[safe_int(bars[idx].get("open_time_ms"))][symbol] = idx

    records: list[dict[str, Any]] = []
    baseline_index: dict[tuple[Any, ...], list[int]] = defaultdict(list)
    events: list[dict[str, Any]] = []
    for open_ms in sorted(by_time):
        here = by_time[open_ms]
        if len(here) < 12:
            continue
        score_rows: list[dict[str, Any]] = []
        btc_mom = 0.0
        eth_mom = 0.0
        if "BTCUSDT" in here:
            btc_mom = features_by_symbol["BTCUSDT"]["ret12"][here["BTCUSDT"]]
        if "ETHUSDT" in here:
            eth_mom = features_by_symbol["ETHUSDT"]["ret12"][here["ETHUSDT"]]
        for symbol, idx in here.items():
            features = features_by_symbol[symbol]
            close = features["closes"][idx]
            if close <= 0:
                continue
            score_rows.append(
                {
                    "symbol": symbol,
                    "idx": idx,
                    "mom12": features["ret12"][idx],
                    "mom24": features["ret24"][idx],
                    "close": close,
                    "above_ma50": close > features["ma50"][idx] > 0,
                }
            )
        if len(score_rows) < 12:
            continue
        moms = [row["mom12"] for row in score_rows]
        ordered = sorted(score_rows, key=lambda row: row["mom12"])
        rank_pct_by_symbol = {
            row["symbol"]: (rank / max(1, len(ordered) - 1) * 100.0)
            for rank, row in enumerate(ordered)
        }
        breadth_above = sum(1 for row in score_rows if row["above_ma50"]) / len(score_rows) * 100.0
        breadth_ret_pos = sum(1 for row in score_rows if row["mom12"] > 0) / len(score_rows) * 100.0
        median_mom = statistics.median(moms)
        recs_at_time: list[dict[str, Any]] = []
        for row in score_rows:
            symbol = row["symbol"]
            idx = row["idx"]
            bars = bars_by_symbol[symbol]
            features = features_by_symbol[symbol]
            regime = signal_edge_lab.regime_v2(features, idx)
            rec = {
                "record_id": len(records),
                "symbol": symbol,
                "interval": interval,
                "idx": idx,
                "ts": bars[idx].get("ts"),
                "open_time_ms": open_ms,
                "split": split_label(open_ms, start_ms, end_ms),
                "regime": regime,
                "rank_pct": round(rank_pct_by_symbol[symbol], 3),
                "volume_pctile": round(features["volume_pctile"][idx], 3),
                "atr_pctile": round(features["atr_pctile"][idx], 3),
                "bb_width_pctile": round(features["bb_width_pctile"][idx], 3),
                "breadth_above_ma50_pct": round(breadth_above, 3),
                "breadth_ret12_pos_pct": round(breadth_ret_pos, 3),
                "market_median_ret12_pct": round(median_mom, 6),
                "mom12_pct": round(row["mom12"], 6),
                "mom24_pct": round(row["mom24"], 6),
                "rel_btc_12_pct": round(row["mom12"] - btc_mom, 6),
                "rel_eth_12_pct": round(row["mom12"] - eth_mom, 6),
            }
            records.append(rec)
            recs_at_time.append(rec)
            key = (
                symbol,
                interval,
                month_from_ts(rec["ts"]),
                regime,
                bucket(rec["rank_pct"]),
                bucket(rec["volume_pctile"]),
                bucket(rec["breadth_above_ma50_pct"]),
            )
            baseline_index[key].append(rec["record_id"])

        for rec in recs_at_time:
            symbol = rec["symbol"]
            idx = rec["idx"]
            bars = bars_by_symbol[symbol]
            features = features_by_symbol[symbol]
            close = features["closes"][idx]
            rank_pct = safe_float(rec["rank_pct"])
            volp = safe_float(rec["volume_pctile"])
            breadth = safe_float(rec["breadth_above_ma50_pct"])
            median_mom12 = safe_float(rec["market_median_ret12_pct"])
            rel_btc = safe_float(rec["rel_btc_12_pct"])
            rel_eth = safe_float(rec["rel_eth_12_pct"])
            reg = str(rec["regime"])

            if rank_pct >= 80 and safe_float(rec["mom12_pct"]) > 0:
                append_event(events, "X1_top20_momentum_long", "cross_sectional", "long", rec)
            if rank_pct <= 20 and safe_float(rec["mom12_pct"]) < 0:
                append_event(events, "X1_bottom20_momentum_short", "cross_sectional", "short", rec)
            if rank_pct >= 80 and breadth >= 55 and median_mom12 > 0:
                append_event(events, "X2_top20_breadth_confirm_long", "breadth", "long", rec)
            if rank_pct <= 20 and breadth <= 45 and median_mom12 < 0:
                append_event(events, "X2_bottom20_breadth_confirm_short", "breadth", "short", rec)
            if rank_pct >= 65 and volp >= 50 and rel_btc >= 0.60 and rel_eth >= -0.20:
                append_event(events, "X3_relative_btc_strength_long", "relative_strength", "long", rec)
            if rank_pct <= 35 and volp >= 50 and rel_btc <= -0.60 and rel_eth <= 0.20:
                append_event(events, "X3_relative_btc_weakness_short", "relative_strength", "short", rec)
            if reg == "range_chop_v2" and volp >= 55 and close > rolling_high(bars, idx, 12):
                append_event(events, "X4_range_chop_breakout_long", "regime_breakout", "long", rec)
            if reg == "range_chop_v2" and volp >= 55 and close < rolling_low(bars, idx, 12):
                append_event(events, "X4_range_chop_breakout_short", "regime_breakout", "short", rec)
            prev_reg = signal_edge_lab.regime_v2(features, idx - 1) if idx > MIN_FEATURE_IDX else ""
            if prev_reg == "compression_tight_v2" and reg == "range_chop_v2" and close > rolling_high(bars, idx, 48):
                append_event(events, "X5_compression_to_range_breakout_long", "compression_transition", "long", rec)
            if prev_reg == "compression_tight_v2" and reg == "range_chop_v2" and close < rolling_low(bars, idx, 48):
                append_event(events, "X5_compression_to_range_breakout_short", "compression_transition", "short", rec)
            if rank_pct >= 70 and breadth >= 65 and safe_float(rec["breadth_ret12_pos_pct"]) >= 60:
                append_event(events, "X6_breadth_thrust_top_long", "breadth", "long", rec)
            if rank_pct <= 30 and breadth <= 35 and safe_float(rec["breadth_ret12_pos_pct"]) <= 40:
                append_event(events, "X6_breadth_crack_bottom_short", "breadth", "short", rec)
            if reg == "range_chop_v2" and rank_pct <= 20 and features["ret3"][idx] <= -0.8 and volp < 75:
                append_event(events, "X7_range_oversold_reversion_long", "mean_reversion", "long", rec)
            if reg == "range_chop_v2" and rank_pct >= 80 and features["ret3"][idx] >= 0.8 and volp < 75:
                append_event(events, "X7_range_overbought_reversion_short", "mean_reversion", "short", rec)
    return events, records, baseline_index, features_by_symbol, bars_by_symbol


def evaluate_events(
    events: list[dict[str, Any]],
    records: list[dict[str, Any]],
    baseline_index: dict[tuple[Any, ...], list[int]],
    features_by_symbol: dict[str, dict[str, list[float]]],
    controls: int,
) -> list[dict[str, Any]]:
    stores: dict[tuple[str, str, int], dict[str, Any]] = defaultdict(
        lambda: {
            "signal": [],
            "baseline": [],
            "splits": defaultdict(list),
            "symbols": Counter(),
            "months": Counter(),
            "regimes": Counter(),
            "families": Counter(),
        }
    )
    for event in events:
        baselines = choose_baselines(baseline_index, records, event, controls)
        if not baselines:
            continue
        side = str(event.get("side"))
        event_features = features_by_symbol[str(event["symbol"])]
        for horizon in HORIZONS:
            sig_ret = forward_ret(event_features, safe_int(event["idx"]), horizon, side)
            if sig_ret is None:
                continue
            base_rets = []
            for rec_id in baselines:
                rec = records[rec_id]
                ret = forward_ret(features_by_symbol[str(rec["symbol"])], safe_int(rec["idx"]), horizon, side)
                if ret is not None:
                    base_rets.append(ret)
            if not base_rets:
                continue
            key = (str(event["context"]), str(event["interval"]), horizon)
            store = stores[key]
            store["signal"].append(sig_ret)
            store["baseline"].extend(base_rets)
            store["splits"][str(event["split"])].append(sig_ret)
            store["symbols"][str(event["symbol"])] += 1
            store["months"][month_from_ts(event["ts"])] += 1
            store["regimes"][str(event["regime"])] += 1
            store["families"][str(event["family"])] += 1
    rows = []
    for (context, interval, horizon), store in stores.items():
        signal_stats = summarize(store["signal"])
        baseline_stats = summarize(store["baseline"])
        split_stats = {name: summarize(values) for name, values in store["splits"].items()}
        symbol_total = max(1, sum(store["symbols"].values()))
        month_total = max(1, sum(store["months"].values()))
        max_symbol_share = max(store["symbols"].values(), default=0) / symbol_total * 100.0
        max_month_share = max(store["months"].values(), default=0) / month_total * 100.0
        top_symbols = [{"symbol": key, "count": value} for key, value in store["symbols"].most_common(8)]
        top_months = [{"month": key, "count": value} for key, value in store["months"].most_common(8)]
        row = {
            "context": context,
            "family": next(iter(store["families"]), ""),
            "interval": interval,
            "horizon_bars": horizon,
            "signal_stats": signal_stats,
            "baseline_stats": baseline_stats,
            "split_stats": split_stats,
            "uplift_avg_pct": round(signal_stats["avg_pct"] - baseline_stats["avg_pct"], 6),
            "uplift_median_pct": round(signal_stats["median_pct"] - baseline_stats["median_pct"], 6),
            "uplift_win_rate_pct": round(signal_stats["win_rate_pct"] - baseline_stats["win_rate_pct"], 6),
            "tail_delta_p10_pct": round(signal_stats["p10_pct"] - baseline_stats["p10_pct"], 6),
            "max_symbol_share_pct": round(max_symbol_share, 3),
            "max_month_share_pct": round(max_month_share, 3),
            "top_symbols": top_symbols,
            "top_months": top_months,
            "regime_counts": dict(store["regimes"]),
        }
        decision, gates = context_decision(row)
        row["decision"] = decision
        row["failed_gates"] = gates
        rows.append(row)
    rows.sort(
        key=lambda item: (
            item["decision"] != "context_candidate",
            item["decision"] != "watchlist",
            -safe_float(item.get("uplift_avg_pct")),
            -safe_float(item.get("uplift_median_pct")),
        )
    )
    return rows


def context_decision(row: dict[str, Any]) -> tuple[str, list[str]]:
    failed: list[str] = []
    signal = row.get("signal_stats") or {}
    baseline = row.get("baseline_stats") or {}
    count = safe_int(signal.get("count"))
    interval = str(row.get("interval"))
    min_count = 120 if interval == "4h" else 250
    if count < min_count:
        failed.append("sample_count_low")
    if safe_float(signal.get("avg_pct")) <= 0:
        failed.append("signal_avg_not_positive")
    if safe_float(signal.get("median_pct")) <= 0:
        failed.append("signal_median_not_positive")
    if safe_float(signal.get("win_rate_pct")) < 50.0:
        failed.append("signal_win_rate_below_50pct")
    if safe_int(baseline.get("count")) < count * 2:
        failed.append("baseline_coverage_low")
    if safe_float(row.get("uplift_avg_pct")) <= 0.05:
        failed.append("avg_uplift_too_small")
    if safe_float(row.get("uplift_median_pct")) <= 0:
        failed.append("median_uplift_not_positive")
    if safe_float(row.get("uplift_win_rate_pct")) < 2.0:
        failed.append("win_rate_uplift_below_2pct")
    if safe_float(row.get("tail_delta_p10_pct")) < -0.25:
        failed.append("tail_risk_worse_than_baseline")
    for split in ["train", "validation", "test"]:
        stats = (row.get("split_stats") or {}).get(split) or {}
        if safe_int(stats.get("count")) < max(20, int(min_count * 0.10)):
            failed.append(f"{split}_sample_low")
        if safe_float(stats.get("avg_pct")) <= 0:
            failed.append(f"{split}_avg_not_positive")
        if safe_float(stats.get("median_pct")) <= -0.05:
            failed.append(f"{split}_median_too_weak")
    if safe_float(row.get("max_symbol_share_pct")) > 25.0:
        failed.append("symbol_concentration_high")
    if safe_float(row.get("max_month_share_pct")) > 35.0:
        failed.append("month_concentration_high")
    if not failed:
        return "context_candidate", []
    hard_failed = {"sample_count_low", "baseline_coverage_low"}
    if any(item in failed for item in hard_failed):
        return "rejected", sorted(set(failed))
    matched_failed = {
        "avg_uplift_too_small",
        "median_uplift_not_positive",
        "win_rate_uplift_below_2pct",
        "tail_risk_worse_than_baseline",
    }
    if not any(item in failed for item in matched_failed):
        return "watchlist", sorted(set(failed))
    if safe_float(row.get("uplift_avg_pct")) > 0 and safe_float(row.get("uplift_median_pct")) > 0:
        return "watchlist", sorted(set(failed))
    return "rejected", sorted(set(failed))


def build_payload(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    end = datetime.now(CST)
    start = end - timedelta(days=max(30, args.days))
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    symbols = shared.universe_symbols(root, None)
    intervals = [item.strip() for item in str(args.intervals).split(",") if item.strip()]
    all_rows: list[dict[str, Any]] = []
    total_events = 0
    coverage: list[dict[str, Any]] = []
    write_progress("running", 0, len(intervals))
    for pos, interval in enumerate(intervals, start=1):
        loaded = shared.load_interval_bars(root=root, symbols=symbols, interval=interval, start=start, end=end)
        coverage.extend(shared.coverage_rows({interval: loaded}))
        events, records, baseline_index, features_by_symbol, _bars_by_symbol = generate_interval_events(
            interval=interval,
            loaded=loaded,
            start_ms=start_ms,
            end_ms=end_ms,
            controls=args.controls,
        )
        total_events += len(events)
        rows = evaluate_events(events, records, baseline_index, features_by_symbol, args.controls)
        all_rows.extend(rows)
        write_progress("running", pos, len(intervals), latest=interval, events=total_events)
    all_rows.sort(
        key=lambda item: (
            item["decision"] != "context_candidate",
            item["decision"] != "watchlist",
            -safe_float(item.get("uplift_avg_pct")),
            -safe_float(item.get("uplift_median_pct")),
        )
    )
    decision_counts = dict(Counter(row["decision"] for row in all_rows))
    failed_gate_counts: Counter[str] = Counter()
    for row in all_rows:
        failed_gate_counts.update(row.get("failed_gates") or [])
    payload = {
        "generated_at": now_iso(),
        "module": "context_alpha_lab",
        "status": "completed",
        "days": args.days,
        "intervals": intervals,
        "symbols": symbols,
        "event_count": total_events,
        "row_count": len(all_rows),
        "decision_counts": decision_counts,
        "failed_gate_counts": dict(failed_gate_counts),
        "context_candidates": [row for row in all_rows if row["decision"] == "context_candidate"],
        "watchlist": [row for row in all_rows if row["decision"] == "watchlist"],
        "results": all_rows,
        "battle_rounds": {
            "round_1_context_discovery_events": total_events,
            "round_2_matched_rows": len(all_rows),
            "round_3_context_candidates": sum(1 for row in all_rows if row["decision"] == "context_candidate"),
            "round_4_watchlist": sum(1 for row in all_rows if row["decision"] == "watchlist"),
        },
        "research_sources_translated_to_tests": [
            "cross_sectional_momentum_rank_top_bottom",
            "market_breadth_confirmation",
            "relative_strength_vs_btc_eth",
            "range_chop_breakout_context",
            "compression_to_range_breakout_context",
            "breadth_thrust_or_crack",
            "range_mean_reversion_extremes",
        ],
        "next_action_rules": [
            "context_candidate can enter full strategy rebuild only after manual review.",
            "watchlist requires another split or stricter context before full strategy rebuild.",
            "rejected contexts should not be tuned for prettier backtests.",
            "microstructure-only ideas require live OFI/depth history and are not faked from Kline.",
        ],
        "coverage": coverage,
        "safety": safety_payload(),
        "paths": {"json": str(RUNTIME_JSON), "html": str(REPORT_HTML), "progress": str(PROGRESS_JSON)},
    }
    write_progress("completed", len(intervals), len(intervals), latest="completed", events=total_events)
    return payload


def fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.4f}"
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return ", ".join(str(item) for item in value[:3])
    return str(value)


def nested(row: dict[str, Any], path: str) -> Any:
    value: Any = row
    for part in path.split("."):
        value = value.get(part, {}) if isinstance(value, dict) else ""
    return value


def table(rows: list[dict[str, Any]], columns: list[tuple[str, str]], limit: int = 100) -> str:
    use = rows[:limit]
    head = "".join(f"<th>{escape(label)}</th>" for _key, label in columns)
    body = []
    for row in use:
        cells = [f"<td>{escape(fmt(nested(row, key)))}</td>" for key, _label in columns]
        body.append("<tr>" + "".join(cells) + "</tr>")
    if not body:
        body.append(f"<tr><td colspan='{len(columns)}'>empty</td></tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def render_html(payload: dict[str, Any]) -> str:
    cols = [
        ("decision", "决策"),
        ("context", "Context"),
        ("interval", "周期"),
        ("horizon_bars", "未来K"),
        ("signal_stats.count", "样本"),
        ("uplift_avg_pct", "均值提升%"),
        ("uplift_median_pct", "中位提升%"),
        ("uplift_win_rate_pct", "胜率提升%"),
        ("tail_delta_p10_pct", "P10差"),
        ("split_stats.test.avg_pct", "测试均值%"),
        ("split_stats.validation.avg_pct", "验证均值%"),
        ("max_symbol_share_pct", "最大币占比%"),
        ("max_month_share_pct", "最大月占比%"),
    ]
    fail_rows = [{"gate": key, "count": value} for key, value in payload["failed_gate_counts"].items()]
    fail_rows.sort(key=lambda item: item["count"], reverse=True)
    battle_rows = [{"round": key, "value": value} for key, value in payload["battle_rounds"].items()]
    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"/><title>Context Alpha Lab</title>
<style>
body{{margin:0;background:#08111a;color:#e9f1f8;font-family:Segoe UI,Arial,sans-serif}} main{{max-width:1440px;margin:0 auto;padding:28px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px}} .panel{{background:#111b26;border:1px solid #263545;border-radius:8px;padding:16px;overflow:auto}}
h1{{margin:0 0 8px;font-size:28px}} h2{{font-size:18px}} p,li{{color:#91a2b1}} code{{color:#f4c95d}}
table{{width:100%;border-collapse:collapse;font-size:13px}} th,td{{border-bottom:1px solid #263545;padding:8px;text-align:right;white-space:nowrap}} th:first-child,td:first-child{{text-align:left}} th{{color:#91a2b1}}
</style></head><body><main>
<h1>Context Alpha Lab</h1>
<p>Generated: <code>{escape(payload['generated_at'])}</code> / events: <code>{payload['event_count']}</code> / rows: <code>{payload['row_count']}</code> / Local only.</p>
<p>Decision counts: <code>{escape(json.dumps(payload['decision_counts'], ensure_ascii=False))}</code></p>
<section class="grid">
<div class="panel"><h2>多轮博弈结果</h2>{table(battle_rows, [('round','轮次'),('value','数量')], 20)}</div>
<div class="panel"><h2>失败门控</h2>{table(fail_rows, [('gate','门控'),('count','次数')], 40)}</div>
<div class="panel"><h2>Context Candidates</h2>{table(payload['context_candidates'], cols, 80)}</div>
<div class="panel"><h2>Watchlist</h2>{table(payload['watchlist'], cols, 120)}</div>
<div class="panel"><h2>总榜</h2>{table(payload['results'], cols, 180)}</div>
<div class="panel"><h2>下一步规则</h2><ul>{''.join(f'<li>{escape(rule)}</li>' for rule in payload['next_action_rules'])}</ul></div>
</section>
<p>JSON: <code>{escape(payload['paths']['json'])}</code></p>
<p>Progress: <code>{escape(payload['paths']['progress'])}</code></p>
</main></body></html>"""
    return html


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local context alpha lab.")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--intervals", default=",".join(DEFAULT_INTERVALS))
    parser.add_argument("--controls", type=int, default=5)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    payload = build_payload(args.root, args)
    write_json(RUNTIME_JSON, payload)
    REPORT_HTML.parent.mkdir(parents=True, exist_ok=True)
    REPORT_HTML.write_text(render_html(payload), encoding="utf-8")
    print(json.dumps({
        "status": payload["status"],
        "event_count": payload["event_count"],
        "row_count": payload["row_count"],
        "decision_counts": payload["decision_counts"],
        "context_candidates": len(payload["context_candidates"]),
        "watchlist": len(payload["watchlist"]),
        "html": str(REPORT_HTML),
        "json": str(RUNTIME_JSON),
        "progress": str(PROGRESS_JSON),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
