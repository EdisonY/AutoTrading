"""X7 range-reversion full strategy research.

Local-only follow-up to ``context_alpha_lab.py``. It turns the weak
``X7_range_overbought_reversion_short / 30m / 3bar`` context edge into a
full trade simulation with pre-registered variants, cost stress,
train/validation/test checks, and contribution breakdowns.

This is not a live strategy. It never calls Binance, mutates live config,
restarts services, places orders, or enables automatic tuning/rollback/upgrade.
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
import context_alpha_lab
import d_e_f_historical_research_report as shared
import j_k_l_indicator_research_report as base_ind


CST = timezone(timedelta(hours=8))
INTERVAL = "30m"
SIGNAL = "X7_range_overbought_reversion_short"
MIN_FEATURE_IDX = 240
RUNTIME_JSON = ROOT / "runtime" / "x7_range_reversion_strategy_research_latest.json"
REPORT_HTML = ROOT / "reports" / "x7_range_reversion_strategy_research_latest.html"


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
    base = {
        "rank_min": 80.0,
        "ret3_min": 0.8,
        "volume_pctile_max": 75.0,
        "trade_size_usdt": 100.0,
        "leverage": 2.0,
    }
    return [
        {
            "name": "x7_exact_3bar_balanced",
            "params": {
                **base,
                "max_hold_bars": 3,
                "atr_stop_multiplier": 1.2,
                "take_profit_atr": 0.8,
                "trailing_pullback_atr": 0.6,
                "trailing_activation_atr": 0.4,
            },
        },
        {
            "name": "x7_exact_3bar_time_exit",
            "params": {
                **base,
                "max_hold_bars": 3,
                "atr_stop_multiplier": 2.5,
                "take_profit_atr": 5.0,
                "trailing_pullback_atr": 0.0,
                "trailing_activation_atr": 0.0,
            },
        },
        {
            "name": "x7_tight_3bar_fast_revert",
            "params": {
                **base,
                "max_hold_bars": 3,
                "atr_stop_multiplier": 0.8,
                "take_profit_atr": 0.6,
                "trailing_pullback_atr": 0.4,
                "trailing_activation_atr": 0.3,
            },
        },
        {
            "name": "x7_exact_6bar_loose",
            "params": {
                **base,
                "max_hold_bars": 6,
                "atr_stop_multiplier": 1.2,
                "take_profit_atr": 1.2,
                "trailing_pullback_atr": 0.7,
                "trailing_activation_atr": 0.5,
            },
        },
        {
            "name": "x7_extreme_rank90_ret1p2",
            "params": {
                **base,
                "rank_min": 90.0,
                "ret3_min": 1.2,
                "volume_pctile_max": 70.0,
                "max_hold_bars": 3,
                "atr_stop_multiplier": 1.2,
                "take_profit_atr": 0.8,
                "trailing_pullback_atr": 0.6,
                "trailing_activation_atr": 0.4,
            },
        },
        {
            "name": "x7_market_not_broad_bull",
            "params": {
                **base,
                "breadth_above_ma50_max": 60.0,
                "market_median_ret12_max": 0.5,
                "max_hold_bars": 3,
                "atr_stop_multiplier": 1.2,
                "take_profit_atr": 0.8,
                "trailing_pullback_atr": 0.6,
                "trailing_activation_atr": 0.4,
            },
        },
        {
            "name": "x7_quiet_range_only",
            "params": {
                **base,
                "atr_pctile_max": 60.0,
                "bb_width_pctile_max": 65.0,
                "max_hold_bars": 3,
                "atr_stop_multiplier": 1.2,
                "take_profit_atr": 0.8,
                "trailing_pullback_atr": 0.6,
                "trailing_activation_atr": 0.4,
            },
        },
        {
            "name": "x7_broad_control_ret0p6",
            "params": {
                **base,
                "ret3_min": 0.6,
                "volume_pctile_max": 85.0,
                "max_hold_bars": 3,
                "atr_stop_multiplier": 1.2,
                "take_profit_atr": 0.8,
                "trailing_pullback_atr": 0.6,
                "trailing_activation_atr": 0.4,
            },
        },
    ]


def month_key(trade: dict[str, Any]) -> str:
    return str(trade.get("exit_ts") or trade.get("entry_ts") or "")[:7] or "unknown"


def build_context_records(loaded: dict[str, list[dict[str, Any]]]) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], dict[str, dict[str, list[float]]]]:
    features_by_symbol: dict[str, dict[str, list[float]]] = {}
    bars_by_symbol: dict[str, list[dict[str, Any]]] = {}
    by_time: dict[int, dict[str, int]] = defaultdict(dict)
    for symbol, bars in loaded.items():
        if len(bars) < MIN_FEATURE_IDX + 12:
            continue
        features = context_alpha_lab.build_features(bars)
        features_by_symbol[symbol] = features
        bars_by_symbol[symbol] = bars
        for idx in range(MIN_FEATURE_IDX, len(bars) - 12):
            by_time[safe_int(bars[idx].get("open_time_ms"))][symbol] = idx

    records: list[dict[str, Any]] = []
    for open_ms in sorted(by_time):
        here = by_time[open_ms]
        if len(here) < 12:
            continue
        btc_mom = 0.0
        eth_mom = 0.0
        if "BTCUSDT" in here:
            btc_mom = features_by_symbol["BTCUSDT"]["ret12"][here["BTCUSDT"]]
        if "ETHUSDT" in here:
            eth_mom = features_by_symbol["ETHUSDT"]["ret12"][here["ETHUSDT"]]
        score_rows: list[dict[str, Any]] = []
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
        ordered = sorted(score_rows, key=lambda row: row["mom12"])
        rank_pct = {
            row["symbol"]: (rank / max(1, len(ordered) - 1) * 100.0)
            for rank, row in enumerate(ordered)
        }
        moms = [row["mom12"] for row in score_rows]
        breadth_above = sum(1 for row in score_rows if row["above_ma50"]) / len(score_rows) * 100.0
        breadth_ret_pos = sum(1 for row in score_rows if row["mom12"] > 0) / len(score_rows) * 100.0
        median_mom = statistics.median(moms) if moms else 0.0
        for row in score_rows:
            symbol = row["symbol"]
            idx = row["idx"]
            features = features_by_symbol[symbol]
            records.append(
                {
                    "symbol": symbol,
                    "idx": idx,
                    "ts": bars_by_symbol[symbol][idx].get("ts"),
                    "open_time_ms": open_ms,
                    "regime": context_alpha_lab.signal_edge_lab.regime_v2(features, idx),
                    "rank_pct": round(rank_pct[symbol], 3),
                    "ret3_pct": round(features["ret3"][idx], 6),
                    "ret12_pct": round(row["mom12"], 6),
                    "volume_pctile": round(features["volume_pctile"][idx], 3),
                    "atr_pctile": round(features["atr_pctile"][idx], 3),
                    "bb_width_pctile": round(features["bb_width_pctile"][idx], 3),
                    "breadth_above_ma50_pct": round(breadth_above, 3),
                    "breadth_ret12_pos_pct": round(breadth_ret_pos, 3),
                    "market_median_ret12_pct": round(median_mom, 6),
                    "rel_btc_12_pct": round(row["mom12"] - btc_mom, 6),
                    "rel_eth_12_pct": round(row["mom12"] - eth_mom, 6),
                }
            )
    records.sort(key=lambda item: (str(item.get("symbol")), safe_int(item.get("idx"))))
    return records, bars_by_symbol, features_by_symbol


def matches_x7(record: dict[str, Any], params: dict[str, Any]) -> bool:
    if record.get("regime") != "range_chop_v2":
        return False
    checks = [
        safe_float(record.get("rank_pct")) >= safe_float(params.get("rank_min"), 80.0),
        safe_float(record.get("ret3_pct")) >= safe_float(params.get("ret3_min"), 0.8),
        safe_float(record.get("volume_pctile")) < safe_float(params.get("volume_pctile_max"), 75.0),
        safe_float(record.get("breadth_above_ma50_pct")) <= safe_float(params.get("breadth_above_ma50_max"), 100.0),
        safe_float(record.get("market_median_ret12_pct")) <= safe_float(params.get("market_median_ret12_max"), 100.0),
        safe_float(record.get("atr_pctile")) <= safe_float(params.get("atr_pctile_max"), 100.0),
        safe_float(record.get("bb_width_pctile")) <= safe_float(params.get("bb_width_pctile_max"), 100.0),
    ]
    return all(checks)


def simulate_variant_from_context(
    records: list[dict[str, Any]],
    bars_by_symbol: dict[str, list[dict[str, Any]]],
    variant: dict[str, Any],
) -> list[dict[str, Any]]:
    params = variant["params"]
    trades: list[dict[str, Any]] = []
    next_allowed_idx: dict[str, int] = defaultdict(int)
    signal_counts: Counter[str] = Counter()
    for record in records:
        symbol = str(record.get("symbol") or "")
        idx = safe_int(record.get("idx"))
        if idx < next_allowed_idx[symbol]:
            continue
        if not matches_x7(record, params):
            continue
        signal_counts[symbol] += 1
        trade = base_ind.simulate_indicator_trade(
            strategy="X7/range_overbought_reversion_short",
            adapter="x7_range_reversion_strategy_research",
            symbol=symbol,
            interval=INTERVAL,
            bars=bars_by_symbol[symbol],
            signal_idx=idx,
            side="short",
            params=params,
            extra={
                "signal": SIGNAL,
                "variant": variant["name"],
                "regime": record.get("regime"),
                "rank_pct": record.get("rank_pct"),
                "ret3_pct": record.get("ret3_pct"),
                "volume_pctile": record.get("volume_pctile"),
                "atr_pctile": record.get("atr_pctile"),
                "bb_width_pctile": record.get("bb_width_pctile"),
                "breadth_above_ma50_pct": record.get("breadth_above_ma50_pct"),
                "market_median_ret12_pct": record.get("market_median_ret12_pct"),
                "rel_btc_12_pct": record.get("rel_btc_12_pct"),
                "rel_eth_12_pct": record.get("rel_eth_12_pct"),
            },
        )
        if trade:
            trades.append(trade)
            next_allowed_idx[symbol] = idx + max(1, safe_int(trade.get("bars_held"), 1))
    return sorted(trades, key=lambda item: str(item.get("exit_ts") or ""))


def prepare_contexts(loaded: dict[str, list[dict[str, Any]]]) -> dict[str, tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]]:
    contexts: dict[str, tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]] = {}
    for split in ["full", "train", "validation", "test"]:
        use_loaded = loaded if split == "full" else {symbol: shared.split_sequence(bars, split) for symbol, bars in loaded.items()}
        records, bars_by_symbol, _features_by_symbol = build_context_records(use_loaded)
        contexts[split] = (records, bars_by_symbol)
    return contexts


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


def max_share(trades: list[dict[str, Any]], key_fn) -> float:
    if not trades:
        return 0.0
    counts = Counter(str(key_fn(trade)) for trade in trades)
    return round(max(counts.values()) / len(trades) * 100.0, 6) if counts else 0.0


def positive_month_share(month_rows: list[dict[str, Any]]) -> float:
    if not month_rows:
        return 0.0
    positive = sum(1 for row in month_rows if safe_float(row.get("net_profit_usdt")) > 0)
    return round(positive / len(month_rows) * 100.0, 6)


def anti_fit(row: dict[str, Any]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    full = row.get("full") or {}
    if safe_int(full.get("trades")) < 200:
        reasons.append("full_trade_count_below_200")
    if safe_float(full.get("net_profit_usdt")) <= 0:
        reasons.append("full_net_not_positive")
    if safe_float(full.get("profit_factor")) < 1.10:
        reasons.append("full_profit_factor_below_1.1")
    for split in ["train", "validation", "test"]:
        summary = row.get(split) or {}
        if safe_int(summary.get("trades")) < 40:
            reasons.append(f"{split}_trade_count_low")
        if safe_float(summary.get("net_profit_usdt")) <= 0:
            reasons.append(f"{split}_net_not_positive")
        if safe_float(summary.get("profit_factor")) < 1.08:
            reasons.append(f"{split}_profit_factor_below_1.08")
    cost10 = next((item for item in row.get("cost_stress") or [] if safe_float(item.get("extra_cost_bps")) == 10.0), {})
    if safe_float(cost10.get("net_profit_usdt")) <= 0:
        reasons.append("cost_10bps_net_not_positive")
    if safe_float(row.get("max_symbol_share_pct")) > 22.0:
        reasons.append("symbol_concentration_high")
    if safe_float(row.get("max_month_share_pct")) > 18.0:
        reasons.append("month_concentration_high")
    if safe_float(row.get("positive_month_share_pct")) < 52.0:
        reasons.append("positive_month_share_low")
    if reasons:
        if safe_float(full.get("net_profit_usdt")) > 0 and safe_float((row.get("test") or {}).get("net_profit_usdt")) > 0:
            return "near_miss", sorted(set(reasons))
        return "rejected", sorted(set(reasons))
    return "research_candidate", []


def evaluate_variant(contexts: dict[str, tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]], variant: dict[str, Any]) -> dict[str, Any]:
    full_trades = simulate_variant_from_context(*contexts["full"], variant)
    train_trades = simulate_variant_from_context(*contexts["train"], variant)
    validation_trades = simulate_variant_from_context(*contexts["validation"], variant)
    test_trades = simulate_variant_from_context(*contexts["test"], variant)
    full, charts = shared.summarize_trades(full_trades)
    train, _ = shared.summarize_trades(train_trades)
    validation, _ = shared.summarize_trades(validation_trades)
    test, _ = shared.summarize_trades(test_trades)
    by_month = group_summary(full_trades, month_key)
    row = {
        "variant": variant["name"],
        "params": variant["params"],
        "full": full,
        "train": train,
        "validation": validation,
        "test": test,
        "cost_stress": summarize_costs(full_trades),
        "max_symbol_share_pct": max_share(full_trades, lambda t: t.get("symbol") or "unknown"),
        "max_month_share_pct": max_share(full_trades, month_key),
        "positive_month_share_pct": positive_month_share(by_month),
        "breakdowns": {
            "by_symbol": group_summary(full_trades, lambda t: t.get("symbol") or "unknown"),
            "by_month": by_month,
            "by_exit_reason": group_summary(full_trades, lambda t: t.get("exit_reason") or "unknown"),
            "by_rank_bucket": group_summary(full_trades, lambda t: int(safe_float(t.get("rank_pct")) // 10 * 10)),
            "by_ret3_bucket": group_summary(full_trades, lambda t: f"{int(safe_float(t.get('ret3_pct')))}%"),
        },
        "sample_trades": full_trades[-120:],
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
    contexts = prepare_contexts(loaded)
    rows = [evaluate_variant(contexts, variant) for variant in variants()]
    rows.sort(key=lambda item: safe_float(item.get("robust_score")), reverse=True)
    decision_counts = dict(Counter(row["decision"] for row in rows))
    best = rows[0] if rows else {}
    return {
        "generated_at": now_iso(),
        "module": "x7_range_reversion_strategy_research",
        "status": "completed",
        "days": days,
        "interval": INTERVAL,
        "signal": SIGNAL,
        "symbols": symbols,
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
        "next_action_rules": [
            "research_candidate still needs manual review before any paper-shadow proposal.",
            "near_miss requires a new pre-registered hypothesis, not parameter fitting.",
            "rejected variants must not be tuned for prettier backtests.",
        ],
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


def table(rows: list[dict[str, Any]], columns: list[tuple[str, str]], limit: int = 200) -> str:
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
        ("train.net_profit_usdt", "训练净利"),
        ("validation.net_profit_usdt", "验证净利"),
        ("test.net_profit_usdt", "测试净利"),
        ("full.profit_factor", "PF"),
        ("full.max_drawdown_pct", "回撤%"),
        ("full.trades", "交易"),
        ("max_symbol_share_pct", "最大币占比%"),
        ("max_month_share_pct", "最大月占比%"),
        ("positive_month_share_pct", "正收益月份%"),
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
<html lang="zh-CN"><head><meta charset="utf-8"/><title>X7 Range Reversion Strategy Research</title>
<style>
body{{margin:0;background:#08111a;color:#e9f1f8;font-family:Segoe UI,Arial,sans-serif}} main{{max-width:1440px;margin:0 auto;padding:28px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px}} .panel{{background:#111b26;border:1px solid #263545;border-radius:8px;padding:16px;overflow:auto}}
h1{{margin:0 0 8px;font-size:28px}} h2{{font-size:18px}} p,li{{color:#91a2b1}} code{{color:#f4c95d}}
table{{width:100%;border-collapse:collapse;font-size:13px}} th,td{{border-bottom:1px solid #263545;padding:8px;text-align:right;white-space:nowrap}} th:first-child,td:first-child{{text-align:left}} th{{color:#91a2b1}}
</style></head><body><main>
<h1>X7 区间过热做空均值回归完整复核</h1>
<p>Generated: <code>{escape(payload['generated_at'])}</code> / Signal: <code>{escape(payload['signal'])}</code> / Local only / No Binance / No live mutation.</p>
<p>Best: <code>{escape(str(best.get('variant','')))}</code> / decision <code>{escape(str(best.get('decision','')))}</code> / reasons <code>{escape(', '.join(best.get('anti_fit_reasons') or []))}</code></p>
<section class="grid">
<div class="panel"><h2>变体总榜</h2>{table(payload.get('results') or [], cols, 80)}</div>
<div class="panel"><h2>最佳成本压力</h2>{table(best.get('cost_stress') or [], stress_cols, 20)}</div>
<div class="panel"><h2>最佳分币种</h2>{table((best.get('breakdowns') or {}).get('by_symbol') or [], breakdown_cols, 80)}</div>
<div class="panel"><h2>最佳分月份</h2>{table((best.get('breakdowns') or {}).get('by_month') or [], breakdown_cols, 80)}</div>
<div class="panel"><h2>最佳退出原因</h2>{table((best.get('breakdowns') or {}).get('by_exit_reason') or [], breakdown_cols, 40)}</div>
<div class="panel"><h2>下一步规则</h2><ul>{''.join(f'<li>{escape(rule)}</li>' for rule in payload.get('next_action_rules') or [])}</ul></div>
</section>
<p>JSON: <code>{escape(payload['paths']['json'])}</code></p>
</main></body></html>"""
    return html


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local X7 range reversion strategy research.")
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
