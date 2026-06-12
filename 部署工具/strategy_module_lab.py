"""Local strategy module lab for J1/J2/J3 research lines.

Research-only. Tests a small set of pre-registered modules:

- J1 early impulse capture;
- J2 low-frequency 4h trend breakout;
- J3 compression breakout inspired by the indicator-factory singularity.

No live config mutation, no Binance, no cloud compute, no orders, no automatic
tuning/rollback/upgrade.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any, Callable

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
import j_k_l_indicator_research_report as base_ind
import regime_classifier


CST = timezone(timedelta(hours=8))
RUNTIME_JSON = ROOT / "runtime" / "strategy_module_lab_latest.json"
REPORT_HTML = ROOT / "reports" / "strategy_module_lab_latest.html"
DEFAULT_INTERVALS = ["15m", "30m", "1h", "4h"]
MAX_TRADES_PER_SYMBOL = 260


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


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


def interval_minutes(interval: str) -> int:
    return {"15m": 15, "30m": 30, "1h": 60, "4h": 240}.get(interval, 60)


def common_params(max_hold_bars: int = 32) -> dict[str, Any]:
    return {
        "atr_stop_multiplier": 1.8,
        "take_profit_atr": 3.2,
        "trailing_pullback_atr": 1.0,
        "trailing_activation_atr": 0.8,
        "max_hold_bars": max_hold_bars,
        "trade_size_usdt": 100.0,
        "leverage": 2.0,
    }


def j_variants() -> list[dict[str, Any]]:
    return [
        {"family": "J1", "name": "early_impulse_strict", "intervals": ["15m", "30m"], "params": {"impulse_min": 0.55, "prior_same_max": 0.55, "volume_ratio_min": 1.6, **common_params(12)}},
        {"family": "J1", "name": "early_impulse_loose", "intervals": ["15m", "30m"], "params": {"impulse_min": 0.35, "prior_same_max": 0.8, "volume_ratio_min": 1.35, **common_params(16)}},
        {"family": "J1", "name": "early_impulse_no_volume", "intervals": ["15m", "30m"], "params": {"impulse_min": 0.55, "prior_same_max": 0.55, "volume_ratio_min": 0.0, **common_params(12)}},
    ]


def j2_variants() -> list[dict[str, Any]]:
    return [
        {"family": "J2", "name": "4h_donchian_trend_strict", "intervals": ["4h"], "params": {"lookback": 40, "trend_ret_min": 4.0, "volume_ratio_min": 1.05, **common_params(40)}},
        {"family": "J2", "name": "4h_donchian_trend_slow", "intervals": ["4h"], "params": {"lookback": 80, "trend_ret_min": 6.0, "volume_ratio_min": 1.0, **common_params(56)}},
        {"family": "J2", "name": "4h_donchian_no_regime", "intervals": ["4h"], "params": {"lookback": 40, "trend_ret_min": 0.0, "volume_ratio_min": 1.0, "disable_regime": True, **common_params(40)}},
    ]


def j3_variants() -> list[dict[str, Any]]:
    return [
        {"family": "J3", "name": "compression_breakout_strict", "intervals": ["1h", "4h"], "params": {"lookback": 48, "max_bb_width": 4.5, "max_atr_pct": 2.2, "volume_ratio_min": 1.05, **common_params(28)}},
        {"family": "J3", "name": "compression_breakout_loose", "intervals": ["1h", "4h"], "params": {"lookback": 48, "max_bb_width": 6.0, "max_atr_pct": 3.0, "volume_ratio_min": 1.0, **common_params(28)}},
        {"family": "J3", "name": "compression_breakout_no_compression", "intervals": ["1h", "4h"], "params": {"lookback": 48, "max_bb_width": 999.0, "max_atr_pct": 999.0, "volume_ratio_min": 1.0, **common_params(28)}},
    ]


def variants() -> list[dict[str, Any]]:
    return j_variants() + j2_variants() + j3_variants()


def simulate_trade(strategy: str, symbol: str, interval: str, bars: list[dict[str, Any]], idx: int, side: str, params: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any] | None:
    return base_ind.simulate_indicator_trade(
        strategy=strategy,
        adapter="strategy_module_lab",
        symbol=symbol,
        interval=interval,
        bars=bars,
        signal_idx=idx,
        side=side,
        params=params,
        extra=extra,
    )


def run_j1_symbol(symbol: str, interval: str, bars: list[dict[str, Any]], variant: dict[str, Any]) -> list[dict[str, Any]]:
    params = variant["params"]
    out: list[dict[str, Any]] = []
    idx = 32
    while idx < len(bars) - 2 and len(out) < MAX_TRADES_PER_SYMBOL:
        phase = alpha.phase_for_bar(
            bars,
            idx,
            lookback=8,
            early_same_max_pct=safe_float(params.get("prior_same_max"), 0.55),
            middle_same_max_pct=2.5,
        )
        if not phase or phase.get("phase") != "early":
            idx += 1
            continue
        if safe_float(phase.get("one_bar_pct")) < safe_float(params.get("impulse_min"), 0.55):
            idx += 1
            continue
        vol_ratio = shared.volume_ratio(bars, idx)
        if vol_ratio < safe_float(params.get("volume_ratio_min"), 1.5):
            idx += 1
            continue
        trade = simulate_trade("J1/early_impulse", symbol, interval, bars, idx, str(phase["side"]), params, {"phase": phase, "volume_ratio": round(vol_ratio, 6)})
        if trade:
            out.append(trade)
            idx += max(1, safe_int(trade.get("bars_held"), 1))
        idx += 1
    return out


def run_j2_symbol(symbol: str, interval: str, bars: list[dict[str, Any]], variant: dict[str, Any]) -> list[dict[str, Any]]:
    params = variant["params"]
    lookback = safe_int(params.get("lookback"), 40)
    out: list[dict[str, Any]] = []
    regime_features = regime_classifier.build_features(bars)
    idx = max(lookback + 1, 220)
    while idx < len(bars) - 2 and len(out) < MAX_TRADES_PER_SYMBOL:
        close = close_price(bars[idx])
        prev = bars[idx - lookback : idx]
        high = max(safe_float(row.get("high")) for row in prev)
        low = min(safe_float(row.get("low")) for row in prev)
        side = "long" if close > high else "short" if close < low else ""
        if not side:
            idx += 1
            continue
        reg = regime_classifier.classify_bar_with_features(bars, regime_features, idx) or {}
        if not params.get("disable_regime"):
            if side == "long" and reg.get("regime") not in {"trend_up", "volume_impulse_up"}:
                idx += 1
                continue
            if side == "short" and reg.get("regime") not in {"trend_down", "volume_impulse_down"}:
                idx += 1
                continue
        ret20 = abs(safe_float((reg or {}).get("ret20_pct")))
        if ret20 < safe_float(params.get("trend_ret_min"), 4.0):
            idx += 1
            continue
        vol_ratio = shared.volume_ratio(bars, idx)
        if vol_ratio < safe_float(params.get("volume_ratio_min"), 1.0):
            idx += 1
            continue
        trade = simulate_trade("J2/4h_trend_breakout", symbol, interval, bars, idx, side, params, {"regime": reg.get("regime"), "ret20_pct": reg.get("ret20_pct"), "volume_ratio": round(vol_ratio, 6)})
        if trade:
            out.append(trade)
            idx += max(1, safe_int(trade.get("bars_held"), 1))
        idx += 1
    return out


def run_j3_symbol(symbol: str, interval: str, bars: list[dict[str, Any]], variant: dict[str, Any]) -> list[dict[str, Any]]:
    params = variant["params"]
    lookback = safe_int(params.get("lookback"), 48)
    out: list[dict[str, Any]] = []
    regime_features = regime_classifier.build_features(bars)
    idx = max(lookback + 1, 220)
    while idx < len(bars) - 2 and len(out) < MAX_TRADES_PER_SYMBOL:
        close = close_price(bars[idx])
        prev = bars[idx - lookback : idx]
        high = max(safe_float(row.get("high")) for row in prev)
        low = min(safe_float(row.get("low")) for row in prev)
        side = "long" if close > high else "short" if close < low else ""
        if not side:
            idx += 1
            continue
        reg = (
            regime_classifier.classify_bar_with_features(bars, regime_features, idx - 1)
            or regime_classifier.classify_bar_with_features(bars, regime_features, idx)
            or {}
        )
        if safe_float(reg.get("bb_width_pct"), 999.0) > safe_float(params.get("max_bb_width"), 4.5):
            idx += 1
            continue
        if safe_float(reg.get("atr_pct"), 999.0) > safe_float(params.get("max_atr_pct"), 2.2):
            idx += 1
            continue
        vol_ratio = shared.volume_ratio(bars, idx)
        if vol_ratio < safe_float(params.get("volume_ratio_min"), 1.0):
            idx += 1
            continue
        trade = simulate_trade("J3/compression_breakout", symbol, interval, bars, idx, side, params, {"regime": reg.get("regime"), "bb_width_pct": reg.get("bb_width_pct"), "atr_pct": reg.get("atr_pct"), "volume_ratio": round(vol_ratio, 6)})
        if trade:
            out.append(trade)
            idx += max(1, safe_int(trade.get("bars_held"), 1))
        idx += 1
    return out


RUNNERS: dict[str, Callable[[str, str, list[dict[str, Any]], dict[str, Any]], list[dict[str, Any]]]] = {
    "J1": run_j1_symbol,
    "J2": run_j2_symbol,
    "J3": run_j3_symbol,
}


def run_variant_interval(loaded: dict[str, list[dict[str, Any]]], interval: str, variant: dict[str, Any], split: str | None) -> list[dict[str, Any]]:
    runner = RUNNERS[variant["family"]]
    trades: list[dict[str, Any]] = []
    for symbol, bars in loaded.items():
        use_bars = shared.split_sequence(bars, split)
        if len(use_bars) < shared.MIN_BARS:
            continue
        trades.extend(runner(symbol, interval, use_bars, variant))
    return sorted(trades, key=lambda item: str(item.get("exit_ts") or ""))


def anti_fit(row: dict[str, Any]) -> tuple[str, list[str]]:
    checks = shared.anti_fit(row)
    reasons = list(checks.get("anti_fit_reasons") or [])
    full = row.get("full") or {}
    test = row.get("test") or {}
    validation = row.get("validation") or {}
    if safe_float(full.get("net_profit_usdt")) <= 0:
        reasons.append("full_net_not_positive")
    if safe_int(full.get("trades")) < 40:
        reasons.append("full_trade_count_low")
    if safe_float(validation.get("net_profit_usdt")) <= 0:
        reasons.append("validation_net_not_positive")
    if safe_float(test.get("net_profit_usdt")) <= 0:
        reasons.append("test_net_not_positive")
    if safe_float(test.get("profit_factor")) > safe_float(full.get("profit_factor")) * 2.0 and safe_float(full.get("profit_factor")) > 0:
        reasons.append("test_pf_too_much_better_than_full")
    if not reasons:
        return "research_candidate", []
    if safe_float(full.get("net_profit_usdt")) > 0 and safe_float(test.get("net_profit_usdt")) > 0:
        return "near_miss", sorted(set(reasons))
    return "rejected", sorted(set(reasons))


def evaluate_variant(root: Path, loaded_by_interval: dict[str, dict[str, list[dict[str, Any]]]], variant: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for interval in variant["intervals"]:
        if interval not in loaded_by_interval:
            continue
        full_trades = run_variant_interval(loaded_by_interval[interval], interval, variant, None)
        train_trades = run_variant_interval(loaded_by_interval[interval], interval, variant, "train")
        validation_trades = run_variant_interval(loaded_by_interval[interval], interval, variant, "validation")
        test_trades = run_variant_interval(loaded_by_interval[interval], interval, variant, "test")
        full, charts = shared.summarize_trades(full_trades)
        train, _ = shared.summarize_trades(train_trades)
        validation, _ = shared.summarize_trades(validation_trades)
        test, _ = shared.summarize_trades(test_trades)
        row = {
            "family": variant["family"],
            "name": variant["name"],
            "interval": interval,
            "params": variant["params"],
            "full": full,
            "train": train,
            "validation": validation,
            "test": test,
            "charts": {"equity_curve": charts.get("equity_curve", [])[-300:], "monthly_returns": charts.get("monthly_returns", [])},
            "module_breakdown": module_breakdown(full_trades),
        }
        row["robust_score"] = shared.robust_score(row)
        decision, reasons = anti_fit(row)
        row["decision"] = decision
        row["anti_fit_reasons"] = reasons
        rows.append(row)
    return rows


def module_breakdown(trades: list[dict[str, Any]]) -> dict[str, Any]:
    by_side: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_regime: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        by_side[str(trade.get("side") or "unknown")].append(trade)
        by_regime[str(trade.get("regime") or trade.get("phase", {}).get("phase") or "unknown")].append(trade)
    def summarize(groups: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
        out = []
        for key, items in groups.items():
            summary, _ = shared.summarize_trades(items)
            out.append({"key": key, **summary})
        out.sort(key=lambda item: safe_float(item.get("net_profit_usdt")), reverse=True)
        return out
    return {"by_side": summarize(by_side), "by_signal_state": summarize(by_regime)}


def build_payload(root: Path, days: int, intervals: list[str], max_variants: int) -> dict[str, Any]:
    end = datetime.now(CST)
    start = end - timedelta(days=max(30, days))
    symbols = shared.universe_symbols(root, None)
    selected = variants()[:max_variants] if max_variants > 0 else variants()
    needed_intervals = sorted({interval for item in selected for interval in item["intervals"] if interval in intervals})
    loaded_by_interval = {
        interval: shared.load_interval_bars(root=root, symbols=symbols, interval=interval, start=start, end=end)
        for interval in needed_intervals
    }
    rows: list[dict[str, Any]] = []
    for variant in selected:
        rows.extend(evaluate_variant(root, loaded_by_interval, variant))
    rows.sort(key=lambda item: safe_float(item.get("robust_score")), reverse=True)
    decision_counts = Counter(row["decision"] for row in rows)
    best_by_family = {}
    for family in ["J1", "J2", "J3"]:
        candidates = [row for row in rows if row["family"] == family]
        best_by_family[family] = candidates[0] if candidates else {}
    payload = {
        "generated_at": now_iso(),
        "module": "strategy_module_lab",
        "status": "completed",
        "days": days,
        "intervals": needed_intervals,
        "symbols": symbols,
        "variant_count": len(selected),
        "row_count": len(rows),
        "decision_counts": dict(decision_counts),
        "results": rows,
        "best_by_family": best_by_family,
        "coverage": shared.coverage_rows(loaded_by_interval),
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
    return payload


def fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def table(rows: list[dict[str, Any]], columns: list[tuple[str, str]], limit: int | None = None) -> str:
    use = rows[:limit] if limit else rows
    head = "".join(f"<th>{escape(label)}</th>" for _key, label in columns)
    body = []
    for row in use:
        cells = []
        for key, _label in columns:
            value: Any = row
            for part in key.split("."):
                value = value.get(part, {}) if isinstance(value, dict) else ""
            cells.append(f"<td>{escape(fmt(value))}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    if not body:
        body.append(f"<tr><td colspan='{len(columns)}'>empty</td></tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def render_html(payload: dict[str, Any]) -> str:
    rows = payload["results"]
    cols = [
        ("decision", "决策"),
        ("family", "线"),
        ("name", "变体"),
        ("interval", "周期"),
        ("full.net_profit_usdt", "全样本净利"),
        ("validation.net_profit_usdt", "验证净利"),
        ("test.net_profit_usdt", "测试净利"),
        ("full.profit_factor", "PF"),
        ("full.max_drawdown_pct", "回撤%"),
        ("full.trades", "交易"),
        ("robust_score", "稳健分"),
    ]
    decision_rows = [{"decision": k, "count": v} for k, v in payload["decision_counts"].items()]
    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"/><title>Strategy Module Lab</title>
<style>
body{{margin:0;background:#08111a;color:#e9f1f8;font-family:Segoe UI,Arial,sans-serif}} main{{max-width:1320px;margin:0 auto;padding:28px}}
.grid{{display:grid;grid-template-columns:.7fr 1.3fr;gap:14px}} .panel{{background:#111b26;border:1px solid #263545;border-radius:8px;padding:16px;overflow:auto}}
h1{{margin:0 0 8px;font-size:28px}} h2{{font-size:18px}} p{{color:#91a2b1}} code{{color:#f4c95d}}
table{{width:100%;border-collapse:collapse;font-size:13px}} th,td{{border-bottom:1px solid #263545;padding:8px;text-align:right;white-space:nowrap}} th:first-child,td:first-child{{text-align:left}} th{{color:#91a2b1}}
</style></head><body><main>
<h1>J1/J2/J3 本地策略模块实验</h1>
<p>Generated: <code>{escape(payload['generated_at'])}</code> / Local only / No Binance / No live mutation / No auto upgrade.</p>
<section class="grid">
<div class="panel"><h2>决策分布</h2>{table(decision_rows, [('decision','决策'),('count','数量')])}</div>
<div class="panel"><h2>总榜</h2>{table(rows, cols)}</div>
</section>
<p>JSON: <code>{escape(payload['paths']['json'])}</code></p>
</main></body></html>"""
    return html


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local J1/J2/J3 strategy module lab.")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--intervals", default=",".join(DEFAULT_INTERVALS))
    parser.add_argument("--max-variants", type=int, default=0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    intervals = [item.strip() for item in str(args.intervals).split(",") if item.strip()]
    payload = build_payload(args.root, args.days, intervals, args.max_variants)
    write_json(RUNTIME_JSON, payload)
    REPORT_HTML.parent.mkdir(parents=True, exist_ok=True)
    REPORT_HTML.write_text(render_html(payload), encoding="utf-8")
    print(json.dumps({"status": "completed", "decision_counts": payload["decision_counts"], "best_by_family": {k: v.get("decision") for k, v in payload["best_by_family"].items()}, "html": str(REPORT_HTML), "json": str(RUNTIME_JSON)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
