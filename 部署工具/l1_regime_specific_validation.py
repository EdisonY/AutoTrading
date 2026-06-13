"""L1 pre-registered regime-specific validation.

Local-only follow-up after K2/J3/X7 produced structure clues but no promotable
strategy. This runner tests a fixed hypothesis registry through forward returns,
matched baselines, train/validation/test stability, and concentration checks.

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
import k_alpha_research
import signal_edge_lab


CST = timezone(timedelta(hours=8))
DEFAULT_INTERVALS = ["30m", "1h", "4h"]
HORIZONS = [1, 3, 6, 12, 24]
MIN_FEATURE_IDX = 240
RUNTIME_JSON = ROOT / "runtime" / "l1_regime_specific_validation_latest.json"
REPORT_HTML = ROOT / "reports" / "l1_regime_specific_validation_latest.html"


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


def hypothesis_registry() -> list[dict[str, Any]]:
    return [
        {
            "name": "L1_high_vol_negative_jump_bounce_long",
            "side": "long",
            "family": "extreme_volatility_reversal",
            "provenance": "K2 forward edge and full-strategy near miss; pre-registered before this L1 run.",
            "rule": "high_vol_chop_v2; ret1 <= -max(0.9%, 1.6*sigma96); volume percentile >= 60.",
        },
        {
            "name": "L1_high_vol_positive_jump_fade_short",
            "side": "short",
            "family": "extreme_volatility_reversal",
            "provenance": "Symmetric control for K2 negative-jump bounce, registered to avoid one-sided fitting.",
            "rule": "high_vol_chop_v2; ret1 >= max(0.9%, 1.6*sigma96); volume percentile >= 60.",
        },
        {
            "name": "L1_range_chop_pump_fade_short",
            "side": "short",
            "family": "range_chop_failure",
            "provenance": "J3/range-chop clue plus X7 range overbought mean-reversion clue.",
            "rule": "range_chop_v2; ret3 >= 0.9%; ret20 < 4%; volume percentile >= 70.",
        },
        {
            "name": "L1_range_chop_dump_bounce_long",
            "side": "long",
            "family": "range_chop_failure",
            "provenance": "Symmetric range-chop failure control.",
            "rule": "range_chop_v2; ret3 <= -0.9%; ret20 > -4%; volume percentile >= 70.",
        },
        {
            "name": "L1_compression_trend_breakout_long",
            "side": "long",
            "family": "compression_breakout",
            "provenance": "Indicator-factory squeeze/Donchian/Ichimoku singularity clue.",
            "rule": "bb width percentile <= 20; atr percentile <= 35; ret3 >= 0.6%; close > ma50; volume percentile >= 60.",
        },
        {
            "name": "L1_compression_trend_breakdown_short",
            "side": "short",
            "family": "compression_breakout",
            "provenance": "Symmetric compression-breakdown control.",
            "rule": "bb width percentile <= 20; atr percentile <= 35; ret3 <= -0.6%; close < ma50; volume percentile >= 60.",
        },
        {
            "name": "L1_breadth_crack_rebound_long",
            "side": "long",
            "family": "breadth_event",
            "provenance": "Context-alpha breadth crack clue, pre-registered as market-wide reversal validation.",
            "rule": "same-time breadth down-share >= 35%; median ret3 <= -0.5%; symbol ret3 <= -0.6%.",
        },
        {
            "name": "L1_breadth_thrust_continue_long",
            "side": "long",
            "family": "breadth_event",
            "provenance": "Context-alpha breadth thrust watchlist clue, pre-registered as market-wide continuation validation.",
            "rule": "same-time breadth up-share >= 35%; median ret3 >= 0.5%; symbol ret3 >= 0.6%.",
        },
    ]


def median(values: list[float]) -> float:
    clean = [value for value in values if math.isfinite(value)]
    return statistics.median(clean) if clean else 0.0


def breadth_by_time(loaded: dict[str, list[dict[str, Any]]], features_by_key: dict[tuple[str, str], dict[str, list[float]]], interval: str) -> dict[int, dict[str, float]]:
    rows: dict[int, list[float]] = defaultdict(list)
    for symbol, bars in loaded.items():
        features = features_by_key.get((symbol, interval))
        if not features:
            continue
        for idx in range(MIN_FEATURE_IDX, len(bars) - max(HORIZONS) - 1):
            open_ms = safe_int(bars[idx].get("open_time_ms"))
            rows[open_ms].append(safe_float(features["ret3"][idx]))
    out: dict[int, dict[str, float]] = {}
    for open_ms, values in rows.items():
        if len(values) < 12:
            continue
        out[open_ms] = {
            "symbol_count": float(len(values)),
            "up_share_pct": sum(1 for value in values if value >= 1.0) / len(values) * 100.0,
            "down_share_pct": sum(1 for value in values if value <= -1.0) / len(values) * 100.0,
            "median_ret3_pct": median(values),
        }
    return out


def base_record(symbol: str, interval: str, bars: list[dict[str, Any]], features: dict[str, list[float]], idx: int, start_ms: int, end_ms: int) -> dict[str, Any]:
    open_ms = safe_int(bars[idx].get("open_time_ms"))
    return {
        "record_id": -1,
        "symbol": symbol,
        "interval": interval,
        "idx": idx,
        "ts": bars[idx].get("ts"),
        "open_time_ms": open_ms,
        "split": context_alpha_lab.split_label(open_ms, start_ms, end_ms),
        "regime": signal_edge_lab.regime_v2(features, idx),
        "ret1_pct": round(safe_float(features["ret1"][idx]), 6),
        "ret3_pct": round(safe_float(features["ret3"][idx]), 6),
        "ret20_pct": round(safe_float(features["ret20"][idx]), 6),
        "ret12_pct": round(safe_float(features["ret12"][idx]), 6),
        "sigma96_pct": round(k_alpha_research.rolling_std(features["ret1"], idx, 96), 6),
        "bb_width_pctile": round(safe_float(features["bb_width_pctile"][idx]), 3),
        "atr_pctile": round(safe_float(features["atr_pctile"][idx]), 3),
        "volume_pctile": round(safe_float(features["volume_pctile"][idx]), 3),
        "close": round(safe_float(features["closes"][idx]), 10),
        "ma50": round(safe_float(features["ma50"][idx]), 10),
    }


def add_baseline_record(record: dict[str, Any], records: list[dict[str, Any]], baseline_index: dict[tuple[Any, ...], list[int]]) -> None:
    record["record_id"] = len(records)
    records.append(record)
    for key in [
        (
            record["symbol"],
            record["interval"],
            k_alpha_research.month_from_ts(record["ts"]),
            record["regime"],
            k_alpha_research.bucket(safe_float(record.get("atr_pctile"))),
            k_alpha_research.bucket(safe_float(record.get("volume_pctile"))),
        ),
        (
            record["symbol"],
            record["interval"],
            k_alpha_research.month_from_ts(record["ts"]),
            record["regime"],
            k_alpha_research.bucket(safe_float(record.get("atr_pctile"))),
            "*",
        ),
        (
            record["symbol"],
            record["interval"],
            k_alpha_research.month_from_ts(record["ts"]),
            record["regime"],
            "*",
            "*",
        ),
    ]:
        baseline_index[key].append(record["record_id"])


def match_events(record: dict[str, Any], breadth: dict[str, float]) -> list[dict[str, Any]]:
    ret1 = safe_float(record.get("ret1_pct"))
    ret3 = safe_float(record.get("ret3_pct"))
    ret20 = safe_float(record.get("ret20_pct"))
    sigma = safe_float(record.get("sigma96_pct"))
    volp = safe_float(record.get("volume_pctile"))
    bbp = safe_float(record.get("bb_width_pctile"))
    atrp = safe_float(record.get("atr_pctile"))
    close = safe_float(record.get("close"))
    ma50 = safe_float(record.get("ma50"))
    regime = str(record.get("regime") or "")
    jump_gate = max(0.90, sigma * 1.60)
    out: list[dict[str, Any]] = []

    def event(name: str, side: str, extra: dict[str, Any] | None = None) -> None:
        payload = dict(record)
        payload["context"] = name
        payload["side"] = side
        payload["breadth"] = extra or breadth
        out.append(payload)

    if regime == "high_vol_chop_v2" and volp >= 60:
        if ret1 <= -jump_gate:
            event("L1_high_vol_negative_jump_bounce_long", "long")
        if ret1 >= jump_gate:
            event("L1_high_vol_positive_jump_fade_short", "short")
    if regime == "range_chop_v2" and volp >= 70:
        if ret3 >= 0.90 and ret20 < 4.0:
            event("L1_range_chop_pump_fade_short", "short")
        if ret3 <= -0.90 and ret20 > -4.0:
            event("L1_range_chop_dump_bounce_long", "long")
    if bbp <= 20 and atrp <= 35 and volp >= 60:
        if ret3 >= 0.60 and close > ma50 > 0:
            event("L1_compression_trend_breakout_long", "long")
        if ret3 <= -0.60 and (close < ma50 or ma50 <= 0):
            event("L1_compression_trend_breakdown_short", "short")
    if breadth:
        if safe_float(breadth.get("down_share_pct")) >= 35.0 and safe_float(breadth.get("median_ret3_pct")) <= -0.50 and ret3 <= -0.60:
            event("L1_breadth_crack_rebound_long", "long")
        if safe_float(breadth.get("up_share_pct")) >= 35.0 and safe_float(breadth.get("median_ret3_pct")) >= 0.50 and ret3 >= 0.60:
            event("L1_breadth_thrust_continue_long", "long")
    return out


def sample_events(events: list[dict[str, Any]], max_per_context_interval: int) -> list[dict[str, Any]]:
    if max_per_context_interval <= 0:
        return events
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        groups[(str(event.get("context") or ""), str(event.get("interval") or ""))].append(event)
    sampled: list[dict[str, Any]] = []
    for key, rows in groups.items():
        if len(rows) <= max_per_context_interval:
            sampled.extend(rows)
            continue
        rnd = random.Random(context_alpha_lab.stable_seed("l1_sample", key[0], key[1], len(rows), max_per_context_interval))
        copied = list(rows)
        rnd.shuffle(copied)
        sampled.extend(copied[:max_per_context_interval])
    sampled.sort(key=lambda item: (str(item.get("interval")), str(item.get("context")), str(item.get("symbol")), safe_int(item.get("idx"))))
    return sampled


def classify_row(row: dict[str, Any], min_count: int) -> tuple[str, list[str]]:
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
    if safe_float(signal.get("win_rate_pct")) < 50.5:
        failed.append("signal_win_rate_below_50p5pct")
    if safe_float(row.get("uplift_avg_pct")) <= 0.08:
        failed.append("avg_uplift_below_0p08")
    if safe_float(row.get("uplift_median_pct")) <= 0.02:
        failed.append("median_uplift_below_0p02")
    if safe_float(row.get("uplift_win_rate_pct")) < 2.0:
        failed.append("win_rate_uplift_below_2pct")
    if safe_float(row.get("tail_delta_p10_pct")) < -0.10:
        failed.append("tail_risk_worse_than_baseline")
    for split in ["train", "validation", "test"]:
        stats = (row.get("split_stats") or {}).get(split) or {}
        if safe_int(stats.get("count")) < max(20, int(min_count * 0.10)):
            failed.append(f"{split}_sample_low")
        if safe_float(stats.get("avg_pct")) <= 0:
            failed.append(f"{split}_avg_not_positive")
        if safe_float(stats.get("median_pct")) <= -0.03:
            failed.append(f"{split}_median_too_weak")
    if safe_float(row.get("max_symbol_share_pct")) > 22.0:
        failed.append("symbol_concentration_high")
    if safe_float(row.get("max_month_share_pct")) > 32.0:
        failed.append("month_concentration_high")
    if not failed:
        return "edge_candidate", []
    if "sample_count_low" in failed or "baseline_coverage_low" in failed:
        return "rejected", sorted(set(failed))
    if safe_float(row.get("uplift_avg_pct")) > 0 and safe_float(row.get("uplift_median_pct")) > 0:
        return "watchlist", sorted(set(failed))
    return "rejected", sorted(set(failed))


def evaluate_events(
    events: list[dict[str, Any]],
    records: list[dict[str, Any]],
    baseline_index: dict[tuple[Any, ...], list[int]],
    features_by_key: dict[tuple[str, str], dict[str, list[float]]],
    controls: int,
) -> list[dict[str, Any]]:
    stores: dict[tuple[str, str, int], dict[str, Any]] = defaultdict(
        lambda: {"signal": [], "baseline": [], "splits": defaultdict(list), "symbols": Counter(), "months": Counter(), "regimes": Counter()}
    )
    for event_item in events:
        baselines = k_alpha_research.choose_baselines(baseline_index, records, event_item, controls)
        if not baselines:
            continue
        features = features_by_key.get((event_item["symbol"], event_item["interval"]))
        if not features:
            continue
        for horizon in HORIZONS:
            ret = k_alpha_research.forward_ret(features, safe_int(event_item.get("idx")), horizon, str(event_item.get("side")))
            if ret is None:
                continue
            base_rets = []
            for rec_id in baselines:
                rec = records[rec_id]
                base_features = features_by_key.get((rec["symbol"], rec["interval"]))
                if not base_features:
                    continue
                base = k_alpha_research.forward_ret(base_features, safe_int(rec.get("idx")), horizon, str(event_item.get("side")))
                if base is not None:
                    base_rets.append(base)
            if not base_rets:
                continue
            key = (event_item["context"], event_item["interval"], horizon)
            store = stores[key]
            store["signal"].append(ret)
            store["baseline"].extend(base_rets)
            store["splits"][event_item["split"]].append(ret)
            store["symbols"][event_item["symbol"]] += 1
            store["months"][k_alpha_research.month_from_ts(event_item["ts"])] += 1
            store["regimes"][event_item["regime"]] += 1
    rows = []
    for (context, interval, horizon), store in stores.items():
        signal = k_alpha_research.summarize(store["signal"])
        baseline = k_alpha_research.summarize(store["baseline"])
        count = safe_int(signal.get("count"))
        row = {
            "context": context,
            "interval": interval,
            "horizon_bars": horizon,
            "signal_stats": signal,
            "baseline_stats": baseline,
            "uplift_avg_pct": round(signal["avg_pct"] - baseline["avg_pct"], 6),
            "uplift_median_pct": round(signal["median_pct"] - baseline["median_pct"], 6),
            "uplift_win_rate_pct": round(signal["win_rate_pct"] - baseline["win_rate_pct"], 6),
            "tail_delta_p10_pct": round(signal["p10_pct"] - baseline["p10_pct"], 6),
            "split_stats": {name: k_alpha_research.summarize(values) for name, values in store["splits"].items()},
            "max_symbol_share_pct": round(max(store["symbols"].values()) / count * 100.0, 6) if count else 0.0,
            "max_month_share_pct": round(max(store["months"].values()) / count * 100.0, 6) if count else 0.0,
            "top_symbols": [{"symbol": key, "count": value} for key, value in store["symbols"].most_common(8)],
            "top_months": [{"month": key, "count": value} for key, value in store["months"].most_common(8)],
            "regime_counts": dict(store["regimes"]),
        }
        min_count = 120 if interval == "4h" else 240 if interval == "1h" else 360
        decision, reasons = classify_row(row, min_count)
        row["decision"] = decision
        row["failed_gates"] = reasons
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


def build_payload(root: Path, days: int, intervals: list[str], controls: int, max_events_per_context_interval: int) -> dict[str, Any]:
    end = datetime.now(CST)
    start = end - timedelta(days=max(30, days))
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    symbols = shared.universe_symbols(root, None)
    all_events: list[dict[str, Any]] = []
    all_records: list[dict[str, Any]] = []
    all_results: list[dict[str, Any]] = []
    coverage = []
    raw_counts: Counter[str] = Counter()
    for interval in intervals:
        loaded = shared.load_interval_bars(root=root, symbols=symbols, interval=interval, start=start, end=end)
        coverage.extend(shared.coverage_rows({interval: loaded}))
        records: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        baseline_index: dict[tuple[Any, ...], list[int]] = defaultdict(list)
        features_by_key: dict[tuple[str, str], dict[str, list[float]]] = {}
        for symbol, bars in loaded.items():
            if len(bars) < MIN_FEATURE_IDX + max(HORIZONS) + 2:
                continue
            features_by_key[(symbol, interval)] = k_alpha_research.build_features(bars)
        breadth = breadth_by_time(loaded, features_by_key, interval)
        for symbol, bars in loaded.items():
            features = features_by_key.get((symbol, interval))
            if not features:
                continue
            for idx in range(MIN_FEATURE_IDX, len(bars) - max(HORIZONS) - 1):
                record = base_record(symbol, interval, bars, features, idx, start_ms, end_ms)
                add_baseline_record(record, records, baseline_index)
                current_breadth = breadth.get(safe_int(record.get("open_time_ms")), {})
                matched = match_events(record, current_breadth)
                for event_item in matched:
                    raw_counts[f"{event_item['context']}|{interval}"] += 1
                events.extend(matched)
        sampled = sample_events(events, max_events_per_context_interval)
        results = evaluate_events(sampled, records, baseline_index, features_by_key, controls)
        for row in results:
            row["raw_events_before_sampling"] = raw_counts.get(f"{row['context']}|{row['interval']}", 0)
            row["max_events_per_context_interval"] = max_events_per_context_interval
        all_events.extend(sampled)
        all_records.extend(records)
        all_results.extend(results)
    all_results.sort(
        key=lambda item: (
            item["decision"] != "edge_candidate",
            item["decision"] != "watchlist",
            -safe_float(item.get("uplift_avg_pct")),
            -safe_float(item.get("uplift_median_pct")),
        )
    )
    return {
        "generated_at": now_iso(),
        "module": "l1_regime_specific_validation",
        "status": "completed",
        "days": days,
        "intervals": intervals,
        "symbols": symbols,
        "hypotheses": hypothesis_registry(),
        "event_count_after_sampling": len(all_events),
        "record_count": len(all_records),
        "decision_counts": dict(Counter(row["decision"] for row in all_results)),
        "edge_candidates": [row for row in all_results if row["decision"] == "edge_candidate"],
        "watchlist": [row for row in all_results if row["decision"] == "watchlist"],
        "results": all_results,
        "coverage": coverage,
        "safety": {
            "local_only": True,
            "binance_requests_enabled": False,
            "cloud_compute_enabled": False,
            "live_scanner_mutation": False,
            "paper_or_real_orders": False,
            "automatic_tuning_allowed": False,
            "automatic_rollback_allowed": False,
            "automatic_upgrade_allowed": False,
            "promotion_allowed": False,
        },
        "next": "Edge candidates, if any, require a separate full strategy reconstruction before paper/live review.",
        "paths": {"json": str(RUNTIME_JSON), "html": str(REPORT_HTML)},
    }


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
        ("context", "假设"),
        ("interval", "周期"),
        ("horizon_bars", "未来K"),
        ("signal_stats.count", "样本"),
        ("signal_stats.avg_pct", "信号均值%"),
        ("baseline_stats.avg_pct", "基线均值%"),
        ("uplift_avg_pct", "均值提升%"),
        ("uplift_median_pct", "中位提升%"),
        ("uplift_win_rate_pct", "胜率提升%"),
        ("tail_delta_p10_pct", "P10差"),
        ("split_stats.validation.avg_pct", "验证均值%"),
        ("split_stats.test.avg_pct", "测试均值%"),
        ("max_symbol_share_pct", "最大币占比%"),
        ("max_month_share_pct", "最大月占比%"),
        ("failed_gates", "未过门"),
    ]
    hyp_cols = [("name", "假设"), ("side", "方向"), ("family", "家族"), ("rule", "预注册规则")]
    decision_counts = payload.get("decision_counts") or {}
    html = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>L1 Regime-Specific Validation</title>
<style>
body{{margin:0;background:#0b1118;color:#d6e2ee;font-family:Segoe UI,Arial,sans-serif}}
main{{padding:24px;max-width:1500px;margin:auto}} h1{{margin:0 0 8px}} h2{{margin:0 0 12px}}
.meta{{color:#91a2b1;margin-bottom:18px}} .grid{{display:grid;grid-template-columns:1fr;gap:14px}}
.panel{{background:#111b26;border:1px solid #263545;border-radius:8px;padding:16px;overflow:auto}}
table{{width:100%;border-collapse:collapse;font-size:13px}} th,td{{border-bottom:1px solid #263545;padding:8px;text-align:right;white-space:nowrap}}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){{text-align:left}} th{{color:#91a2b1}}
.good{{color:#22c55e}} .bad{{color:#ef4444}} code{{color:#9ddcff}}
</style></head><body><main>
<h1>L1 Regime-Specific Validation</h1>
<div class="meta">Generated <code>{escape(payload['generated_at'])}</code> / Local only / No Binance / No live mutation</div>
<div class="panel">
<p>Decision counts: <code>{escape(json.dumps(decision_counts, ensure_ascii=False))}</code></p>
<p>Events after sampling: <code>{payload.get('event_count_after_sampling')}</code> / Records: <code>{payload.get('record_count')}</code></p>
<p>Rule: even an <code>edge_candidate</code> is not paper/live. It only earns a separate full-strategy reconstruction.</p>
</div>
<section class="grid">
<div class="panel"><h2>预注册假设</h2>{table(payload.get('hypotheses') or [], hyp_cols, 20)}</div>
<div class="panel"><h2>Edge Candidates</h2>{table(payload.get('edge_candidates') or [], cols, 60)}</div>
<div class="panel"><h2>Watchlist</h2>{table(payload.get('watchlist') or [], cols, 120)}</div>
<div class="panel"><h2>All Results</h2>{table(payload.get('results') or [], cols, 200)}</div>
</section>
<p>JSON: <code>{escape(payload['paths']['json'])}</code></p>
</main></body></html>"""
    return html


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run L1 pre-registered regime-specific validation.")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--intervals", default=",".join(DEFAULT_INTERVALS))
    parser.add_argument("--controls", type=int, default=5)
    parser.add_argument("--max-events-per-context-interval", type=int, default=5000)
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
        "decision_counts": payload.get("decision_counts"),
        "edge_candidates": len(payload.get("edge_candidates") or []),
        "watchlist": len(payload.get("watchlist") or []),
        "event_count_after_sampling": payload.get("event_count_after_sampling"),
        "html": str(REPORT_HTML),
        "json": str(RUNTIME_JSON),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
