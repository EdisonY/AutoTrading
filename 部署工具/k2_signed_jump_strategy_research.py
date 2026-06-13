"""K2 negative signed-jump bounce full strategy research.

Local-only follow-up to ``k_alpha_research.py``. It turns the only K2 edge
candidate, ``K2_negative_signed_jump_bounce_long / 4h / 12bar``, into a full
trade simulation with pre-registered variants, cost stress, train/validation
/test checks, and contribution breakdowns.

No Binance, no cloud compute, no live scanner/config mutation, no paper/real
orders, and no automatic tuning/rollback/upgrade.
"""

from __future__ import annotations

import argparse
import json
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
import j_k_l_indicator_research_report as base_ind
import k_alpha_research
import signal_edge_lab


CST = timezone(timedelta(hours=8))
INTERVAL = "4h"
SIGNAL = "K2_negative_signed_jump_bounce_long"
MIN_FEATURE_IDX = 240
RUNTIME_JSON = ROOT / "runtime" / "k2_signed_jump_strategy_research_latest.json"
REPORT_HTML = ROOT / "reports" / "k2_signed_jump_strategy_research_latest.html"


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def safe_float(value: Any, default: float = 0.0) -> float:
    return backtest_engine.safe_float(value, default)


def safe_int(value: Any, default: int = 0) -> int:
    return backtest_engine.safe_int(value, default)


def pct(a: float, b: float) -> float:
    return (b - a) / a * 100.0 if a > 0 else 0.0


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def variants() -> list[dict[str, Any]]:
    base = {
        "trade_size_usdt": 100.0,
        "leverage": 2.0,
        "ret1_abs_min_pct": 0.90,
        "sigma_mult": 1.60,
        "volume_pctile_min": 60.0,
    }
    return [
        {"name": "k2_neg_jump_12bar_time_exit", "params": {**base, "max_hold_bars": 12, "atr_stop_multiplier": 3.0, "take_profit_atr": 8.0, "trailing_pullback_atr": 0.0, "trailing_activation_atr": 0.0}},
        {"name": "k2_neg_jump_12bar_balanced", "params": {**base, "max_hold_bars": 12, "atr_stop_multiplier": 2.0, "take_profit_atr": 4.5, "trailing_pullback_atr": 1.4, "trailing_activation_atr": 1.0}},
        {"name": "k2_neg_jump_6bar_fast", "params": {**base, "max_hold_bars": 6, "atr_stop_multiplier": 1.4, "take_profit_atr": 3.0, "trailing_pullback_atr": 1.0, "trailing_activation_atr": 0.8}},
        {"name": "k2_neg_jump_18bar_slow", "params": {**base, "max_hold_bars": 18, "atr_stop_multiplier": 2.4, "take_profit_atr": 5.5, "trailing_pullback_atr": 1.6, "trailing_activation_atr": 1.2}},
        {"name": "k2_neg_jump_24bar_slow_time", "params": {**base, "max_hold_bars": 24, "atr_stop_multiplier": 3.2, "take_profit_atr": 9.0, "trailing_pullback_atr": 0.0, "trailing_activation_atr": 0.0}},
        {"name": "k2_neg_jump_strict_volume75", "params": {**base, "volume_pctile_min": 75.0, "max_hold_bars": 12, "atr_stop_multiplier": 2.0, "take_profit_atr": 4.5, "trailing_pullback_atr": 1.4, "trailing_activation_atr": 1.0}},
        {"name": "k2_neg_jump_extreme_ret1p2", "params": {**base, "ret1_abs_min_pct": 1.20, "max_hold_bars": 12, "atr_stop_multiplier": 2.0, "take_profit_atr": 4.5, "trailing_pullback_atr": 1.4, "trailing_activation_atr": 1.0}},
        {"name": "k2_neg_jump_badvol_confirm", "params": {**base, "require_bad_vol_bias": True, "max_hold_bars": 12, "atr_stop_multiplier": 2.0, "take_profit_atr": 4.5, "trailing_pullback_atr": 1.4, "trailing_activation_atr": 1.0}},
    ]


def rolling_std(values: list[float], idx: int, lookback: int) -> float:
    start = max(1, idx - lookback + 1)
    window = values[start : idx + 1]
    return statistics.pstdev(window) if len(window) >= 3 else 0.0


