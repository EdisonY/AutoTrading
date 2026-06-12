"""J3 v2 compression-breakout full strategy research.

Local-only follow-up to ``signal_edge_lab.py``. It turns the weak J3 v2
forward-edge candidate into a full trade simulation with P3 no-trade filters,
cost stress, train/validation/test checks, and contribution breakdowns.
"""

from __future__ import annotations

import argparse
import json
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
import j_k_l_indicator_research_report as base_ind
import signal_edge_lab


CST = timezone(timedelta(hours=8))
INTERVAL = "4h"
SIGNAL = "J3_compression_breakout_v2"
RUNTIME_JSON = ROOT / "runtime" / "j3_v2_strategy_research_latest.json"
REPORT_HTML = ROOT / "reports" / "j3_v2_strategy_research_latest.html"


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


def load_no_trade_rules(root: Path) -> set[str]:
    path = root / "runtime" / "signal_edge_lab_latest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return set()
    rules = set()
    for row in payload.get("no_trade_filters") or []:
        if row.get("signal") != SIGNAL or row.get("interval") != INTERVAL:
            continue
        if safe_int(row.get("horizon_bars")) in {3, 6}:
            rules.add(str(row.get("regime") or ""))
    return {item for item in rules if item}


def variants(no_trade_rules: set[str]) -> list[dict[str, Any]]:
    base = {
        "lookback": 48,
        "trade_size_usdt": 100.0,
        "leverage": 2.0,
        "take_profit_atr": 3.2,
        "trailing_pullback_atr": 1.0,
        "trailing_activation_atr": 0.8,
        "volume_pctile_min": 50.0,
    }
    return [
        {"name": "j3v2_edge_3bar_no_filter", "use_no_trade": False, "params": {**base, "max_hold_bars": 3, "atr_stop_multiplier": 1.8}},
        {"name": "j3v2_edge_3bar_p3_filter", "use_no_trade": True, "params": {**base, "max_hold_bars": 3, "atr_stop_multiplier": 1.8, "no_trade_regimes": sorted(no_trade_rules)}},
        {"name": "j3v2_edge_6bar_p3_filter", "use_no_trade": True, "params": {**base, "max_hold_bars": 6, "atr_stop_multiplier": 1.8, "no_trade_regimes": sorted(no_trade_rules)}},
        {"name": "j3v2_tight_stop_3bar_p3_filter", "use_no_trade": True, "params": {**base, "max_hold_bars": 3, "atr_stop_multiplier": 1.2, "no_trade_regimes": sorted(no_trade_rules)}},
        {"name": "j3v2_wide_stop_3bar_p3_filter", "use_no_trade": True, "params": {**base, "max_hold_bars": 3, "atr_stop_multiplier": 2.4, "no_trade_regimes": sorted(no_trade_rules)}},
    ]


def j3_signal(bars: list[dict[str, Any]], features: dict[str, list[float]], idx: int) -> tuple[str, dict[str, Any]] | None:
    if idx < 240 or idx < 48 or idx >= len(bars) - 14:
        return None
    close = features["closes"][idx]
    if close <= 0:
        return None
    prev = bars[idx - 48 : idx]
    high = max(safe_float(row.get("high")) for row in prev)
    low = min(safe_float(row.get("low")) for row in prev)
    prev_reg = signal_edge_lab.regime_v2(features, idx - 1)
    current_reg = signal_edge_lab.regime_v2(features, idx)
    volume_pctile = features["volume_pctile"][idx]
    if prev_reg != "compression_tight_v2" or volume_pctile < 50.0:
        return None
    if close > high:
        side = "long"
    elif close < low:
        side = "short"
    else:
        return None
    return side, {
        "signal": SIGNAL,
        "regime": current_reg,
        "previous_regime": prev_reg,
        "bb_width_pctile": round(features["bb_width_pctile"][idx], 3),
        "atr_pctile": round(features["atr_pctile"][idx], 3),
        "volume_pctile": round(volume_pctile, 3),
        "breakout_lookback": 48,
    }


