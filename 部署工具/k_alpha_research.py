"""K-line plus microstructure next-phase alpha research.

Local-only research runner for the post X7/J3 phase:

K1. Microstructure availability and short-window edge audit. It uses only
    existing local compact microstructure/depth snapshots and refuses to fake
    two-year order-flow history from Klines.
K2. Signed jump / good-bad volatility forward-edge lab with matched baselines.
K3. Low-frequency time-series momentum with simple volatility scaling.

No Binance, no cloud compute, no live scanner/config mutation, no paper/real
orders, and no automatic tuning/rollback/upgrade.
"""

from __future__ import annotations

import argparse
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
import context_alpha_lab
import d_e_f_historical_research_report as shared
import j_k_l_indicator_research_report as base_ind
import signal_edge_lab


CST = timezone(timedelta(hours=8))
DEFAULT_INTERVALS = ["15m", "30m", "1h", "4h"]
HORIZONS = [1, 3, 6, 12]
MIN_FEATURE_IDX = 240
RUNTIME_JSON = ROOT / "runtime" / "k_alpha_research_latest.json"
PROGRESS_JSON = ROOT / "runtime" / "k_alpha_research_progress_latest.json"
REPORT_HTML = ROOT / "reports" / "k_alpha_research_latest.html"


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


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


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


def write_progress(stage: str, detail: str, completed: int = 0, total: int = 1) -> None:
    write_json(
        PROGRESS_JSON,
        {
            "generated_at": now_iso(),
            "module": "k_alpha_research",
            "stage": stage,
            "detail": detail,
            "completed": completed,
            "total": total,
            "progress_pct": round(completed / max(1, total) * 100.0, 3),
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


def month_from_ts(value: Any) -> str:
    return str(value or "")[:7] or "unknown"


def bucket(value: float, size: float = 20.0) -> int:
    if not math.isfinite(value):
        return 0
    return int(max(0.0, min(100.0, value)) // size)


def summarize(values: list[float]) -> dict[str, Any]:
    clean = [value for value in values if math.isfinite(value)]
    if not clean:
        return {"count": 0, "avg_pct": 0.0, "median_pct": 0.0, "win_rate_pct": 0.0, "p10_pct": 0.0, "p90_pct": 0.0}
    ordered = sorted(clean)

    def q(frac: float) -> float:
        idx = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * frac)))
        return ordered[idx]

    return {
        "count": len(clean),
        "avg_pct": round(sum(clean) / len(clean), 6),
        "median_pct": round(statistics.median(clean), 6),
        "win_rate_pct": round(sum(1 for value in clean if value > 0) / len(clean) * 100.0, 3),
        "p10_pct": round(q(0.10), 6),
        "p90_pct": round(q(0.90), 6),
    }


def add_returns(features: dict[str, list[float]], windows: list[int]) -> None:
    closes = features["closes"]
    for window in windows:
        out = [0.0 for _ in closes]
        for idx in range(window, len(closes)):
            out[idx] = pct(closes[idx - window], closes[idx])
        features[f"ret{window}"] = out


def rolling_std(values: list[float], idx: int, lookback: int) -> float:
    if idx <= 0:
        return 0.0
    start = max(1, idx - lookback + 1)
    window = values[start : idx + 1]
    if len(window) < 3:
        return 0.0
    return statistics.pstdev(window)


def build_features(bars: list[dict[str, Any]]) -> dict[str, list[float]]:
    features = signal_edge_lab.build_edge_features(bars)
    add_returns(features, [1, 3, 6, 12, 24, 48, 72, 96])
    ret1 = features["ret1"]
    pos_vol = [0.0 for _ in ret1]
    neg_vol = [0.0 for _ in ret1]
    for idx in range(len(ret1)):
        start = max(1, idx - 48 + 1)
        window = ret1[start : idx + 1]
        pos = [value for value in window if value > 0]
        neg = [-value for value in window if value < 0]
        pos_vol[idx] = statistics.pstdev(pos) if len(pos) >= 3 else 0.0
        neg_vol[idx] = statistics.pstdev(neg) if len(neg) >= 3 else 0.0
    features["pos_vol48"] = pos_vol
    features["neg_vol48"] = neg_vol
    return features


def forward_ret(features: dict[str, list[float]], idx: int, horizon: int, side: str) -> float | None:
    if idx + horizon >= len(features["closes"]):
        return None
    entry = features["closes"][idx]
    exit_price = features["closes"][idx + horizon]
    if entry <= 0 or exit_price <= 0:
        return None
    return directional_return(entry, exit_price, side)


