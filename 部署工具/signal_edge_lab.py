"""Local signal edge lab.

P1-P4 local research runner:

- P1: signal forward-return edge study;
- P2: percentile-based regime edge matrix;
- P3: no-trade filter discovery;
- P4: J1/J2/J3 v2 candidate screen from signal edge, not fitted PnL.

This is local research only. It reads ``research_store/historical_klines`` and
never mutates live config, restarts scanners, calls Binance, submits orders, or
enables automatic tuning/rollback/upgrade.
"""

from __future__ import annotations

import argparse
import json
import math
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

import alpha_discovery_research_report as alpha
import backtest_engine
import d_e_f_historical_research_report as shared
import regime_classifier


CST = timezone(timedelta(hours=8))
DEFAULT_INTERVALS = ["15m", "30m", "1h", "4h"]
HORIZONS = [1, 3, 6, 12]
RUNTIME_JSON = ROOT / "runtime" / "signal_edge_lab_latest.json"
REPORT_HTML = ROOT / "reports" / "signal_edge_lab_latest.html"


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


def percentile_rank(values: list[float], idx: int, length: int) -> float:
    if idx <= 0:
        return 50.0
    start = max(0, idx - length + 1)
    window = [v for v in values[start : idx + 1] if math.isfinite(v)]
    if not window:
        return 50.0
    current = values[idx]
    less_equal = sum(1 for v in window if v <= current)
    return less_equal / len(window) * 100.0


def build_edge_features(bars: list[dict[str, Any]]) -> dict[str, list[float]]:
    base = regime_classifier.build_features(bars)
    closes = base["closes"]
    ret1 = [0.0]
    ret3 = [0.0 for _ in bars]
    ret20 = [0.0 for _ in bars]
    for idx in range(1, len(bars)):
        ret1.append(pct(closes[idx - 1], closes[idx]))
    for idx in range(len(bars)):
        if idx >= 3:
            ret3[idx] = pct(closes[idx - 3], closes[idx])
        if idx >= 20:
            ret20[idx] = pct(closes[idx - 20], closes[idx])
    base["ret1"] = ret1
    base["ret3"] = ret3
    base["ret20"] = ret20
    base["bb_width_pctile"] = [percentile_rank(base["bb_width"], idx, 240) for idx in range(len(bars))]
    base["atr_pctile"] = [percentile_rank(base["atr_pct"], idx, 240) for idx in range(len(bars))]
    base["volume_pctile"] = [percentile_rank(base["vol_ratio"], idx, 240) for idx in range(len(bars))]
    return base


def regime_v2(features: dict[str, list[float]], idx: int) -> str:
    ret3 = features["ret3"][idx]
    ret20 = features["ret20"][idx]
    ma50 = features["ma50"][idx]
    ma200 = features["ma200"][idx]
    close = features["closes"][idx]
    bbp = features["bb_width_pctile"][idx]
    atrp = features["atr_pctile"][idx]
    volp = features["volume_pctile"][idx]
    if atrp >= 88:
        return "high_vol_chop_v2"
    if ret3 >= 0.9 and volp >= 75 and ret20 < 4.0:
        return "volume_impulse_up_v2"
    if ret3 <= -0.9 and volp >= 75 and ret20 > -4.0:
        return "volume_impulse_down_v2"
    if bbp <= 20 and atrp <= 35:
        return "compression_tight_v2"
    if ma50 > ma200 and close > ma50 and ret20 > 1.5:
        return "trend_up_v2"
    if ma50 < ma200 and close < ma50 and ret20 < -1.5:
        return "trend_down_v2"
    return "range_chop_v2"


def forward_returns(bars: list[dict[str, Any]], features: dict[str, list[float]], idx: int, side: str) -> dict[str, float]:
    entry = features["closes"][idx]
    out: dict[str, float] = {}
    for horizon in HORIZONS:
        if idx + horizon >= len(bars) or entry <= 0:
            continue
        out[str(horizon)] = directional_return(entry, features["closes"][idx + horizon], side)
    return out