def simulate_symbol(symbol: str, bars: list[dict[str, Any]], variant: dict[str, Any]) -> list[dict[str, Any]]:
    if len(bars) < 260:
        return []
    params = variant["params"]
    features = signal_edge_lab.build_edge_features(bars)
    no_trade = set(params.get("no_trade_regimes") or []) if variant.get("use_no_trade") else set()
    out: list[dict[str, Any]] = []
    idx = 240
    while idx < len(bars) - 14 and len(out) < 400:
        signal = j3_signal(bars, features, idx)
        if not signal:
            idx += 1
            continue
        side, extra = signal
        if extra["regime"] in no_trade:
            idx += 1
            continue
        trade = base_ind.simulate_indicator_trade(
            strategy="J3/v2_compression_breakout",
            adapter="j3_v2_strategy_research",
            symbol=symbol,
            interval=INTERVAL,
            bars=bars,
            signal_idx=idx,
            side=side,
            params=params,
            extra={**extra, "variant": variant["name"]},
        )
        if trade:
            out.append(trade)
            idx += max(1, safe_int(trade.get("bars_held"), 1))
        idx += 1
    return out


def run_variant(loaded: dict[str, list[dict[str, Any]]], variant: dict[str, Any], split: str | None) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    for symbol, bars in loaded.items():
        use_bars = shared.split_sequence(bars, split)
        trades.extend(simulate_symbol(symbol, use_bars, variant))
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
    reasons = []
    for split in ["full", "train", "validation", "test"]:
        summary = row.get(split) or {}
        if safe_float(summary.get("net_profit_usdt")) <= 0:
            reasons.append(f"{split}_net_not_positive")
        if safe_float(summary.get("profit_factor")) < 1.10:
            reasons.append(f"{split}_profit_factor_below_1.1")
    if safe_int((row.get("full") or {}).get("trades")) < 80:
        reasons.append("full_trade_count_low")
    cost10 = next((item for item in row.get("cost_stress") or [] if safe_float(item.get("extra_cost_bps")) == 10.0), {})
    if safe_float(cost10.get("net_profit_usdt")) <= 0:
        reasons.append("cost_10bps_net_not_positive")
    if reasons:
        if safe_float((row.get("full") or {}).get("net_profit_usdt")) > 0 and safe_float((row.get("test") or {}).get("net_profit_usdt")) > 0:
            return "near_miss", sorted(set(reasons))
        return "rejected", sorted(set(reasons))
    return "research_candidate", []


