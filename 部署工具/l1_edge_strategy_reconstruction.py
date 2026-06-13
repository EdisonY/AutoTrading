"""Full strategy reconstruction for L1 edge candidates.

Local-only follow-up to ``l1_regime_specific_validation.py``. It reconstructs
the two strict L1 edge candidates as complete 1h trade strategies with
pre-registered exits, costs, train/validation/test, and breakdowns.

No Binance, no cloud compute, no live scanner/config mutation, no paper/real
orders, and no automatic tuning/rollback/upgrade.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict, deque
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
import j_k_l_indicator_research_report as base_ind
import k_alpha_research
import signal_edge_lab


CST = timezone(timedelta(hours=8))
INTERVAL = "1h"
MIN_FEATURE_IDX = 240
RUNTIME_JSON = ROOT / "runtime" / "l1_edge_strategy_reconstruction_latest.json"
REPORT_HTML = ROOT / "reports" / "l1_edge_strategy_reconstruction_latest.html"


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


def variants() -> list[dict[str, Any]]:
    neg_base = {
        "signal": "L1_high_vol_negative_jump_bounce_long",
        "side": "long",
        "trade_size_usdt": 100.0,
        "leverage": 2.0,
        "ret1_abs_min_pct": 0.90,
        "sigma_mult": 1.60,
        "volume_pctile_min": 60.0,
    }
    range_base = {
        "signal": "L1_range_chop_pump_fade_short",
        "side": "short",
        "trade_size_usdt": 100.0,
        "leverage": 2.0,
        "ret3_min_pct": 0.90,
        "ret20_max_pct": 4.0,
        "volume_pctile_min": 70.0,
    }
    return [
        {"name": "l1_neg_jump_12bar_time", "params": {**neg_base, "max_hold_bars": 12, "atr_stop_multiplier": 3.0, "take_profit_atr": 8.0, "trailing_pullback_atr": 0.0, "trailing_activation_atr": 0.0}},
        {"name": "l1_neg_jump_12bar_balanced", "params": {**neg_base, "max_hold_bars": 12, "atr_stop_multiplier": 2.4, "take_profit_atr": 5.5, "trailing_pullback_atr": 1.6, "trailing_activation_atr": 1.1}},
        {"name": "l1_neg_jump_18bar_slow", "params": {**neg_base, "max_hold_bars": 18, "atr_stop_multiplier": 2.8, "take_profit_atr": 7.0, "trailing_pullback_atr": 1.8, "trailing_activation_atr": 1.2}},
        {"name": "l1_neg_jump_24bar_slow_time", "params": {**neg_base, "max_hold_bars": 24, "atr_stop_multiplier": 3.2, "take_profit_atr": 9.0, "trailing_pullback_atr": 0.0, "trailing_activation_atr": 0.0}},
        {"name": "l1_neg_jump_strict_volume75", "params": {**neg_base, "volume_pctile_min": 75.0, "max_hold_bars": 12, "atr_stop_multiplier": 2.4, "take_profit_atr": 5.5, "trailing_pullback_atr": 1.6, "trailing_activation_atr": 1.1}},
        {"name": "l1_range_pump_3bar_time", "params": {**range_base, "max_hold_bars": 3, "atr_stop_multiplier": 2.0, "take_profit_atr": 3.0, "trailing_pullback_atr": 0.0, "trailing_activation_atr": 0.0}},
        {"name": "l1_range_pump_3bar_tight", "params": {**range_base, "max_hold_bars": 3, "atr_stop_multiplier": 1.2, "take_profit_atr": 2.0, "trailing_pullback_atr": 0.8, "trailing_activation_atr": 0.6}},
        {"name": "l1_range_pump_6bar_time", "params": {**range_base, "max_hold_bars": 6, "atr_stop_multiplier": 2.0, "take_profit_atr": 4.0, "trailing_pullback_atr": 0.0, "trailing_activation_atr": 0.0}},
        {"name": "l1_range_pump_6bar_balanced", "params": {**range_base, "max_hold_bars": 6, "atr_stop_multiplier": 1.6, "take_profit_atr": 3.0, "trailing_pullback_atr": 1.0, "trailing_activation_atr": 0.8}},
        {"name": "l1_range_pump_strict_volume80", "params": {**range_base, "volume_pctile_min": 80.0, "max_hold_bars": 3, "atr_stop_multiplier": 1.6, "take_profit_atr": 3.0, "trailing_pullback_atr": 0.0, "trailing_activation_atr": 0.0}},
    ]


def rolling_std_series(values: list[float], lookback: int) -> list[float]:
    out = [0.0 for _ in values]
    q: deque[float] = deque()
    total = 0.0
    total_sq = 0.0
    for idx, raw in enumerate(values):
        if idx == 0:
            continue
        value = safe_float(raw)
        q.append(value)
        total += value
        total_sq += value * value
        if len(q) > lookback:
            old = q.popleft()
            total -= old
            total_sq -= old * old
        count = len(q)
        if count >= 3:
            mean = total / count
            variance = max(0.0, total_sq / count - mean * mean)
            out[idx] = math.sqrt(variance)
    return out


def enrich_features(features: dict[str, list[Any]]) -> dict[str, list[Any]]:
    features["sigma96"] = rolling_std_series([safe_float(item) for item in features["ret1"]], 96)
    features["regime_v2"] = [signal_edge_lab.regime_v2(features, idx) for idx in range(len(features["ret1"]))]
    return features


def matches_signal(features: dict[str, list[Any]], idx: int, params: dict[str, Any]) -> bool:
    signal = str(params.get("signal") or "")
    regime = str(features["regime_v2"][idx])
    volp = safe_float(features["volume_pctile"][idx])
    if signal == "L1_high_vol_negative_jump_bounce_long":
        ret1 = safe_float(features["ret1"][idx])
        sigma = safe_float(features["sigma96"][idx])
        gate = max(safe_float(params.get("ret1_abs_min_pct"), 0.90), sigma * safe_float(params.get("sigma_mult"), 1.60))
        return regime == "high_vol_chop_v2" and volp >= safe_float(params.get("volume_pctile_min"), 60.0) and ret1 <= -gate
    if signal == "L1_range_chop_pump_fade_short":
        ret3 = safe_float(features["ret3"][idx])
        ret20 = safe_float(features["ret20"][idx])
        return (
            regime == "range_chop_v2"
            and volp >= safe_float(params.get("volume_pctile_min"), 70.0)
            and ret3 >= safe_float(params.get("ret3_min_pct"), 0.90)
            and ret20 < safe_float(params.get("ret20_max_pct"), 4.0)
        )
    return False


PreparedSeries = dict[str, dict[str, Any]]


def split_key(split: str | None) -> str:
    return split or "full"


def split_ranges(total: int) -> dict[str, tuple[int, int]]:
    train_end = max(0, int(total * backtest_engine.TRAIN_RATIO))
    validation_end = max(train_end, int(total * (backtest_engine.TRAIN_RATIO + backtest_engine.VALIDATION_RATIO)))
    return {
        "full": (0, total),
        "train": (0, train_end),
        "validation": (train_end, validation_end),
        "test": (validation_end, total),
    }


def prepare_series(loaded: dict[str, list[dict[str, Any]]]) -> PreparedSeries:
    prepared: PreparedSeries = {}
    for symbol, bars in loaded.items():
        if len(bars) < MIN_FEATURE_IDX + 32:
            continue
        prepared[symbol] = {
            "bars": bars,
            "features": enrich_features(k_alpha_research.build_features(bars)),
            "splits": split_ranges(len(bars)),
        }
    return prepared


def simulate_symbol(
    symbol: str,
    bars: list[dict[str, Any]],
    features: dict[str, list[Any]],
    variant: dict[str, Any],
    start_idx: int,
) -> list[dict[str, Any]]:
    if len(bars) < MIN_FEATURE_IDX + 32:
        return []
    params = variant["params"]
    trades: list[dict[str, Any]] = []
    idx = max(MIN_FEATURE_IDX, start_idx)
    while idx < len(bars) - 4 and len(trades) < 800:
        if not matches_signal(features, idx, params):
            idx += 1
            continue
        trade = base_ind.simulate_indicator_trade(
            strategy=str(params.get("signal")),
            adapter="l1_edge_strategy_reconstruction",
            symbol=symbol,
            interval=INTERVAL,
            bars=bars,
            signal_idx=idx,
            side=str(params.get("side")),
            params=params,
            extra={
                "variant": variant["name"],
                "signal": params.get("signal"),
                "ret1_pct": round(features["ret1"][idx], 6),
                "ret3_pct": round(features["ret3"][idx], 6),
                "ret20_pct": round(features["ret20"][idx], 6),
                "sigma96_pct": round(features["sigma96"][idx], 6),
                "volume_pctile": round(features["volume_pctile"][idx], 3),
                "atr_pctile": round(features["atr_pctile"][idx], 3),
                "bb_width_pctile": round(features["bb_width_pctile"][idx], 3),
                "regime": features["regime_v2"][idx],
            },
        )
        if trade:
            trades.append(trade)
            idx += max(1, safe_int(trade.get("bars_held"), 1))
        idx += 1
    return trades


def run_variant(prepared: PreparedSeries, variant: dict[str, Any], split: str | None) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    key = split_key(split)
    for symbol, series in prepared.items():
        start_idx, end_idx = series["splits"].get(key, (0, 0))
        if end_idx - start_idx < MIN_FEATURE_IDX + 32:
            continue
        bars = series["bars"] if end_idx >= len(series["bars"]) else series["bars"][:end_idx]
        trades.extend(simulate_symbol(symbol, bars, series["features"], variant, start_idx))
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


def month_key(trade: dict[str, Any]) -> str:
    return str(trade.get("exit_ts") or trade.get("entry_ts") or "")[:7] or "unknown"


def anti_fit(row: dict[str, Any]) -> tuple[str, list[str]]:
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


def evaluate_variant(prepared: PreparedSeries, variant: dict[str, Any]) -> dict[str, Any]:
    full_trades = run_variant(prepared, variant, None)
    train_trades = run_variant(prepared, variant, "train")
    validation_trades = run_variant(prepared, variant, "validation")
    test_trades = run_variant(prepared, variant, "test")
    full, charts = shared.summarize_trades(full_trades)
    train, _ = shared.summarize_trades(train_trades)
    validation, _ = shared.summarize_trades(validation_trades)
    test, _ = shared.summarize_trades(test_trades)
    row = {
        "variant": variant["name"],
        "signal": variant["params"].get("signal"),
        "params": variant["params"],
        "full": full,
        "train": train,
        "validation": validation,
        "test": test,
        "cost_stress": summarize_costs(full_trades),
        "breakdowns": {
            "by_symbol": group_summary(full_trades, lambda t: t.get("symbol") or "unknown"),
            "by_month": group_summary(full_trades, month_key),
            "by_regime": group_summary(full_trades, lambda t: t.get("regime") or "unknown"),
            "by_exit_reason": group_summary(full_trades, lambda t: t.get("exit_reason") or "unknown"),
        },
        "charts": {"equity_curve": charts.get("equity_curve", [])[-400:], "monthly_returns": charts.get("monthly_returns", [])},
    }
    row["robust_score"] = shared.robust_score(row)
    decision, reasons = anti_fit(row)
    row["decision"] = decision
    row["anti_fit_reasons"] = reasons
    return row


def build_payload(root: Path, days: int) -> dict[str, Any]:
    end = datetime.now(CST)
    start = end - timedelta(days=max(30, days))
    symbols = shared.universe_symbols(root, None)
    loaded = shared.load_interval_bars(root=root, symbols=symbols, interval=INTERVAL, start=start, end=end)
    prepared = prepare_series(loaded)
    rows = [evaluate_variant(prepared, variant) for variant in variants()]
    rows.sort(key=lambda item: safe_float(item.get("robust_score")), reverse=True)
    return {
        "generated_at": now_iso(),
        "module": "l1_edge_strategy_reconstruction",
        "status": "completed",
        "days": days,
        "interval": INTERVAL,
        "symbols": symbols,
        "decision_counts": dict(Counter(row["decision"] for row in rows)),
        "results": rows,
        "best": rows[0] if rows else {},
        "coverage": shared.coverage_rows({INTERVAL: loaded}),
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
        "next": "No paper/live path unless a row is research_candidate and operator approves a separate review.",
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
        ("variant", "变体"),
        ("signal", "信号"),
        ("robust_score", "稳健分"),
        ("full.net_profit_usdt", "全样本净利"),
        ("full.profit_factor", "PF"),
        ("full.trades", "交易数"),
        ("train.net_profit_usdt", "训练净利"),
        ("validation.net_profit_usdt", "验证净利"),
        ("test.net_profit_usdt", "测试净利"),
        ("test.profit_factor", "测试PF"),
        ("anti_fit_reasons", "未过门"),
    ]
    stress_cols = [("extra_cost_bps", "额外bps"), ("net_profit_usdt", "净利"), ("profit_factor", "PF"), ("trades", "交易数")]
    breakdown_cols = [("key", "分组"), ("net_profit_usdt", "净利"), ("profit_factor", "PF"), ("trades", "交易数"), ("win_rate_pct", "胜率")]
    best = payload.get("best") or {}
    html = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>L1 Edge Strategy Reconstruction</title>
<style>
body{{margin:0;background:#0b1118;color:#d6e2ee;font-family:Segoe UI,Arial,sans-serif}}
main{{padding:24px;max-width:1500px;margin:auto}} h1{{margin:0 0 8px}} h2{{margin:0 0 12px}}
.meta{{color:#91a2b1;margin-bottom:18px}} .grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
.panel{{background:#111b26;border:1px solid #263545;border-radius:8px;padding:16px;overflow:auto}}
table{{width:100%;border-collapse:collapse;font-size:13px}} th,td{{border-bottom:1px solid #263545;padding:8px;text-align:right;white-space:nowrap}}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){{text-align:left}} th{{color:#91a2b1}} code{{color:#9ddcff}}
</style></head><body><main>
<h1>L1 Edge Strategy Reconstruction</h1>
<div class="meta">Generated <code>{escape(payload['generated_at'])}</code> / Local only / No Binance / No live mutation</div>
<p>Decision counts: <code>{escape(json.dumps(payload.get('decision_counts') or {}, ensure_ascii=False))}</code></p>
<p>Best: <code>{escape(str(best.get('variant','')))}</code> / decision <code>{escape(str(best.get('decision','')))}</code> / reasons <code>{escape(', '.join(best.get('anti_fit_reasons') or []))}</code></p>
<section class="grid">
<div class="panel"><h2>变体总榜</h2>{table(payload.get('results') or [], cols, 80)}</div>
<div class="panel"><h2>最佳成本压力</h2>{table(best.get('cost_stress') or [], stress_cols, 20)}</div>
<div class="panel"><h2>最佳分币种</h2>{table((best.get('breakdowns') or {}).get('by_symbol') or [], breakdown_cols, 80)}</div>
<div class="panel"><h2>最佳分月份</h2>{table((best.get('breakdowns') or {}).get('by_month') or [], breakdown_cols, 80)}</div>
<div class="panel"><h2>最佳Regime</h2>{table((best.get('breakdowns') or {}).get('by_regime') or [], breakdown_cols, 40)}</div>
<div class="panel"><h2>最佳退出原因</h2>{table((best.get('breakdowns') or {}).get('by_exit_reason') or [], breakdown_cols, 40)}</div>
</section>
<p>JSON: <code>{escape(payload['paths']['json'])}</code></p>
</main></body></html>"""
    return html


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run L1 edge full strategy reconstruction.")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--days", type=int, default=730)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    payload = build_payload(args.root, args.days)
    write_json(RUNTIME_JSON, payload)
    REPORT_HTML.parent.mkdir(parents=True, exist_ok=True)
    REPORT_HTML.write_text(render_html(payload), encoding="utf-8")
    best = payload.get("best") or {}
    print(json.dumps({
        "status": payload["status"],
        "decision_counts": payload.get("decision_counts"),
        "best_variant": best.get("variant"),
        "best_signal": best.get("signal"),
        "best_decision": best.get("decision"),
        "best_full": best.get("full"),
        "best_train": best.get("train"),
        "best_validation": best.get("validation"),
        "best_test": best.get("test"),
        "reasons": best.get("anti_fit_reasons"),
        "html": str(REPORT_HTML),
        "json": str(RUNTIME_JSON),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