def signal_rows(symbol: str, interval: str, bars: list[dict[str, Any]], features: dict[str, list[float]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for idx in range(240, len(bars) - max(HORIZONS) - 1):
        close = features["closes"][idx]
        if close <= 0:
            continue
        reg = regime_v2(features, idx)
        ret1 = features["ret1"][idx]
        ret3 = features["ret3"][idx]
        ret20 = features["ret20"][idx]
        volp = features["volume_pctile"][idx]
        bbp = features["bb_width_pctile"][idx]
        atrp = features["atr_pctile"][idx]
        candidates: list[tuple[str, str, dict[str, Any]]] = []

        phase = alpha.phase_for_bar(bars, idx, lookback=8, early_same_max_pct=0.55, middle_same_max_pct=2.5)
        if phase and phase.get("phase") == "early" and safe_float(phase.get("one_bar_pct")) >= 0.45 and volp >= 70:
            candidates.append(("J1_early_impulse_v2", str(phase["side"]), {"phase": phase.get("phase")}))

        if abs(ret3) >= 0.9 and volp >= 75:
            candidates.append(("mover_impulse_v2", "long" if ret3 > 0 else "short", {}))

        if interval == "4h" and idx >= 80:
            prev = bars[idx - 80 : idx]
            high = max(safe_float(row.get("high")) for row in prev)
            low = min(safe_float(row.get("low")) for row in prev)
            if close > high and reg in {"trend_up_v2", "volume_impulse_up_v2"} and 35 <= atrp <= 85:
                candidates.append(("J2_4h_trend_breakout_v2", "long", {}))
            elif close < low and reg in {"trend_down_v2", "volume_impulse_down_v2"} and 35 <= atrp <= 85:
                candidates.append(("J2_4h_trend_breakout_v2", "short", {}))

        if interval in {"1h", "4h"} and idx >= 48:
            prev = bars[idx - 48 : idx]
            high = max(safe_float(row.get("high")) for row in prev)
            low = min(safe_float(row.get("low")) for row in prev)
            prev_reg = regime_v2(features, idx - 1)
            if prev_reg == "compression_tight_v2" and close > high and volp >= 50:
                candidates.append(("J3_compression_breakout_v2", "long", {}))
            elif prev_reg == "compression_tight_v2" and close < low and volp >= 50:
                candidates.append(("J3_compression_breakout_v2", "short", {}))

        if interval == "1h" and idx >= 48:
            prev = bars[idx - 48 : idx]
            high = max(safe_float(row.get("high")) for row in prev)
            low = min(safe_float(row.get("low")) for row in prev)
            trend_ok_long = features["ma50"][idx] > features["ma200"][idx] and close > features["ma50"][idx]
            trend_ok_short = features["ma50"][idx] < features["ma200"][idx] and close < features["ma50"][idx]
            if bbp <= 25 and atrp <= 45 and close > high and trend_ok_long:
                candidates.append(("indicator_singularity_proxy_v2", "long", {}))
            elif bbp <= 25 and atrp <= 45 and close < low and trend_ok_short:
                candidates.append(("indicator_singularity_proxy_v2", "short", {}))

        for name, side, extra in candidates:
            fwd = forward_returns(bars, features, idx, side)
            if not fwd:
                continue
            rows.append(
                {
                    "signal": name,
                    "symbol": symbol,
                    "interval": interval,
                    "ts": bars[idx].get("ts"),
                    "side": side,
                    "regime": reg,
                    "ret1_pct": round(ret1, 6),
                    "ret3_pct": round(ret3, 6),
                    "ret20_pct": round(ret20, 6),
                    "bb_width_pctile": round(bbp, 3),
                    "atr_pctile": round(atrp, 3),
                    "volume_pctile": round(volp, 3),
                    "forward": {k: round(v, 6) for k, v in fwd.items()},
                    **extra,
                }
            )
    return rows


def quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    pos = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * q))))
    return ordered[pos]


def summarize_values(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "win_rate_pct": 0.0, "avg_pct": 0.0, "median_pct": 0.0, "p10_pct": 0.0, "p90_pct": 0.0}
    return {
        "count": len(values),
        "win_rate_pct": round(sum(1 for value in values if value > 0) / len(values) * 100.0, 3),
        "avg_pct": round(sum(values) / len(values), 6),
        "median_pct": round(statistics.median(values), 6),
        "p10_pct": round(quantile(values, 0.10), 6),
        "p90_pct": round(quantile(values, 0.90), 6),
    }


def grouped_edge(rows: list[dict[str, Any]], keys: list[str]) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, ...], dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        key = tuple(row.get(item, "") for item in keys)
        for horizon, value in (row.get("forward") or {}).items():
            grouped[key][str(horizon)].append(safe_float(value))
    out = []
    for key, by_horizon in grouped.items():
        for horizon, values in by_horizon.items():
            item = {keys[idx]: key[idx] for idx in range(len(keys))}
            item["horizon_bars"] = safe_int(horizon)
            item.update(summarize_values(values))
            out.append(item)
    out.sort(key=lambda item: (item.get("signal", ""), item.get("interval", ""), safe_int(item.get("horizon_bars")), -safe_float(item.get("avg_pct"))))
    return out