def evaluate_variant(loaded: dict[str, list[dict[str, Any]]], variant: dict[str, Any]) -> dict[str, Any]:
    full_trades = run_variant(loaded, variant, None)
    train_trades = run_variant(loaded, variant, "train")
    validation_trades = run_variant(loaded, variant, "validation")
    test_trades = run_variant(loaded, variant, "test")
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
            "by_month": group_summary(full_trades, month_key),
            "by_side": group_summary(full_trades, lambda t: t.get("side") or "unknown"),
            "by_regime": group_summary(full_trades, lambda t: t.get("regime") or "unknown"),
            "by_exit_reason": group_summary(full_trades, lambda t: t.get("exit_reason") or "unknown"),
        },
        "charts": {"equity_curve": charts.get("equity_curve", [])[-300:], "monthly_returns": charts.get("monthly_returns", [])},
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
    rules = load_no_trade_rules(root)
    rows = [evaluate_variant(loaded, variant) for variant in variants(rules)]
    rows.sort(key=lambda item: safe_float(item.get("robust_score")), reverse=True)
    decision_counts = dict(Counter(row["decision"] for row in rows))
    best = rows[0] if rows else {}
    return {
        "generated_at": now_iso(),
        "module": "j3_v2_strategy_research",
        "status": "completed",
        "days": days,
        "interval": INTERVAL,
        "symbols": symbols,
        "no_trade_regimes": sorted(rules),
        "decision_counts": decision_counts,
        "results": rows,
        "best": best,
        "coverage": shared.coverage_rows({INTERVAL: loaded}),
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
            value: Any = row
            for part in key.split("."):
                value = value.get(part, {}) if isinstance(value, dict) else ""
            cells.append(f"<td>{escape(fmt(value))}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    if not body:
        body.append(f"<tr><td colspan='{len(columns)}'>empty</td></tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def render_html(payload: dict[str, Any]) -> str:
    cols = [
        ("decision", "决策"),
        ("variant", "变体"),
        ("full.net_profit_usdt", "全样本净利"),
        ("train.net_profit_usdt", "训练净利"),
        ("validation.net_profit_usdt", "验证净利"),
        ("test.net_profit_usdt", "测试净利"),
        ("full.profit_factor", "PF"),
        ("full.max_drawdown_pct", "回撤%"),
        ("full.trades", "交易"),
        ("robust_score", "稳健分"),
    ]
    stress_cols = [
        ("extra_cost_bps", "额外成本bps"),
        ("net_profit_usdt", "净利"),
        ("profit_factor", "PF"),
        ("max_drawdown_pct", "回撤%"),
        ("win_rate_pct", "胜率%"),
        ("trades", "交易"),
    ]
    breakdown_cols = [
        ("key", "分组"),
        ("net_profit_usdt", "净利"),
        ("profit_factor", "PF"),
        ("win_rate_pct", "胜率%"),
        ("trades", "交易"),
    ]
    best = payload.get("best") or {}
    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"/><title>J3 v2 Strategy Research</title>
<style>
body{{margin:0;background:#08111a;color:#e9f1f8;font-family:Segoe UI,Arial,sans-serif}} main{{max-width:1320px;margin:0 auto;padding:28px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px}} .panel{{background:#111b26;border:1px solid #263545;border-radius:8px;padding:16px;overflow:auto}}
h1{{margin:0 0 8px;font-size:28px}} h2{{font-size:18px}} p,li{{color:#91a2b1}} code{{color:#f4c95d}}
table{{width:100%;border-collapse:collapse;font-size:13px}} th,td{{border-bottom:1px solid #263545;padding:8px;text-align:right;white-space:nowrap}} th:first-child,td:first-child{{text-align:left}} th{{color:#91a2b1}}
</style></head><body><main>
<h1>J3 v2 压缩突破完整策略研究</h1>
<p>Generated: <code>{escape(payload['generated_at'])}</code> / Local only / No Binance / No live mutation.</p>
<p>Best: <code>{escape(str(best.get('variant','')))}</code> / decision <code>{escape(str(best.get('decision','')))}</code> / reasons <code>{escape(', '.join(best.get('anti_fit_reasons') or []))}</code></p>
<section class="grid">
<div class="panel"><h2>变体总榜</h2>{table(payload.get('results') or [], cols, 50)}</div>
<div class="panel"><h2>最佳成本压力</h2>{table(best.get('cost_stress') or [], stress_cols, 20)}</div>
<div class="panel"><h2>最佳分币种</h2>{table((best.get('breakdowns') or {}).get('by_symbol') or [], breakdown_cols, 80)}</div>
<div class="panel"><h2>最佳分月份</h2>{table((best.get('breakdowns') or {}).get('by_month') or [], breakdown_cols, 80)}</div>
<div class="panel"><h2>最佳分Regime</h2>{table((best.get('breakdowns') or {}).get('by_regime') or [], breakdown_cols, 40)}</div>
<div class="panel"><h2>最佳退出原因</h2>{table((best.get('breakdowns') or {}).get('by_exit_reason') or [], breakdown_cols, 40)}</div>
</section>
<p>JSON: <code>{escape(payload['paths']['json'])}</code></p>
</main></body></html>"""
    return html


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local J3 v2 full strategy research.")
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
        "status": "completed",
        "decision_counts": payload.get("decision_counts"),
        "best_variant": best.get("variant"),
        "best_decision": best.get("decision"),
        "best_full": best.get("full"),
        "best_test": best.get("test"),
        "reasons": best.get("anti_fit_reasons"),
        "html": str(REPORT_HTML),
        "json": str(RUNTIME_JSON),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