def matches_signal(features: dict[str, list[float]], idx: int, params: dict[str, Any]) -> bool:
    ret1 = features["ret1"][idx]
    sigma = rolling_std(features["ret1"], idx, 96)
    gate = max(safe_float(params.get("ret1_abs_min_pct"), 0.90), sigma * safe_float(params.get("sigma_mult"), 1.60))
    if ret1 > -gate:
        return False
    if features["volume_pctile"][idx] < safe_float(params.get("volume_pctile_min"), 60.0):
        return False
    if params.get("require_bad_vol_bias"):
        if features["neg_vol48"][idx] <= features["pos_vol48"][idx] * 1.15:
            return False
    return True


def simulate_symbol(symbol: str, bars: list[dict[str, Any]], variant: dict[str, Any]) -> list[dict[str, Any]]:
    if len(bars) < MIN_FEATURE_IDX + 24:
        return []
    params = variant["params"]
    features = k_alpha_research.build_features(bars)
    trades: list[dict[str, Any]] = []
    idx = MIN_FEATURE_IDX
    while idx < len(bars) - 4 and len(trades) < 500:
        if not matches_signal(features, idx, params):
            idx += 1
            continue
        trade = base_ind.simulate_indicator_trade(
            strategy="K2/negative_signed_jump_bounce_long",
            adapter="k2_signed_jump_strategy_research",
            symbol=symbol,
            interval=INTERVAL,
            bars=bars,
            signal_idx=idx,
            side="long",
            params=params,
            extra={
                "signal": SIGNAL,
                "variant": variant["name"],
                "ret1_pct": round(features["ret1"][idx], 6),
                "ret12_pct": round(features["ret12"][idx], 6),
                "sigma96_pct": round(rolling_std(features["ret1"], idx, 96), 6),
                "volume_pctile": round(features["volume_pctile"][idx], 3),
                "atr_pctile": round(features["atr_pctile"][idx], 3),
                "pos_vol48": round(features["pos_vol48"][idx], 6),
                "neg_vol48": round(features["neg_vol48"][idx], 6),
                "regime": signal_edge_lab.regime_v2(features, idx),
            },
        )
        if trade:
            trades.append(trade)
            idx += max(1, safe_int(trade.get("bars_held"), 1))
        idx += 1
    return trades


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
    rows = [evaluate_variant(loaded, variant) for variant in variants()]
    rows.sort(key=lambda item: safe_float(item.get("robust_score")), reverse=True)
    return {
        "generated_at": now_iso(),
        "module": "k2_signed_jump_strategy_research",
        "status": "completed",
        "days": days,
        "interval": INTERVAL,
        "signal": SIGNAL,
        "symbols": symbols,
        "decision_counts": dict(Counter(row["decision"] for row in rows)),
        "results": rows,
        "best": rows[0] if rows else {},
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
        body.append("<tr>" + "".join(f"<td>{escape(fmt(nested(row, key)))}</td>" for key, _label in columns) + "</tr>")
    if not body:
        body.append(f"<tr><td colspan='{len(columns)}'>empty</td></tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def render_html(payload: dict[str, Any]) -> str:
    cols = [
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
    stress_cols = [
        ("extra_cost_bps", "额外成本bps"),
        ("net_profit_usdt", "净利"),
        ("profit_factor", "PF"),
        ("max_drawdown_pct", "回撤%"),
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
<html lang="zh-CN"><head><meta charset="utf-8"/><title>K2 Signed Jump Strategy Research</title>
<style>
body{{margin:0;background:#08111a;color:#e9f1f8;font-family:Segoe UI,Arial,sans-serif}} main{{max-width:1440px;margin:0 auto;padding:28px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px}} .panel{{background:#111b26;border:1px solid #263545;border-radius:8px;padding:16px;overflow:auto}}
h1{{margin:0 0 8px;font-size:28px}} h2{{font-size:18px}} p,li{{color:#91a2b1}} code{{color:#f4c95d}}
table{{width:100%;border-collapse:collapse;font-size:13px}} th,td{{border-bottom:1px solid #263545;padding:8px;text-align:right;white-space:nowrap}} th:first-child,td:first-child{{text-align:left}} th{{color:#91a2b1}}
</style></head><body><main>
<h1>K2 负向跳变反弹完整策略复核</h1>
<p>Generated: <code>{escape(payload['generated_at'])}</code> / Signal: <code>{escape(payload['signal'])}</code> / Local only / No Binance / No live mutation.</p>
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
    parser = argparse.ArgumentParser(description="Run K2 signed-jump full strategy research.")
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