def no_trade_filters(edge_by_regime: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for row in edge_by_regime:
        if safe_int(row.get("horizon_bars")) not in {3, 6}:
            continue
        count = safe_int(row.get("count"))
        avg = safe_float(row.get("avg_pct"))
        win = safe_float(row.get("win_rate_pct"))
        p10 = safe_float(row.get("p10_pct"))
        if count >= 80 and (avg < 0 or win < 48.0) and p10 < -0.6:
            rows.append(
                {
                    "signal": row.get("signal"),
                    "interval": row.get("interval"),
                    "regime": row.get("regime"),
                    "horizon_bars": row.get("horizon_bars"),
                    "count": count,
                    "avg_pct": avg,
                    "win_rate_pct": win,
                    "p10_pct": p10,
                    "rule": f"skip {row.get('signal')} on {row.get('interval')} when regime={row.get('regime')}",
                }
            )
    rows.sort(key=lambda item: (safe_float(item.get("avg_pct")), safe_float(item.get("win_rate_pct"))))
    return rows


def j_candidates(edge_by_signal: list[dict[str, Any]], no_trade: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blocked = {(row.get("signal"), row.get("interval"), row.get("regime")) for row in no_trade}
    out = []
    for row in edge_by_signal:
        if safe_int(row.get("horizon_bars")) not in {3, 6, 12}:
            continue
        signal = str(row.get("signal") or "")
        interval = str(row.get("interval") or "")
        if safe_int(row.get("count")) < 100:
            continue
        if safe_float(row.get("avg_pct")) <= 0.08:
            continue
        if safe_float(row.get("median_pct")) <= 0:
            continue
        if safe_float(row.get("win_rate_pct")) < 52.0:
            continue
        family = "J1" if signal.startswith("J1") or signal.startswith("mover") else "J2" if signal.startswith("J2") else "J3"
        out.append(
            {
                "family": family,
                "signal": signal,
                "interval": interval,
                "horizon_bars": row.get("horizon_bars"),
                "count": row.get("count"),
                "avg_pct": row.get("avg_pct"),
                "median_pct": row.get("median_pct"),
                "win_rate_pct": row.get("win_rate_pct"),
                "p10_pct": row.get("p10_pct"),
                "status": "candidate_for_full_strategy_rebuild",
                "blocked_regime_rules_available": sum(1 for key in blocked if key[0] == signal and key[1] == interval),
            }
        )
    out.sort(key=lambda item: (safe_float(item.get("avg_pct")), safe_float(item.get("median_pct"))), reverse=True)
    return out


def build_payload(root: Path, days: int, intervals: list[str], max_rows_per_symbol: int) -> dict[str, Any]:
    end = datetime.now(CST)
    start = end - timedelta(days=max(30, days))
    symbols = shared.universe_symbols(root, None)
    all_rows: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    regime_counts: Counter[str] = Counter()
    for interval in intervals:
        loaded = shared.load_interval_bars(root=root, symbols=symbols, interval=interval, start=start, end=end)
        coverage.extend(shared.coverage_rows({interval: loaded}))
        for symbol, bars in loaded.items():
            if len(bars) < 260:
                continue
            features = build_edge_features(bars)
            rows = signal_rows(symbol, interval, bars, features)
            if max_rows_per_symbol > 0:
                rows = rows[-max_rows_per_symbol:]
            all_rows.extend(rows)
            for row in rows:
                regime_counts[row["regime"]] += 1
    edge_by_signal = grouped_edge(all_rows, ["signal", "interval"])
    edge_by_regime = grouped_edge(all_rows, ["signal", "interval", "regime"])
    edge_by_symbol = grouped_edge(all_rows, ["signal", "interval", "symbol"])
    filters = no_trade_filters(edge_by_regime)
    candidates = j_candidates(edge_by_signal, filters)
    return {
        "generated_at": now_iso(),
        "module": "signal_edge_lab",
        "status": "completed",
        "days": days,
        "intervals": intervals,
        "symbols": symbols,
        "sample_count": len(all_rows),
        "signal_counts": dict(Counter(row["signal"] for row in all_rows)),
        "regime_counts": dict(regime_counts),
        "edge_by_signal": edge_by_signal,
        "regime_edge_matrix": edge_by_regime,
        "edge_by_symbol": edge_by_symbol[:1000],
        "no_trade_filters": filters,
        "j_v2_candidates": candidates,
        "coverage": coverage,
        "optimization_backlog": [
            "Calibrate percentile thresholds by symbol/interval instead of global constants.",
            "Add entry-only baseline-vs-random matched samples for each regime.",
            "If J v2 candidate exists, rebuild full strategy with no-trade filters and cost stress.",
        ],
        "safety": {
            "local_only": True,
            "binance_requests_enabled": False,
            "cloud_compute": False,
            "live_config_mutation": False,
            "paper_or_real_orders": False,
            "automatic_tuning_allowed": False,
            "automatic_rollback_allowed": False,
            "automatic_upgrade_allowed": False,
        },
        "paths": {"json": str(RUNTIME_JSON), "html": str(REPORT_HTML)},
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def table(rows: list[dict[str, Any]], columns: list[tuple[str, str]], limit: int = 200) -> str:
    use = rows[:limit]
    head = "".join(f"<th>{escape(label)}</th>" for _key, label in columns)
    body = []
    for row in use:
        cells = []
        for key, _label in columns:
            cells.append(f"<td>{escape(fmt(row.get(key, '')))}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    if not body:
        body.append(f"<tr><td colspan='{len(columns)}'>empty</td></tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def render_html(payload: dict[str, Any]) -> str:
    signal_cols = [
        ("signal", "信号"),
        ("interval", "周期"),
        ("horizon_bars", "未来K"),
        ("count", "样本"),
        ("win_rate_pct", "胜率%"),
        ("avg_pct", "均值%"),
        ("median_pct", "中位%"),
        ("p10_pct", "P10%"),
        ("p90_pct", "P90%"),
    ]
    filter_cols = [
        ("signal", "信号"),
        ("interval", "周期"),
        ("regime", "禁交易状态"),
        ("horizon_bars", "未来K"),
        ("count", "样本"),
        ("avg_pct", "均值%"),
        ("win_rate_pct", "胜率%"),
        ("p10_pct", "P10%"),
        ("rule", "规则"),
    ]
    candidate_cols = [
        ("family", "线"),
        ("signal", "信号"),
        ("interval", "周期"),
        ("horizon_bars", "未来K"),
        ("count", "样本"),
        ("avg_pct", "均值%"),
        ("median_pct", "中位%"),
        ("win_rate_pct", "胜率%"),
        ("status", "状态"),
    ]
    regime_rows = [{"regime": key, "count": value} for key, value in payload["regime_counts"].items()]
    regime_rows.sort(key=lambda item: item["count"], reverse=True)
    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"/><title>Signal Edge Lab</title>
<style>
body{{margin:0;background:#08111a;color:#e9f1f8;font-family:Segoe UI,Arial,sans-serif}} main{{max-width:1320px;margin:0 auto;padding:28px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px}} .panel{{background:#111b26;border:1px solid #263545;border-radius:8px;padding:16px;overflow:auto}}
h1{{margin:0 0 8px;font-size:28px}} h2{{font-size:18px}} p,li{{color:#91a2b1}} code{{color:#f4c95d}}
table{{width:100%;border-collapse:collapse;font-size:13px}} th,td{{border-bottom:1px solid #263545;padding:8px;text-align:right;white-space:nowrap}} th:first-child,td:first-child{{text-align:left}} th{{color:#91a2b1}}
</style></head><body><main>
<h1>P1-P4 本地信号预测力实验室</h1>
<p>Generated: <code>{escape(payload['generated_at'])}</code> / samples: <code>{payload['sample_count']}</code> / Local only / No Binance / No live mutation.</p>
<section class="grid">
<div class="panel"><h2>P1 信号 Forward Edge</h2>{table(payload['edge_by_signal'], signal_cols, 120)}</div>
<div class="panel"><h2>P2 Regime 样本分布</h2>{table(regime_rows, [('regime','状态'),('count','样本')], 40)}</div>
<div class="panel"><h2>P3 No-Trade Filters</h2>{table(payload['no_trade_filters'], filter_cols, 120)}</div>
<div class="panel"><h2>P4 J v2 候选</h2>{table(payload['j_v2_candidates'], candidate_cols, 80)}</div>
</section>
<p>JSON: <code>{escape(payload['paths']['json'])}</code></p>
</main></body></html>"""
    return html


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local signal edge lab.")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--intervals", default=",".join(DEFAULT_INTERVALS))
    parser.add_argument("--max-rows-per-symbol", type=int, default=0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    intervals = [item.strip() for item in str(args.intervals).split(",") if item.strip()]
    payload = build_payload(args.root, args.days, intervals, args.max_rows_per_symbol)
    write_json(RUNTIME_JSON, payload)
    REPORT_HTML.parent.mkdir(parents=True, exist_ok=True)
    REPORT_HTML.write_text(render_html(payload), encoding="utf-8")
    print(json.dumps({
        "status": payload["status"],
        "sample_count": payload["sample_count"],
        "signals": payload["signal_counts"],
        "no_trade_filters": len(payload["no_trade_filters"]),
        "j_v2_candidates": len(payload["j_v2_candidates"]),
        "html": str(REPORT_HTML),
        "json": str(RUNTIME_JSON),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