def choose_baselines(index: dict[tuple[Any, ...], list[int]], records: list[dict[str, Any]], event: dict[str, Any], controls: int) -> list[int]:
    keys = [
        (
            event["symbol"],
            event["interval"],
            month_from_ts(event["ts"]),
            event["regime"],
            bucket(safe_float(event.get("atr_pctile"))),
            bucket(safe_float(event.get("volume_pctile"))),
        ),
        (
            event["symbol"],
            event["interval"],
            month_from_ts(event["ts"]),
            event["regime"],
            bucket(safe_float(event.get("atr_pctile"))),
            "*",
        ),
        (
            event["symbol"],
            event["interval"],
            month_from_ts(event["ts"]),
            event["regime"],
            "*",
            "*",
        ),
    ]
    candidates: list[int] = []
    own_idx = safe_int(event.get("idx"))
    for key in keys:
        for rec_id in index.get(key, []):
            rec = records[rec_id]
            if abs(safe_int(rec.get("idx")) - own_idx) <= 2:
                continue
            candidates.append(rec_id)
        if len(candidates) >= controls * 3:
            break
    if not candidates:
        return []
    rnd = random.Random(context_alpha_lab.stable_seed(event["symbol"], event["interval"], event["idx"], event["context"]))
    rnd.shuffle(candidates)
    return candidates[:controls]


def classify_edge(row: dict[str, Any], *, min_count: int) -> tuple[str, list[str]]:
    failed: list[str] = []
    signal = row.get("signal_stats") or {}
    baseline = row.get("baseline_stats") or {}
    count = safe_int(signal.get("count"))
    if count < min_count:
        failed.append("sample_count_low")
    if safe_int(baseline.get("count")) < count * 2:
        failed.append("baseline_coverage_low")
    if safe_float(signal.get("avg_pct")) <= 0:
        failed.append("signal_avg_not_positive")
    if safe_float(signal.get("median_pct")) <= 0:
        failed.append("signal_median_not_positive")
    if safe_float(signal.get("win_rate_pct")) < 50.0:
        failed.append("signal_win_rate_below_50pct")
    if safe_float(row.get("uplift_avg_pct")) <= 0.04:
        failed.append("avg_uplift_too_small")
    if safe_float(row.get("uplift_median_pct")) <= 0:
        failed.append("median_uplift_not_positive")
    if safe_float(row.get("uplift_win_rate_pct")) < 1.5:
        failed.append("win_rate_uplift_below_1p5pct")
    if safe_float(row.get("tail_delta_p10_pct")) < -0.20:
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
        return "edge_candidate", []
    if "sample_count_low" in failed or "baseline_coverage_low" in failed:
        return "rejected", sorted(set(failed))
    if safe_float(row.get("uplift_avg_pct")) > 0 and safe_float(row.get("uplift_median_pct")) > 0:
        return "watchlist", sorted(set(failed))
    return "rejected", sorted(set(failed))


def evaluate_forward_events(
    events: list[dict[str, Any]],
    records: list[dict[str, Any]],
    baseline_index: dict[tuple[Any, ...], list[int]],
    features_by_key: dict[tuple[str, str], dict[str, list[float]]],
    *,
    controls: int,
    min_count: int,
) -> list[dict[str, Any]]:
    stores: dict[tuple[str, str, int], dict[str, Any]] = defaultdict(
        lambda: {
            "signal": [],
            "baseline": [],
            "splits": defaultdict(list),
            "symbols": Counter(),
            "months": Counter(),
            "regimes": Counter(),
        }
    )
    for event in events:
        baselines = choose_baselines(baseline_index, records, event, controls)
        if not baselines:
            continue
        for horizon in HORIZONS:
            features = features_by_key.get((event["symbol"], event["interval"]))
            if not features:
                continue
            ret = forward_ret(features, safe_int(event.get("idx")), horizon, str(event.get("side")))
            if ret is None:
                continue
            base_rets = []
            for rec_id in baselines:
                rec = records[rec_id]
                base_features = features_by_key.get((rec["symbol"], rec["interval"]))
                if not base_features:
                    continue
                base = forward_ret(base_features, safe_int(rec.get("idx")), horizon, str(event.get("side")))
                if base is not None:
                    base_rets.append(base)
            if not base_rets:
                continue
            key = (event["context"], event["interval"], horizon)
            store = stores[key]
            store["signal"].append(ret)
            store["baseline"].extend(base_rets)
            store["splits"][event["split"]].append(ret)
            store["symbols"][event["symbol"]] += 1
            store["months"][month_from_ts(event["ts"])] += 1
            store["regimes"][event["regime"]] += 1
    rows = []
    for (context, interval, horizon), store in stores.items():
        signal_stats = summarize(store["signal"])
        baseline_stats = summarize(store["baseline"])
        count = safe_int(signal_stats.get("count"))
        split_stats = {name: summarize(values) for name, values in store["splits"].items()}
        row = {
            "context": context,
            "interval": interval,
            "horizon_bars": horizon,
            "signal_stats": signal_stats,
            "baseline_stats": baseline_stats,
            "uplift_avg_pct": round(signal_stats["avg_pct"] - baseline_stats["avg_pct"], 6),
            "uplift_median_pct": round(signal_stats["median_pct"] - baseline_stats["median_pct"], 6),
            "uplift_win_rate_pct": round(signal_stats["win_rate_pct"] - baseline_stats["win_rate_pct"], 6),
            "tail_delta_p10_pct": round(signal_stats["p10_pct"] - baseline_stats["p10_pct"], 6),
            "split_stats": split_stats,
            "max_symbol_share_pct": round(max(store["symbols"].values()) / count * 100.0, 6) if count else 0.0,
            "max_month_share_pct": round(max(store["months"].values()) / count * 100.0, 6) if count else 0.0,
            "top_symbols": [{"symbol": key, "count": value} for key, value in store["symbols"].most_common(8)],
            "top_months": [{"month": key, "count": value} for key, value in store["months"].most_common(8)],
            "regime_counts": dict(store["regimes"]),
        }
        decision, gates = classify_edge(row, min_count=min_count)
        row["decision"] = decision
        row["failed_gates"] = gates
        rows.append(row)
    rows.sort(
        key=lambda item: (
            item["decision"] != "edge_candidate",
            item["decision"] != "watchlist",
            -safe_float(item.get("uplift_avg_pct")),
            -safe_float(item.get("uplift_median_pct")),
        )
    )
    return rows


