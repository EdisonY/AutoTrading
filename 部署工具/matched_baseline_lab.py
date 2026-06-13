"""Matched baseline lab for local signal validation.

Research-only runner. For each signal sample, it compares forward returns with
deterministic matched baseline points from the same symbol, interval, month,
regime, and nearby volatility/volume buckets.

This tool is designed for unattended local runs. It writes progress JSON while
running and final JSON/HTML reports when complete. It never calls Binance,
mutates live config, restarts services, submits orders, or enables automation.
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
RUNTIME_JSON = ROOT / "runtime" / "matched_baseline_lab_latest.json"
PROGRESS_JSON = ROOT / "runtime" / "matched_baseline_lab_progress_latest.json"
REPORT_HTML = ROOT / "reports" / "matched_baseline_lab_latest.html"


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


def month_from_ts(value: Any) -> str:
    return str(value or "")[:7] or "unknown"


def bucket(value: float, size: float = 20.0) -> int:
    if not math.isfinite(value):
        return 0
    return int(max(0, min(100, value)) // size)


def stable_seed(*parts: Any) -> int:
    text = "|".join(str(part) for part in parts)
    return int(hashlib.sha1(text.encode("utf-8")).hexdigest()[:12], 16)


def directional_return(features: dict[str, list[float]], idx: int, horizon: int, side: str) -> float | None:
    if idx + horizon >= len(features["closes"]):
        return None
    entry = features["closes"][idx]
    exit_price = features["closes"][idx + horizon]
    if entry <= 0 or exit_price <= 0:
        return None
    return signal_edge_lab.directional_return(entry, exit_price, side)


def indexed_baselines(bars: list[dict[str, Any]], features: dict[str, list[float]]) -> dict[tuple[str, str, int, int], list[int]]:
    index: dict[tuple[str, str, int, int], list[int]] = defaultdict(list)
    last = len(bars) - max(HORIZONS) - 1
    for idx in range(240, max(240, last)):
        reg = signal_edge_lab.regime_v2(features, idx)
        month = month_from_ts(bars[idx].get("ts"))
        atr_bucket = bucket(features["atr_pctile"][idx])
        volume_bucket = bucket(features["volume_pctile"][idx])
        index[(reg, month, atr_bucket, volume_bucket)].append(idx)
    return index


def nearby_keys(regime: str, month: str, atr_bucket: int, volume_bucket: int) -> list[tuple[str, str, int, int]]:
    keys = []
    for da in (0, -1, 1):
        for dv in (0, -1, 1):
            keys.append((regime, month, max(0, min(5, atr_bucket + da)), max(0, min(5, volume_bucket + dv))))
    return keys


def choose_baselines(
    *,
    index: dict[tuple[str, str, int, int], list[int]],
    signal_row: dict[str, Any],
    signal_idx: int,
    controls: int,
) -> list[int]:
    regime = str(signal_row.get("regime") or "")
    month = month_from_ts(signal_row.get("ts"))
    atr_bucket = bucket(safe_float(signal_row.get("atr_pctile")))
    volume_bucket = bucket(safe_float(signal_row.get("volume_pctile")))
    pool: list[int] = []
    for key in nearby_keys(regime, month, atr_bucket, volume_bucket):
        pool.extend(index.get(key, []))
    pool = [idx for idx in sorted(set(pool)) if abs(idx - signal_idx) > 12]
    if not pool:
        return []
    rng = random.Random(stable_seed(signal_row.get("signal"), signal_row.get("symbol"), signal_row.get("interval"), signal_row.get("ts"), signal_row.get("side")))
    if len(pool) <= controls:
        return pool
    return rng.sample(pool, controls)


def summarize(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "win_rate_pct": 0.0, "avg_pct": 0.0, "median_pct": 0.0, "p10_pct": 0.0, "p90_pct": 0.0}
    ordered = sorted(values)
    def q(frac: float) -> float:
        pos = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * frac))))
        return ordered[pos]
    return {
        "count": len(values),
        "win_rate_pct": round(sum(1 for value in values if value > 0) / len(values) * 100.0, 3),
        "avg_pct": round(sum(values) / len(values), 6),
        "median_pct": round(statistics.median(values), 6),
        "p10_pct": round(q(0.10), 6),
        "p90_pct": round(q(0.90), 6),
    }


def build_comparison(signal_values: list[float], baseline_values: list[float]) -> dict[str, Any]:
    signal = summarize(signal_values)
    baseline = summarize(baseline_values)
    return {
        "signal_stats": signal,
        "baseline_stats": baseline,
        "uplift_avg_pct": round(safe_float(signal.get("avg_pct")) - safe_float(baseline.get("avg_pct")), 6),
        "uplift_median_pct": round(safe_float(signal.get("median_pct")) - safe_float(baseline.get("median_pct")), 6),
        "uplift_win_rate_pct": round(safe_float(signal.get("win_rate_pct")) - safe_float(baseline.get("win_rate_pct")), 6),
        "tail_delta_p10_pct": round(safe_float(signal.get("p10_pct")) - safe_float(baseline.get("p10_pct")), 6),
    }


def quality_decision(row: dict[str, Any]) -> tuple[str, list[str]]:
    reasons = []
    signal_count = safe_int((row.get("signal_stats") or {}).get("count"))
    baseline_count = safe_int((row.get("baseline_stats") or {}).get("count"))
    if signal_count < 100:
        reasons.append("signal_count_low")
    if baseline_count < signal_count:
        reasons.append("baseline_coverage_low")
    if safe_float(row.get("uplift_avg_pct")) <= 0:
        reasons.append("avg_uplift_not_positive")
    if safe_float(row.get("uplift_median_pct")) <= 0:
        reasons.append("median_uplift_not_positive")
    if safe_float(row.get("uplift_win_rate_pct")) < 2.0:
        reasons.append("win_rate_uplift_below_2pct")
    if safe_float(row.get("tail_delta_p10_pct")) < -0.25:
        reasons.append("tail_risk_worse_than_baseline")
    if not reasons:
        return "baseline_outperformer", []
    if safe_float(row.get("uplift_avg_pct")) > 0 and safe_float(row.get("uplift_median_pct")) > 0:
        return "near_miss", sorted(set(reasons))
    return "rejected", sorted(set(reasons))


def append_values(store: dict[tuple[Any, ...], dict[str, list[float]]], key: tuple[Any, ...], horizon: int, signal_return: float, baseline_returns: list[float]) -> None:
    signal_key = f"signal_{horizon}"
    baseline_key = f"baseline_{horizon}"
    store[key][signal_key].append(signal_return)
    store[key][baseline_key].extend(baseline_returns)


def process_symbol_interval(
    *,
    symbol: str,
    interval: str,
    bars: list[dict[str, Any]],
    controls: int,
    max_samples_per_signal_interval: int,
    stores: dict[str, dict[tuple[Any, ...], dict[str, list[float]]]],
) -> int:
    if len(bars) < 260:
        return 0
    features = signal_edge_lab.build_edge_features(bars)
    rows = signal_edge_lab.signal_rows(symbol, interval, bars, features)
    if max_samples_per_signal_interval > 0:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[str(row.get("signal"))].append(row)
        rows = []
        for sig_rows in grouped.values():
            rows.extend(sig_rows[-max_samples_per_signal_interval:])
    index = indexed_baselines(bars, features)
    signal_idx_by_ts = {str(bars[idx].get("ts")): idx for idx in range(len(bars))}
    matched = 0
    for row in rows:
        signal_idx = signal_idx_by_ts.get(str(row.get("ts")))
        if signal_idx is None:
            continue
        baseline_indexes = choose_baselines(index=index, signal_row=row, signal_idx=signal_idx, controls=controls)
        if not baseline_indexes:
            continue
        side = str(row.get("side") or "")
        for horizon in HORIZONS:
            signal_ret = directional_return(features, signal_idx, horizon, side)
            if signal_ret is None:
                continue
            baseline_rets = [item for item in (directional_return(features, idx, horizon, side) for idx in baseline_indexes) if item is not None]
            if not baseline_rets:
                continue
            signal = row.get("signal")
            regime = row.get("regime")
            append_values(stores["by_signal"], (signal, interval, horizon), horizon, signal_ret, baseline_rets)
            append_values(stores["by_regime"], (signal, interval, regime, horizon), horizon, signal_ret, baseline_rets)
            append_values(stores["by_symbol"], (signal, interval, symbol, horizon), horizon, signal_ret, baseline_rets)
        matched += 1
    return matched


def flatten_store(store: dict[tuple[Any, ...], dict[str, list[float]]], keys: list[str]) -> list[dict[str, Any]]:
    rows = []
    for key, values in store.items():
        horizon = safe_int(key[-1])
        comparison = build_comparison(values.get(f"signal_{horizon}", []), values.get(f"baseline_{horizon}", []))
        row = {keys[idx]: key[idx] for idx in range(len(keys))}
        row.update(comparison)
        decision, reasons = quality_decision(row)
        row["decision"] = decision
        row["reasons"] = reasons
        rows.append(row)
    rows.sort(key=lambda item: (item.get("decision") != "baseline_outperformer", -safe_float(item.get("uplift_avg_pct")), -safe_float(item.get("uplift_median_pct"))))
    return rows


def write_progress(*, status: str, completed: int, total: int, matched_samples: int, latest: str = "") -> None:
    payload = {
        "generated_at": now_iso(),
        "module": "matched_baseline_lab",
        "status": status,
        "completed_symbol_intervals": completed,
        "total_symbol_intervals": total,
        "progress_pct": round(completed / max(1, total) * 100.0, 3),
        "matched_samples": matched_samples,
        "latest": latest,
        "safety": safety_payload(),
    }
    write_json(PROGRESS_JSON, payload)


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


def build_payload(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    end = datetime.now(CST)
    start = end - timedelta(days=max(30, args.days))
    symbols = shared.universe_symbols(root, None)
    intervals = [item.strip() for item in str(args.intervals).split(",") if item.strip()]
    stores: dict[str, dict[tuple[Any, ...], dict[str, list[float]]]] = {
        "by_signal": defaultdict(lambda: defaultdict(list)),
        "by_regime": defaultdict(lambda: defaultdict(list)),
        "by_symbol": defaultdict(lambda: defaultdict(list)),
    }
    total = len(symbols) * len(intervals)
    completed = 0
    matched_samples = 0
    coverage: list[dict[str, Any]] = []
    write_progress(status="running", completed=0, total=total, matched_samples=0)
    for interval in intervals:
        loaded = shared.load_interval_bars(root=root, symbols=symbols, interval=interval, start=start, end=end)
        coverage.extend(shared.coverage_rows({interval: loaded}))
        for symbol in symbols:
            latest = f"{symbol} {interval}"
            matched_samples += process_symbol_interval(
                symbol=symbol,
                interval=interval,
                bars=loaded.get(symbol) or [],
                controls=args.controls,
                max_samples_per_signal_interval=args.max_samples_per_signal_interval,
                stores=stores,
            )
            completed += 1
            if completed % max(1, args.progress_every) == 0 or completed == total:
                write_progress(status="running", completed=completed, total=total, matched_samples=matched_samples, latest=latest)
    by_signal = flatten_store(stores["by_signal"], ["signal", "interval", "horizon_bars"])
    by_regime = flatten_store(stores["by_regime"], ["signal", "interval", "regime", "horizon_bars"])
    by_symbol = flatten_store(stores["by_symbol"], ["signal", "interval", "symbol", "horizon_bars"])
    decision_counts = dict(Counter(row["decision"] for row in by_signal))
    payload = {
        "generated_at": now_iso(),
        "module": "matched_baseline_lab",
        "status": "completed",
        "days": args.days,
        "intervals": intervals,
        "controls": args.controls,
        "symbols": symbols,
        "matched_samples": matched_samples,
        "decision_counts": decision_counts,
        "by_signal": by_signal,
        "by_regime": by_regime,
        "by_symbol": by_symbol[:1500],
        "baseline_outperformers": [row for row in by_signal if row.get("decision") == "baseline_outperformer"],
        "near_misses": [row for row in by_signal if row.get("decision") == "near_miss"],
        "coverage": coverage,
        "optimization_backlog": [
            "Add bootstrap confidence intervals for uplift.",
            "Add matched random samples that preserve long/short balance by signal family.",
            "If no signal beats matched baseline, pivot next alpha search to microstructure/cross-sectional features.",
        ],
        "safety": safety_payload(),
        "paths": {"json": str(RUNTIME_JSON), "html": str(REPORT_HTML), "progress": str(PROGRESS_JSON)},
    }
    write_progress(status="completed", completed=completed, total=total, matched_samples=matched_samples, latest="completed")
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
        ("signal", "信号"),
        ("interval", "周期"),
        ("horizon_bars", "未来K"),
        ("signal_stats.count", "信号样本"),
        ("baseline_stats.count", "基线样本"),
        ("uplift_avg_pct", "均值提升%"),
        ("uplift_median_pct", "中位提升%"),
        ("uplift_win_rate_pct", "胜率提升%"),
        ("tail_delta_p10_pct", "P10差"),
    ]
    regime_cols = [
        ("decision", "决策"),
        ("signal", "信号"),
        ("interval", "周期"),
        ("regime", "Regime"),
        ("horizon_bars", "未来K"),
        ("signal_stats.count", "信号样本"),
        ("uplift_avg_pct", "均值提升%"),
        ("uplift_median_pct", "中位提升%"),
        ("uplift_win_rate_pct", "胜率提升%"),
    ]
    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"/><title>Matched Baseline Lab</title>
<style>
body{{margin:0;background:#08111a;color:#e9f1f8;font-family:Segoe UI,Arial,sans-serif}} main{{max-width:1360px;margin:0 auto;padding:28px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px}} .panel{{background:#111b26;border:1px solid #263545;border-radius:8px;padding:16px;overflow:auto}}
h1{{margin:0 0 8px;font-size:28px}} h2{{font-size:18px}} p,li{{color:#91a2b1}} code{{color:#f4c95d}}
table{{width:100%;border-collapse:collapse;font-size:13px}} th,td{{border-bottom:1px solid #263545;padding:8px;text-align:right;white-space:nowrap}} th:first-child,td:first-child{{text-align:left}} th{{color:#91a2b1}}
</style></head><body><main>
<h1>Matched Baseline 验收</h1>
<p>Generated: <code>{escape(payload['generated_at'])}</code> / matched samples: <code>{payload['matched_samples']}</code> / controls: <code>{payload['controls']}</code> / Local only.</p>
<p>Decision counts: <code>{escape(json.dumps(payload['decision_counts'], ensure_ascii=False))}</code></p>
<section class="grid">
<div class="panel"><h2>信号 vs 匹配随机基线</h2>{table(payload['by_signal'], cols, 120)}</div>
<div class="panel"><h2>Baseline Outperformers</h2>{table(payload['baseline_outperformers'], cols, 80)}</div>
<div class="panel"><h2>Near Misses</h2>{table(payload['near_misses'], cols, 80)}</div>
<div class="panel"><h2>Regime Uplift</h2>{table(payload['by_regime'], regime_cols, 160)}</div>
</section>
<p>JSON: <code>{escape(payload['paths']['json'])}</code></p>
<p>Progress: <code>{escape(payload['paths']['progress'])}</code></p>
</main></body></html>"""
    return html


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local matched baseline signal validation.")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--intervals", default=",".join(DEFAULT_INTERVALS))
    parser.add_argument("--controls", type=int, default=5)
    parser.add_argument("--max-samples-per-signal-interval", type=int, default=0)
    parser.add_argument("--progress-every", type=int, default=2)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    payload = build_payload(args.root, args)
    write_json(RUNTIME_JSON, payload)
    REPORT_HTML.parent.mkdir(parents=True, exist_ok=True)
    REPORT_HTML.write_text(render_html(payload), encoding="utf-8")
    print(json.dumps({
        "status": payload["status"],
        "matched_samples": payload["matched_samples"],
        "decision_counts": payload["decision_counts"],
        "baseline_outperformers": len(payload["baseline_outperformers"]),
        "near_misses": len(payload["near_misses"]),
        "html": str(REPORT_HTML),
        "json": str(RUNTIME_JSON),
        "progress": str(PROGRESS_JSON),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