def load_microstructure_records(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    latest = read_json(root / "runtime" / "market_microstructure_latest.json")
    features = latest.get("features") if isinstance(latest.get("features"), dict) else {}
    for item in features.values():
        if isinstance(item, dict):
            rec = dict(item)
            rec["source_file"] = "runtime/market_microstructure_latest.json"
            rows.append(rec)
    for path in sorted((root / "runtime" / "market_microstructure").glob("*.jsonl")):
        for rec in read_jsonl(path):
            rec["source_file"] = str(path)
            rows.append(rec)
    return rows


def load_depth_records(root: Path) -> list[dict[str, Any]]:
    candidates = list((root / "runtime").glob("**/depth_snapshots/date=*/data.jsonl"))
    rows: list[dict[str, Any]] = []
    for path in sorted(candidates):
        for rec in read_jsonl(path):
            rec["source_file"] = str(path)
            rows.append(rec)
    return rows


def mid_from_depth(row: dict[str, Any]) -> float:
    try:
        bids = json.loads(str(row.get("bids_json") or "[]"))
        asks = json.loads(str(row.get("asks_json") or "[]"))
    except Exception:
        return 0.0
    if not bids or not asks:
        return 0.0
    bid = safe_float(bids[0][0])
    ask = safe_float(asks[0][0])
    return (bid + ask) / 2.0 if bid > 0 and ask > 0 else 0.0


def depth_imbalance(row: dict[str, Any], levels: int = 10) -> float:
    try:
        bids = json.loads(str(row.get("bids_json") or "[]"))[:levels]
        asks = json.loads(str(row.get("asks_json") or "[]"))[:levels]
    except Exception:
        return 0.0
    bid_qty = sum(safe_float(item[1]) for item in bids)
    ask_qty = sum(safe_float(item[1]) for item in asks)
    denom = bid_qty + ask_qty
    return (bid_qty - ask_qty) / denom if denom > 0 else 0.0


def nearest_forward_return(bars: list[dict[str, Any]], sample_ms: int, horizon: int, side: str) -> float | None:
    if not bars:
        return None
    idx = None
    for pos, bar in enumerate(bars):
        if safe_int(bar.get("open_time_ms")) >= sample_ms:
            idx = pos
            break
    if idx is None or idx + horizon >= len(bars):
        return None
    entry = safe_float(bars[idx].get("open"), safe_float(bars[idx].get("close")))
    exit_price = safe_float(bars[idx + horizon].get("close"))
    if entry <= 0 or exit_price <= 0:
        return None
    return directional_return(entry, exit_price, side)


def run_k1_microstructure(root: Path, days: int) -> dict[str, Any]:
    micro = load_microstructure_records(root)
    depth = load_depth_records(root)
    start = datetime.now(CST) - timedelta(days=max(7, min(days, 30)))
    end = datetime.now(CST)
    symbols = shared.universe_symbols(root, None)
    loaded15 = shared.load_interval_bars(root=root, symbols=symbols, interval="15m", start=start, end=end)
    records = []
    for rec in micro:
        symbol = str(rec.get("symbol") or "").upper()
        ts = rec.get("unix_ts")
        sample_ms = int(safe_float(ts) * 1000) if ts else backtest_engine.iso_to_ms(rec.get("ts"))
        ofi = safe_float(rec.get("ofi"))
        cvd = safe_float(rec.get("cvd"))
        if abs(ofi) >= 0.20:
            side = "long" if ofi > 0 else "short"
            ret = nearest_forward_return(loaded15.get(symbol, []), sample_ms, 3, side)
            if ret is not None:
                records.append({"signal": "K1_ofi_abs_0p20", "symbol": symbol, "side": side, "ret_pct": ret})
        if abs(cvd) >= 0.50:
            side = "long" if cvd > 0 else "short"
            ret = nearest_forward_return(loaded15.get(symbol, []), sample_ms, 3, side)
            if ret is not None:
                records.append({"signal": "K1_cvd_abs_0p50", "symbol": symbol, "side": side, "ret_pct": ret})
        if abs(ofi) >= 0.15 and abs(cvd) >= 0.50 and ofi * cvd > 0:
            side = "long" if ofi > 0 else "short"
            ret = nearest_forward_return(loaded15.get(symbol, []), sample_ms, 3, side)
            if ret is not None:
                records.append({"signal": "K1_ofi_cvd_agree", "symbol": symbol, "side": side, "ret_pct": ret})
    for rec in depth:
        symbol = str(rec.get("symbol") or "").upper()
        sample_ms = safe_int(rec.get("snapshot_time_ms"))
        imb = depth_imbalance(rec)
        if abs(imb) >= 0.10:
            side = "long" if imb > 0 else "short"
            ret = nearest_forward_return(loaded15.get(symbol, []), sample_ms, 3, side)
            if ret is not None:
                records.append({"signal": "K1_depth_imbalance_abs_0p10", "symbol": symbol, "side": side, "ret_pct": ret})
    by_signal: dict[str, list[float]] = defaultdict(list)
    for rec in records:
        by_signal[rec["signal"]].append(safe_float(rec.get("ret_pct")))
    rows = []
    for signal, values in by_signal.items():
        stats = summarize(values)
        decision = "microstructure_data_gap"
        if safe_int(stats.get("count")) >= 80 and safe_float(stats.get("avg_pct")) > 0 and safe_float(stats.get("median_pct")) > 0:
            decision = "microstructure_watchlist"
        rows.append({"signal": signal, "decision": decision, **stats})
    rows.sort(key=lambda item: safe_float(item.get("avg_pct")), reverse=True)
    return {
        "module": "K1_microstructure_short_window",
        "status": "completed",
        "microstructure_records": len(micro),
        "depth_records": len(depth),
        "aligned_forward_records": len(records),
        "decision": "data_gap" if len(records) < 80 else "short_window_only",
        "reason": "Microstructure history is compact/latest only; no two-year OFI/depth backtest is claimed.",
        "rows": rows,
    }


def generate_k2_interval(interval: str, loaded: dict[str, list[dict[str, Any]]], start_ms: int, end_ms: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[tuple[Any, ...], list[int]], dict[tuple[str, str], dict[str, list[float]]]]:
    records: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    baseline_index: dict[tuple[Any, ...], list[int]] = defaultdict(list)
    features_by_key: dict[tuple[str, str], dict[str, list[float]]] = {}
    for symbol, bars in loaded.items():
        if len(bars) < MIN_FEATURE_IDX + max(HORIZONS) + 2:
            continue
        features = build_features(bars)
        features_by_key[(symbol, interval)] = features
        for idx in range(MIN_FEATURE_IDX, len(bars) - max(HORIZONS) - 1):
            open_ms = safe_int(bars[idx].get("open_time_ms"))
            ret1 = features["ret1"][idx]
            sigma = rolling_std(features["ret1"], idx, 96)
            regime = signal_edge_lab.regime_v2(features, idx)
            rec = {
                "record_id": len(records),
                "symbol": symbol,
                "interval": interval,
                "idx": idx,
                "ts": bars[idx].get("ts"),
                "open_time_ms": open_ms,
                "split": context_alpha_lab.split_label(open_ms, start_ms, end_ms),
                "regime": regime,
                "ret1_pct": round(ret1, 6),
                "ret12_pct": round(features["ret12"][idx], 6),
                "sigma96_pct": round(sigma, 6),
                "volume_pctile": round(features["volume_pctile"][idx], 3),
                "atr_pctile": round(features["atr_pctile"][idx], 3),
                "pos_vol48": round(features["pos_vol48"][idx], 6),
                "neg_vol48": round(features["neg_vol48"][idx], 6),
            }
            records.append(rec)
            for key in [
                (symbol, interval, month_from_ts(rec["ts"]), regime, bucket(rec["atr_pctile"]), bucket(rec["volume_pctile"])),
                (symbol, interval, month_from_ts(rec["ts"]), regime, bucket(rec["atr_pctile"]), "*"),
                (symbol, interval, month_from_ts(rec["ts"]), regime, "*", "*"),
            ]:
                baseline_index[key].append(rec["record_id"])
            jump_gate = max(0.65 if interval in {"15m", "30m"} else 0.90, sigma * 1.60)
            if ret1 >= jump_gate and rec["volume_pctile"] >= 60:
                events.append({**rec, "context": "K2_positive_signed_jump_continue_long", "side": "long"})
                events.append({**rec, "context": "K2_positive_signed_jump_fade_short", "side": "short"})
            if ret1 <= -jump_gate and rec["volume_pctile"] >= 60:
                events.append({**rec, "context": "K2_negative_signed_jump_continue_short", "side": "short"})
                events.append({**rec, "context": "K2_negative_signed_jump_bounce_long", "side": "long"})
            if features["pos_vol48"][idx] > features["neg_vol48"][idx] * 1.35 and features["ret12"][idx] > 0:
                events.append({**rec, "context": "K2_good_vol_trend_long", "side": "long"})
            if features["neg_vol48"][idx] > features["pos_vol48"][idx] * 1.35 and features["ret12"][idx] < 0:
                events.append({**rec, "context": "K2_bad_vol_trend_short", "side": "short"})
    return events, records, baseline_index, features_by_key


def sample_events(events: list[dict[str, Any]], max_per_context: int) -> list[dict[str, Any]]:
    if max_per_context <= 0:
        return events
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        groups[str(event.get("context") or "")].append(event)
    sampled: list[dict[str, Any]] = []
    for context, rows in groups.items():
        if len(rows) <= max_per_context:
            sampled.extend(rows)
            continue
        rnd = random.Random(context_alpha_lab.stable_seed("k2_sample", context, len(rows), max_per_context))
        copied = list(rows)
        rnd.shuffle(copied)
        sampled.extend(copied[:max_per_context])
    sampled.sort(key=lambda item: (str(item.get("interval")), str(item.get("context")), str(item.get("symbol")), safe_int(item.get("idx"))))
    return sampled


def run_k2_signed_jump(root: Path, days: int, intervals: list[str], controls: int, max_events_per_context: int) -> dict[str, Any]:
    end = datetime.now(CST)
    start = end - timedelta(days=max(30, days))
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    symbols = shared.universe_symbols(root, None)
    all_rows: list[dict[str, Any]] = []
    total_events = 0
    coverage = []
    for pos, interval in enumerate(intervals, start=1):
        write_progress("K2", interval, pos - 1, len(intervals))
        loaded = shared.load_interval_bars(root=root, symbols=symbols, interval=interval, start=start, end=end)
        coverage.extend(shared.coverage_rows({interval: loaded}))
        events, records, baseline_index, features = generate_k2_interval(interval, loaded, start_ms, end_ms)
        raw_events = len(events)
        events = sample_events(events, max_events_per_context)
        total_events += len(events)
        rows = evaluate_forward_events(events, records, baseline_index, features, controls=controls, min_count=120 if interval == "4h" else 250)
        for row in rows:
            row["raw_interval_events_before_sampling"] = raw_events
            row["max_events_per_context"] = max_events_per_context
        all_rows.extend(rows)
    all_rows.sort(
        key=lambda item: (
            item["decision"] != "edge_candidate",
            item["decision"] != "watchlist",
            -safe_float(item.get("uplift_avg_pct")),
            -safe_float(item.get("uplift_median_pct")),
        )
    )
    return {
        "module": "K2_signed_jump_good_bad_vol",
        "status": "completed",
        "event_count": total_events,
        "row_count": len(all_rows),
        "decision_counts": dict(Counter(row["decision"] for row in all_rows)),
        "edge_candidates": [row for row in all_rows if row["decision"] == "edge_candidate"],
        "watchlist": [row for row in all_rows if row["decision"] == "watchlist"],
        "results": all_rows,
        "coverage": coverage,
    }


def k3_variants() -> list[dict[str, Any]]:
    base = {
        "trade_size_usdt": 100.0,
        "min_trade_size_usdt": 30.0,
        "max_trade_size_usdt": 120.0,
        "target_atr_pct": 1.2,
        "leverage": 2.0,
        "atr_pct_min": 0.10,
        "atr_pct_max": 6.0,
        "volume_pctile_min": 35.0,
    }
    return [
        {"name": "k3_4h_mom24_hold12", "params": {**base, "lookback_bars": 24, "max_hold_bars": 12, "atr_stop_multiplier": 2.0, "take_profit_atr": 4.0, "trailing_pullback_atr": 1.4, "trailing_activation_atr": 1.0}},
        {"name": "k3_4h_mom48_hold18", "params": {**base, "lookback_bars": 48, "max_hold_bars": 18, "atr_stop_multiplier": 2.2, "take_profit_atr": 5.0, "trailing_pullback_atr": 1.6, "trailing_activation_atr": 1.1}},
        {"name": "k3_4h_mom72_hold24", "params": {**base, "lookback_bars": 72, "max_hold_bars": 24, "atr_stop_multiplier": 2.5, "take_profit_atr": 6.0, "trailing_pullback_atr": 1.8, "trailing_activation_atr": 1.2}},
        {"name": "k3_4h_mom48_low_turnover", "params": {**base, "lookback_bars": 48, "entry_ret_min_pct": 4.0, "max_hold_bars": 24, "atr_stop_multiplier": 2.4, "take_profit_atr": 6.0, "trailing_pullback_atr": 1.8, "trailing_activation_atr": 1.3}},
        {"name": "k3_4h_mom96_slow", "params": {**base, "lookback_bars": 96, "entry_ret_min_pct": 5.0, "max_hold_bars": 30, "atr_stop_multiplier": 2.8, "take_profit_atr": 7.0, "trailing_pullback_atr": 2.0, "trailing_activation_atr": 1.4}},
    ]


def volatility_scaled_size(params: dict[str, Any], atr_pct: float) -> float:
    base = safe_float(params.get("trade_size_usdt"), 100.0)
    target = safe_float(params.get("target_atr_pct"), 1.2)
    raw = base * target / max(0.20, atr_pct)
    return max(safe_float(params.get("min_trade_size_usdt"), 30.0), min(safe_float(params.get("max_trade_size_usdt"), 120.0), raw))


def simulate_k3_symbol(symbol: str, bars: list[dict[str, Any]], variant: dict[str, Any]) -> list[dict[str, Any]]:
    if len(bars) < 260:
        return []
    params = variant["params"]
    features = build_features(bars)
    lookback = max(12, safe_int(params.get("lookback_bars"), 48))
    entry_min = safe_float(params.get("entry_ret_min_pct"), 2.5)
    trades: list[dict[str, Any]] = []
    idx = max(MIN_FEATURE_IDX, lookback + 2)
    while idx < len(bars) - 4 and len(trades) < 500:
        ret = features.get(f"ret{lookback}")
        lookback_ret = ret[idx] if ret else pct(features["closes"][idx - lookback], features["closes"][idx])
        side = ""
        if lookback_ret >= entry_min and features["closes"][idx] > features["ma50"][idx] > 0:
            side = "long"
        elif lookback_ret <= -entry_min and (
            features["closes"][idx] < features["ma50"][idx] or features["ma50"][idx] <= 0
        ):
            side = "short"
        if not side:
            idx += 1
            continue
        atr_value = max(backtest_engine.atr(bars, idx), features["closes"][idx] * 0.001)
        atr_pct = atr_value / max(1e-12, features["closes"][idx]) * 100.0
        if atr_pct < safe_float(params.get("atr_pct_min"), 0.10) or atr_pct > safe_float(params.get("atr_pct_max"), 6.0):
            idx += 1
            continue
        if features["volume_pctile"][idx] < safe_float(params.get("volume_pctile_min"), 35.0):
            idx += 1
            continue
        local_params = dict(params)
        local_params["trade_size_usdt"] = volatility_scaled_size(params, atr_pct)
        trade = base_ind.simulate_indicator_trade(
            strategy="K3/tsmom_vol_scaled",
            adapter="k_alpha_research",
            symbol=symbol,
            interval="4h",
            bars=bars,
            signal_idx=idx,
            side=side,
            params=local_params,
            extra={
                "variant": variant["name"],
                "lookback_ret_pct": round(lookback_ret, 6),
                "atr_pct": round(atr_pct, 6),
                "volume_pctile": round(features["volume_pctile"][idx], 3),
                "regime": signal_edge_lab.regime_v2(features, idx),
                "vol_scaled_size_usdt": round(local_params["trade_size_usdt"], 6),
            },
        )
        if trade:
            trades.append(trade)
            idx += max(1, safe_int(trade.get("bars_held"), 1))
        idx += 1
    return sorted(trades, key=lambda item: str(item.get("exit_ts") or ""))


def run_k3_variant(loaded: dict[str, list[dict[str, Any]]], variant: dict[str, Any], split: str | None) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    for symbol, bars in loaded.items():
        use_bars = shared.split_sequence(bars, split)
        trades.extend(simulate_k3_symbol(symbol, use_bars, variant))
    return sorted(trades, key=lambda item: str(item.get("exit_ts") or ""))


def with_extra_cost(trades: list[dict[str, Any]], extra_bps: float) -> list[dict[str, Any]]:
    out = []
    for trade in trades:
        item = dict(trade)
        qty = abs(safe_float(trade.get("quantity") or trade.get("requested_quantity")))
        entry = abs(safe_float(trade.get("entry_price")))
        exit_price = abs(safe_float(trade.get("exit_price")))
        extra = qty * (entry + exit_price) * extra_bps / 10_000.0
        item["net_pnl_usdt"] = safe_float(item.get("net_pnl_usdt")) - extra
        item["extra_cost_usdt"] = round(extra, 8)
        out.append(item)
    return out


def summarize_costs(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for bps in [0.0, 5.0, 10.0, 20.0]:
        summary, _ = shared.summarize_trades(with_extra_cost(trades, bps))
        rows.append({"extra_cost_bps": bps, **summary})
    return rows


def group_summary(trades: list[dict[str, Any]], key_fn) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        groups[str(key_fn(trade))].append(trade)
    rows = []
    for key, items in groups.items():
        summary, _ = shared.summarize_trades(items)
        rows.append({"key": key, **summary})
    rows.sort(key=lambda item: safe_float(item.get("net_profit_usdt")), reverse=True)
    return rows


def trade_month(trade: dict[str, Any]) -> str:
    return str(trade.get("exit_ts") or trade.get("entry_ts") or "")[:7] or "unknown"


def k3_anti_fit(row: dict[str, Any]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    full = row.get("full") or {}
    if safe_int(full.get("trades")) < 80:
        reasons.append("full_trade_count_low")
    if safe_float(full.get("net_profit_usdt")) <= 0:
        reasons.append("full_net_not_positive")
    if safe_float(full.get("profit_factor")) < 1.15:
        reasons.append("full_pf_below_1.15")
    for split in ["train", "validation", "test"]:
        metrics = row.get(split) or {}
        if safe_int(metrics.get("trades")) < 20:
            reasons.append(f"{split}_trade_count_low")
        if safe_float(metrics.get("net_profit_usdt")) <= 0:
            reasons.append(f"{split}_net_not_positive")
        if safe_float(metrics.get("profit_factor")) < 1.10:
            reasons.append(f"{split}_pf_below_1.10")
    cost10 = next((item for item in row.get("cost_stress") or [] if safe_float(item.get("extra_cost_bps")) == 10.0), {})
    if safe_float(cost10.get("net_profit_usdt")) <= 0:
        reasons.append("cost_10bps_net_not_positive")
    if reasons:
        if safe_float(full.get("net_profit_usdt")) > 0 and safe_float((row.get("test") or {}).get("net_profit_usdt")) > 0:
            return "near_miss", sorted(set(reasons))
        return "rejected", sorted(set(reasons))
    return "research_candidate", []


def evaluate_k3_variant(loaded: dict[str, list[dict[str, Any]]], variant: dict[str, Any]) -> dict[str, Any]:
    full_trades = run_k3_variant(loaded, variant, None)
    train_trades = run_k3_variant(loaded, variant, "train")
    validation_trades = run_k3_variant(loaded, variant, "validation")
    test_trades = run_k3_variant(loaded, variant, "test")
    full, charts = shared.summarize_trades(full_trades)
    train, _ = shared.summarize_trades(train_trades)
    validation, _ = shared.summarize_trades(validation_trades)
    test, _ = shared.summarize_trades(test_trades)
    row = {
        "variant": variant["name"],
        "params": variant["params"],
        "full": full,
        "train": train,
        "validation": validation,
        "test": test,
        "cost_stress": summarize_costs(full_trades),
        "breakdowns": {
            "by_symbol": group_summary(full_trades, lambda t: t.get("symbol") or "unknown"),
            "by_month": group_summary(full_trades, trade_month),
            "by_side": group_summary(full_trades, lambda t: t.get("side") or "unknown"),
            "by_regime": group_summary(full_trades, lambda t: t.get("regime") or "unknown"),
            "by_exit_reason": group_summary(full_trades, lambda t: t.get("exit_reason") or "unknown"),
        },
        "charts": {"equity_curve": charts.get("equity_curve", [])[-400:], "monthly_returns": charts.get("monthly_returns", [])},
    }
    row["robust_score"] = shared.robust_score(row)
    decision, reasons = k3_anti_fit(row)
    row["decision"] = decision
    row["anti_fit_reasons"] = reasons
    return row


def run_k3_tsmom(root: Path, days: int) -> dict[str, Any]:
    end = datetime.now(CST)
    start = end - timedelta(days=max(30, days))
    symbols = shared.universe_symbols(root, None)
    loaded = shared.load_interval_bars(root=root, symbols=symbols, interval="4h", start=start, end=end)
    rows = [evaluate_k3_variant(loaded, variant) for variant in k3_variants()]
    rows.sort(key=lambda item: safe_float(item.get("robust_score")), reverse=True)
    return {
        "module": "K3_tsmom_vol_scaled",
        "status": "completed",
        "interval": "4h",
        "decision_counts": dict(Counter(row["decision"] for row in rows)),
        "results": rows,
        "best": rows[0] if rows else {},
        "coverage": shared.coverage_rows({"4h": loaded}),
    }


def build_payload(root: Path, days: int, intervals: list[str], controls: int, max_events_per_context: int) -> dict[str, Any]:
    write_progress("K1", "microstructure", 0, 3)
    k1 = run_k1_microstructure(root, days)
    write_progress("K2", "signed_jump", 1, 3)
    k2 = run_k2_signed_jump(root, days, intervals, controls, max_events_per_context)
    write_progress("K3", "tsmom", 2, 3)
    k3 = run_k3_tsmom(root, days)
    write_progress("completed", "all", 3, 3)
    return {
        "generated_at": now_iso(),
        "module": "k_alpha_research",
        "status": "completed",
        "days": days,
        "intervals": intervals,
        "controls": controls,
        "max_events_per_context": max_events_per_context,
        "K1": k1,
        "K2": k2,
        "K3": k3,
        "decision": {
            "paper_shadow_allowed": False,
            "automatic_tuning_allowed": False,
            "automatic_rollback_allowed": False,
            "automatic_upgrade_allowed": False,
            "next": "Promote nothing automatically. Review K2 edge candidates and K3 research candidates manually if any survive.",
        },
        "safety": safety_payload(),
        "paths": {"json": str(RUNTIME_JSON), "html": str(REPORT_HTML), "progress": str(PROGRESS_JSON)},
    }


def fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.4f}"
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
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
    edge_cols = [
        ("decision", "决策"),
        ("context", "信号"),
        ("interval", "周期"),
        ("horizon_bars", "未来K"),
        ("signal_stats.count", "样本"),
        ("signal_stats.avg_pct", "信号均值%"),
        ("signal_stats.median_pct", "信号中位%"),
        ("uplift_avg_pct", "均值提升%"),
        ("uplift_median_pct", "中位提升%"),
        ("uplift_win_rate_pct", "胜率提升%"),
        ("tail_delta_p10_pct", "P10差"),
    ]
    k3_cols = [
        ("decision", "决策"),
        ("variant", "变体"),
        ("full.net_profit_usdt", "全样本净利"),
        ("train.net_profit_usdt", "训练"),
        ("validation.net_profit_usdt", "验证"),
        ("test.net_profit_usdt", "测试"),
        ("full.profit_factor", "PF"),
        ("full.max_drawdown_pct", "回撤%"),
        ("full.trades", "交易"),
        ("robust_score", "稳健分"),
    ]
    micro_cols = [
        ("decision", "决策"),
        ("signal", "信号"),
        ("count", "样本"),
        ("avg_pct", "均值%"),
        ("median_pct", "中位%"),
        ("win_rate_pct", "胜率%"),
        ("p10_pct", "P10%"),
    ]
    k1 = payload.get("K1") or {}
    k2 = payload.get("K2") or {}
    k3 = payload.get("K3") or {}
    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"/><title>K Alpha Research</title>
<style>
body{{margin:0;background:#08111a;color:#e9f1f8;font-family:Segoe UI,Arial,sans-serif}} main{{max-width:1440px;margin:0 auto;padding:28px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px}} .panel{{background:#111b26;border:1px solid #263545;border-radius:8px;padding:16px;overflow:auto}}
h1{{margin:0 0 8px;font-size:28px}} h2{{font-size:18px}} p,li{{color:#91a2b1}} code{{color:#f4c95d}}
table{{width:100%;border-collapse:collapse;font-size:13px}} th,td{{border-bottom:1px solid #263545;padding:8px;text-align:right;white-space:nowrap}} th:first-child,td:first-child{{text-align:left}} th{{color:#91a2b1}}
</style></head><body><main>
<h1>K Alpha Research</h1>
<p>Generated: <code>{escape(payload['generated_at'])}</code> / Local only / No Binance / No live mutation.</p>
<p>K1 decision: <code>{escape(str(k1.get('decision')))}</code>, micro records <code>{k1.get('microstructure_records')}</code>, depth records <code>{k1.get('depth_records')}</code>, aligned <code>{k1.get('aligned_forward_records')}</code>.</p>
<p>K2 counts: <code>{escape(json.dumps(k2.get('decision_counts') or {}, ensure_ascii=False))}</code> / K3 counts: <code>{escape(json.dumps(k3.get('decision_counts') or {}, ensure_ascii=False))}</code></p>
<section class="grid">
<div class="panel"><h2>K1 微结构短窗</h2>{table(k1.get('rows') or [], micro_cols, 40)}</div>
<div class="panel"><h2>K2 Edge Candidates</h2>{table(k2.get('edge_candidates') or [], edge_cols, 80)}</div>
<div class="panel"><h2>K2 Watchlist</h2>{table(k2.get('watchlist') or [], edge_cols, 120)}</div>
<div class="panel"><h2>K3 低频趋势</h2>{table(k3.get('results') or [], k3_cols, 50)}</div>
</section>
<p>JSON: <code>{escape(payload['paths']['json'])}</code></p>
<p>Progress: <code>{escape(payload['paths']['progress'])}</code></p>
</main></body></html>"""
    return html


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local K alpha research.")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--intervals", default=",".join(DEFAULT_INTERVALS))
    parser.add_argument("--controls", type=int, default=5)
    parser.add_argument("--max-events-per-context-interval", type=int, default=6000)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    intervals = [item.strip() for item in str(args.intervals).split(",") if item.strip()]
    payload = build_payload(args.root, args.days, intervals, args.controls, args.max_events_per_context_interval)
    write_json(RUNTIME_JSON, payload)
    REPORT_HTML.parent.mkdir(parents=True, exist_ok=True)
    REPORT_HTML.write_text(render_html(payload), encoding="utf-8")
    print(json.dumps({
        "status": payload["status"],
        "k1_decision": payload["K1"].get("decision"),
        "k1_aligned_records": payload["K1"].get("aligned_forward_records"),
        "k2_decision_counts": payload["K2"].get("decision_counts"),
        "k2_edge_candidates": len(payload["K2"].get("edge_candidates") or []),
        "k2_watchlist": len(payload["K2"].get("watchlist") or []),
        "k3_decision_counts": payload["K3"].get("decision_counts"),
        "k3_best": (payload["K3"].get("best") or {}).get("variant"),
        "k3_best_decision": (payload["K3"].get("best") or {}).get("decision"),
        "html": str(REPORT_HTML),
        "json": str(RUNTIME_JSON),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
