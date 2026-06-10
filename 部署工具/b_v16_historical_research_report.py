"""B/v16 one-year historical research report.

Read-only Tencent research runner. It compares a small pre-registered B/v16
parameter set on the completed historical Kline warehouse and writes a readable
HTML report. It never changes live strategy config, scanners, orders, or
automatic upgrade state.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
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
import v11_historical_research_report as shared


CST = timezone(timedelta(hours=8))
DEFAULT_INTERVALS = ["15m", "30m", "1h", "4h"]
MIN_SPLIT_TRADES = 5
TITLE = "B/v16 一年历史回测与参数研究"


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def latest_json_path(root: Path = ROOT) -> Path:
    return shared.runtime_dir(root) / "b_v16_historical_research_latest.json"


def latest_html_path(root: Path = ROOT) -> Path:
    return shared.reports_dir(root) / "b_v16_historical_research_latest.html"


def read_json(path: Path) -> dict[str, Any]:
    return shared.read_json(path)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    shared.write_json(path, payload)


def safety_payload() -> dict[str, Any]:
    return {
        "binance_requests_enabled": False,
        "paper_or_real_orders": False,
        "live_scanner_impact": "none",
        "strategy_frequency_change": False,
        "live_config_mutation": False,
        "auto_apply_allowed": False,
        "automatic_tuning_allowed": False,
        "automatic_rollback_allowed": False,
        "automatic_upgrade_allowed": False,
    }


def default_b_v16_params(interval: str) -> dict[str, float]:
    """Baseline approximates current B/v16 live-approved parameters.

    Live B uses 1h entry + 15m confirmation. The research adapter can run other
    intervals for comparison, so 30m/4h reuse the 1h score baseline as a
    reference only.
    """

    score_threshold = 55.0 if interval == "15m" else 38.0
    return {
        "score_threshold": score_threshold,
        "overheat_cap": 85.0,
        "atr_stop_multiplier": 2.0,
        "trailing_pullback_atr": 1.0,
        "trailing_activation_atr": 1.0,
        "tp_mult": 4.0,
        "trade_size_usdt": 100.0,
        "leverage": 4.0,
    }


def build_variants(interval: str, max_variants: int) -> list[dict[str, Any]]:
    base = default_b_v16_params(interval)
    raw: list[tuple[str, dict[str, float]]] = [("baseline_live_approx", dict(base))]

    threshold_candidates = [50.0, 55.0, 60.0, 65.0] if interval == "15m" else [40.0, 42.0, 46.0, 50.0]
    for value in threshold_candidates:
        params = dict(base)
        params["score_threshold"] = value
        raw.append((f"score_threshold={value:g}", params))

    for value in [75.0, 80.0, 90.0, 100.0]:
        params = dict(base)
        params["overheat_cap"] = value
        raw.append((f"overheat_cap={value:g}", params))

    for value in [1.5, 2.5, 3.0]:
        params = dict(base)
        params["atr_stop_multiplier"] = value
        raw.append((f"atr_stop_multiplier={value:g}", params))

    combo_rows = (
        [(50.0, 80.0, 2.0), (60.0, 80.0, 2.0), (55.0, 90.0, 2.5), (60.0, 90.0, 1.5)]
        if interval == "15m"
        else [(40.0, 80.0, 2.0), (42.0, 80.0, 2.5), (46.0, 90.0, 1.5), (50.0, 90.0, 2.0)]
    )
    for threshold, cap, stop in combo_rows:
        params = dict(base)
        params["score_threshold"] = threshold
        params["overheat_cap"] = cap
        params["atr_stop_multiplier"] = stop
        raw.append((f"score={threshold:g},cap={cap:g},stop={stop:g}", params))

    variants: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name, params in raw:
        key = json.dumps(params, sort_keys=True)
        if key in seen:
            continue
        variants.append({"name": name, "params": params})
        seen.add(key)
        if len(variants) >= max(1, max_variants):
            break
    return variants


def robust_score(row: dict[str, Any]) -> float:
    full = row.get("full") if isinstance(row.get("full"), dict) else {}
    validation = row.get("validation") if isinstance(row.get("validation"), dict) else {}
    test = row.get("test") if isinstance(row.get("test"), dict) else {}
    net = backtest_engine.safe_float(test.get("net_profit_usdt"))
    val_net = backtest_engine.safe_float(validation.get("net_profit_usdt"))
    full_net = backtest_engine.safe_float(full.get("net_profit_usdt"))
    dd = max(0.0, backtest_engine.safe_float(full.get("max_drawdown_pct")))
    pf = backtest_engine.safe_float(test.get("profit_factor"))
    trades = min(
        backtest_engine.safe_int(validation.get("trades")),
        backtest_engine.safe_int(test.get("trades")),
    )
    penalty = dd * 3.0
    if val_net <= 0:
        penalty += abs(val_net) + 100.0
    if net <= 0:
        penalty += abs(net) + 150.0
    if trades < MIN_SPLIT_TRADES:
        penalty += (MIN_SPLIT_TRADES - trades) * 50.0
    return full_net * 0.15 + val_net * 0.35 + net * 0.50 + pf * 10.0 - penalty


def anti_fit_reasons(row: dict[str, Any]) -> list[str]:
    train = row.get("train") if isinstance(row.get("train"), dict) else {}
    validation = row.get("validation") if isinstance(row.get("validation"), dict) else {}
    test = row.get("test") if isinstance(row.get("test"), dict) else {}
    full = row.get("full") if isinstance(row.get("full"), dict) else {}
    reasons: list[str] = []
    for label, summary in (("train", train), ("validation", validation), ("test", test)):
        if backtest_engine.safe_int(summary.get("trades")) < MIN_SPLIT_TRADES:
            reasons.append(f"{label}_trade_count_low")
        if backtest_engine.safe_float(summary.get("net_profit_usdt")) <= 0:
            reasons.append(f"{label}_net_not_positive")
    if backtest_engine.safe_float(test.get("profit_factor")) < 1.05:
        reasons.append("test_profit_factor_below_1.05")
    if backtest_engine.safe_float(full.get("max_drawdown_pct")) > 20.0:
        reasons.append("drawdown_above_20pct")
    return reasons


def neighbor_stability(name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    target = next((row for row in rows if row.get("name") == name), None)
    if not target:
        return {"status": "unknown", "positive_neighbors": 0, "neighbors": 0}
    params = target.get("params") if isinstance(target.get("params"), dict) else {}
    tolerances = {
        "score_threshold": 4.01,
        "overheat_cap": 5.01,
        "atr_stop_multiplier": 0.51,
        "trailing_pullback_atr": 0.21,
        "trailing_activation_atr": 0.21,
        "tp_mult": 0.51,
        "trade_size_usdt": 0.01,
        "leverage": 0.01,
    }
    neighbors = []
    for row in rows:
        if row is target:
            continue
        other = row.get("params") if isinstance(row.get("params"), dict) else {}
        keys = set(params) | set(other)
        if not keys:
            continue
        close = True
        for key in keys:
            a = backtest_engine.safe_float(params.get(key))
            b = backtest_engine.safe_float(other.get(key))
            if abs(a - b) > tolerances.get(key, 1e-9):
                close = False
                break
        if close:
            neighbors.append(row)
    positive = sum(1 for row in neighbors if backtest_engine.safe_float((row.get("test") or {}).get("net_profit_usdt")) > 0)
    status = "stable_enough" if neighbors and positive / len(neighbors) >= 0.5 else "weak_or_sparse"
    return {"status": status, "positive_neighbors": positive, "neighbors": len(neighbors)}


def run_variant(
    *,
    spec: dict[str, Any],
    symbol_bars: dict[str, list[dict[str, Any]]],
    variant: dict[str, Any],
) -> dict[str, Any]:
    params = dict(variant.get("params") or {})
    trades, full, charts = backtest_engine.run_for_params(spec=spec, symbol_bars=symbol_bars, params=params)
    _tr, train, _tc = backtest_engine.run_for_params(spec=spec, symbol_bars=symbol_bars, params=params, split="train")
    _va, validation, _vc = backtest_engine.run_for_params(spec=spec, symbol_bars=symbol_bars, params=params, split="validation")
    _te, test, _ec = backtest_engine.run_for_params(spec=spec, symbol_bars=symbol_bars, params=params, split="test")
    row = {
        "name": variant.get("name") or "variant",
        "params": params,
        "full": full,
        "train": train,
        "validation": validation,
        "test": test,
        "charts": {
            "equity_curve": charts.get("equity_curve", [])[-600:],
            "monthly_returns": charts.get("monthly_returns", []),
            "drawdown": charts.get("drawdown", [])[-600:],
        },
        "trades": sorted(trades, key=lambda item: str(item.get("exit_ts") or item.get("entry_ts") or ""))[-500:],
    }
    row["anti_fit_reasons"] = anti_fit_reasons(row)
    row["anti_fit_pass"] = not row["anti_fit_reasons"]
    row["robust_score"] = round(robust_score(row), 6)
    return row


def per_symbol_rows(
    *,
    spec: dict[str, Any],
    symbol_bars: dict[str, list[dict[str, Any]]],
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for symbol, bars in symbol_bars.items():
        if len(bars) < backtest_engine.MIN_BARS:
            rows.append({"symbol": symbol, "usable": False, "bars": len(bars), "summary": {}})
            continue
        _trades, summary, _charts = backtest_engine.run_for_params(spec=spec, symbol_bars={symbol: bars}, params=params)
        rows.append({"symbol": symbol, "usable": True, "bars": len(bars), "summary": summary})
    return rows


def choose_variants(rows: list[dict[str, Any]]) -> dict[str, Any]:
    baseline = next((row for row in rows if row.get("name") == "baseline_live_approx"), rows[0] if rows else {})
    best_full = max(rows, key=lambda item: backtest_engine.safe_float((item.get("full") or {}).get("net_profit_usdt"))) if rows else {}
    eligible = [row for row in rows if row.get("anti_fit_pass")]
    best_robust = max(eligible, key=lambda item: backtest_engine.safe_float(item.get("robust_score"))) if eligible else {}
    if not best_robust and rows:
        best_robust = max(rows, key=lambda item: backtest_engine.safe_float(item.get("robust_score")))
    return {"baseline": baseline, "best_full": best_full, "best_robust": best_robust}


def write_progress(root: Path, payload: dict[str, Any]) -> None:
    base = {
        "generated_at": now_iso(),
        "strategy": "B/v16",
        "status": "running",
        "engine_parity": "research_adapter",
        "safety": safety_payload(),
    }
    base.update(payload)
    write_json(latest_json_path(root), base)


def run_research(
    *,
    root: Path = ROOT,
    intervals: list[str] | None = None,
    symbols: list[str] | None = None,
    period_days: int = 365,
    capital_usdt: float = 10_000.0,
    fee_bps: float = 4.0,
    slippage_bps: float = 0.0,
    max_variants: int = 16,
) -> dict[str, Any]:
    intervals = intervals or list(DEFAULT_INTERVALS)
    symbols = shared.universe_symbols(root, symbols)
    end = datetime.now(CST)
    start = end - timedelta(days=max(7, min(365, int(period_days))))

    write_progress(
        root,
        {
            "status": "loading_history",
            "progress": {"completed": 0, "total": 1, "percent": 0.0, "current": "load historical_klines"},
            "symbols": symbols,
            "intervals": intervals,
            "period": {"start": start.isoformat(timespec="seconds"), "end": end.isoformat(timespec="seconds")},
        },
    )
    loaded = shared.load_all_bars(root=root, symbols=symbols, intervals=intervals, start=start, end=end)
    coverage = shared.coverage_rows(loaded)
    total_jobs = sum(len(build_variants(interval, max_variants)) for interval in intervals)
    completed = 0
    interval_results: dict[str, Any] = {}

    for interval in intervals:
        variants = build_variants(interval, max_variants)
        usable = {symbol: bars for symbol, bars in loaded.get(interval, {}).items() if len(bars) >= backtest_engine.MIN_BARS}
        spec = {
            "strategy": "B/v16",
            "symbols": list(usable),
            "interval": interval,
            "direction": "strategy_default",
            "period_days": period_days,
            "start": start.isoformat(timespec="seconds"),
            "end": end.isoformat(timespec="seconds"),
            "capital_usdt": capital_usdt,
            "fee_bps": fee_bps,
            "slippage_bps": slippage_bps,
            "fill_model": "paper_fill_model_v2",
            "parameter_variants": len(variants),
        }
        rows: list[dict[str, Any]] = []
        for variant in variants:
            completed += 1
            write_progress(
                root,
                {
                    "progress": {
                        "completed": completed - 1,
                        "total": total_jobs,
                        "percent": round((completed - 1) / max(total_jobs, 1) * 100.0, 2),
                        "current": f"{interval} {variant.get('name')}",
                    },
                    "coverage": {
                        "usable_symbol_intervals": sum(1 for row in coverage if row.get("usable")),
                        "target_symbol_intervals": len(coverage),
                    },
                },
            )
            if usable:
                rows.append(run_variant(spec=spec, symbol_bars=usable, variant=variant))
            else:
                rows.append(
                    {
                        "name": variant.get("name") or "variant",
                        "params": variant.get("params") or {},
                        "full": {"net_profit_usdt": 0.0, "trades": 0, "return_pct": 0.0, "max_drawdown_pct": 0.0},
                        "train": {"net_profit_usdt": 0.0, "trades": 0},
                        "validation": {"net_profit_usdt": 0.0, "trades": 0},
                        "test": {"net_profit_usdt": 0.0, "trades": 0},
                        "charts": {"equity_curve": [], "monthly_returns": [], "drawdown": []},
                        "trades": [],
                        "anti_fit_reasons": ["no_usable_symbols"],
                        "anti_fit_pass": False,
                        "robust_score": -999999.0,
                    }
                )
        chosen = choose_variants(rows)
        for key in ("best_full", "best_robust"):
            candidate = chosen.get(key) if isinstance(chosen.get(key), dict) else {}
            if candidate:
                candidate["neighbor_stability"] = neighbor_stability(str(candidate.get("name") or ""), rows)
        baseline = chosen.get("baseline") if isinstance(chosen.get("baseline"), dict) else {}
        best_robust = chosen.get("best_robust") if isinstance(chosen.get("best_robust"), dict) else {}
        baseline_params = baseline.get("params") if isinstance(baseline.get("params"), dict) else default_b_v16_params(interval)
        interval_results[interval] = {
            "interval": interval,
            "usable_symbols": list(usable),
            "usable_symbol_count": len(usable),
            "target_symbol_count": len(symbols),
            "variant_count": len(rows),
            "variants": compact_variants(rows),
            "baseline": baseline,
            "best_full": chosen.get("best_full") or {},
            "best_robust": best_robust,
            "per_symbol_baseline": per_symbol_rows(spec=spec, symbol_bars=loaded.get(interval, {}), params=baseline_params),
            "research_decision": interval_decision(baseline, best_robust),
            "interval_note": (
                "live_entry_reference" if interval == "1h" else "research_comparison_only_not_current_b_v16_entry_timeframe"
            ),
        }

    payload = build_payload(
        root=root,
        intervals=intervals,
        symbols=symbols,
        start=start,
        end=end,
        coverage=coverage,
        interval_results=interval_results,
        capital_usdt=capital_usdt,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
        max_variants=max_variants,
    )
    html = render_html(payload)
    html_path = latest_html_path(root)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html, encoding="utf-8")
    payload["report_path"] = str(html_path)
    write_json(latest_json_path(root), payload)
    return payload


def compact_variants(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "name": row.get("name") or "",
                "params": row.get("params") or {},
                "full": row.get("full") or {},
                "train": row.get("train") or {},
                "validation": row.get("validation") or {},
                "test": row.get("test") or {},
                "anti_fit_pass": bool(row.get("anti_fit_pass")),
                "anti_fit_reasons": row.get("anti_fit_reasons") or [],
                "robust_score": row.get("robust_score", 0.0),
            }
        )
    return out


def interval_decision(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    base_full = baseline.get("full") if isinstance(baseline.get("full"), dict) else {}
    cand_full = candidate.get("full") if isinstance(candidate.get("full"), dict) else {}
    if not candidate:
        return {
            "action": "no_candidate",
            "reason": "no_usable_research_candidate",
            "auto_apply_allowed": False,
        }
    improvement = backtest_engine.safe_float(cand_full.get("net_profit_usdt")) - backtest_engine.safe_float(base_full.get("net_profit_usdt"))
    if candidate.get("anti_fit_pass") and improvement > 0:
        action = "research_candidate_only"
        reason = "candidate_improves_baseline_and_passes_local_oos_checks"
    elif improvement > 0:
        action = "better_full_window_but_not_robust"
        reason = "single_window_or_oos_gate_not_good_enough"
    else:
        action = "keep_baseline_for_research"
        reason = "candidate_does_not_improve_baseline_after_oos_penalty"
    return {
        "action": action,
        "reason": reason,
        "net_improvement_usdt": round(improvement, 6),
        "auto_apply_allowed": False,
        "automatic_upgrade_allowed": False,
    }


def build_payload(
    *,
    root: Path,
    intervals: list[str],
    symbols: list[str],
    start: datetime,
    end: datetime,
    coverage: list[dict[str, Any]],
    interval_results: dict[str, Any],
    capital_usdt: float,
    fee_bps: float,
    slippage_bps: float,
    max_variants: int,
) -> dict[str, Any]:
    baseline_total_net = 0.0
    candidate_total_net = 0.0
    baseline_total_trades = 0
    candidate_total_trades = 0
    robust_candidates = 0
    for result in interval_results.values():
        baseline = result.get("baseline") if isinstance(result.get("baseline"), dict) else {}
        candidate = result.get("best_robust") if isinstance(result.get("best_robust"), dict) else {}
        baseline_full = baseline.get("full") if isinstance(baseline.get("full"), dict) else {}
        candidate_full = candidate.get("full") if isinstance(candidate.get("full"), dict) else {}
        baseline_total_net += backtest_engine.safe_float(baseline_full.get("net_profit_usdt"))
        candidate_total_net += backtest_engine.safe_float(candidate_full.get("net_profit_usdt"))
        baseline_total_trades += backtest_engine.safe_int(baseline_full.get("trades"))
        candidate_total_trades += backtest_engine.safe_int(candidate_full.get("trades"))
        if candidate.get("anti_fit_pass"):
            robust_candidates += 1

    hist = shared.historical_payload(root)
    quality = hist.get("quality") if isinstance(hist.get("quality"), dict) else {}
    payload = {
        "generated_at": now_iso(),
        "status": "completed",
        "strategy": "B/v16",
        "engine_parity": "research_adapter",
        "adapter_note": "historical Kline research adapter; not live scanner byte-for-byte replay",
        "safety": safety_payload(),
        "period": {"start": start.isoformat(timespec="seconds"), "end": end.isoformat(timespec="seconds"), "days": (end - start).days},
        "config": {
            "intervals": intervals,
            "symbols": symbols,
            "capital_usdt_per_interval": capital_usdt,
            "fee_bps": fee_bps,
            "slippage_bps": slippage_bps,
            "max_variants_per_interval": max_variants,
            "baseline_params": {interval: default_b_v16_params(interval) for interval in intervals},
            "tested_parameters": ["score_threshold", "overheat_cap", "atr_stop_multiplier"],
            "not_tested_parameters": ["ofi_threshold"],
            "not_tested_reason": "ofi_threshold is registered, but this one-year warehouse has Kline bars only; no aligned one-year OFI/CVD/depth feature history is available in the research adapter.",
            "live_timeframe_note": "Current B/v16 live entry is 1h with 15m confirmation. 15m/30m/4h rows here are research comparisons, not proof those live entry timeframes are enabled.",
            "atr_stop_note": "Live B/v16 uses ATR stop bands by volatility regime; this adapter tests fixed atr_stop_multiplier values only.",
        },
        "historical_quality": quality,
        "coverage": {
            "rows": coverage,
            "usable_symbol_intervals": sum(1 for row in coverage if row.get("usable")),
            "target_symbol_intervals": len(coverage),
            "usable_symbols": len({row.get("symbol") for row in coverage if row.get("usable")}),
            "target_symbols": len(symbols),
        },
        "portfolio_summary": {
            "baseline_net_profit_usdt": round(baseline_total_net, 6),
            "baseline_return_pct_on_interval_capital": round(baseline_total_net / max(capital_usdt * len(intervals), 1.0) * 100.0, 6),
            "baseline_trades": baseline_total_trades,
            "candidate_net_profit_usdt": round(candidate_total_net, 6),
            "candidate_return_pct_on_interval_capital": round(candidate_total_net / max(capital_usdt * len(intervals), 1.0) * 100.0, 6),
            "candidate_trades": candidate_total_trades,
            "robust_candidate_intervals": robust_candidates,
            "intervals": len(intervals),
            "auto_apply_allowed": False,
        },
        "interval_results": interval_results,
        "operator_summary": operator_summary(interval_results),
    }
    return payload


def operator_summary(interval_results: dict[str, Any]) -> dict[str, Any]:
    lines: list[str] = []
    candidates: list[str] = []
    warnings: list[str] = []
    for interval, result in interval_results.items():
        baseline = result.get("baseline") if isinstance(result.get("baseline"), dict) else {}
        best_full = result.get("best_full") if isinstance(result.get("best_full"), dict) else {}
        best_robust = result.get("best_robust") if isinstance(result.get("best_robust"), dict) else {}
        decision = result.get("research_decision") if isinstance(result.get("research_decision"), dict) else {}
        base_net = backtest_engine.safe_float((baseline.get("full") or {}).get("net_profit_usdt"))
        full_net = backtest_engine.safe_float((best_full.get("full") or {}).get("net_profit_usdt"))
        robust_net = backtest_engine.safe_float((best_robust.get("full") or {}).get("net_profit_usdt"))
        lines.append(
            f"{interval}: baseline {base_net:+.2f} USDT; best_full {full_net:+.2f}; best_robust {robust_net:+.2f}; {decision.get('action', '-')}"
        )
        if best_robust.get("anti_fit_pass") and decision.get("action") == "research_candidate_only":
            candidates.append(f"{interval} {best_robust.get('name')}")
        elif full_net > base_net and not best_full.get("anti_fit_pass"):
            warnings.append(f"{interval} full-window improved but OOS/anti-fit failed")
    overall = "no_live_parameter_change"
    if candidates:
        overall = "research_candidates_need_manual_review"
    return {
        "overall_action": overall,
        "lines": lines,
        "research_candidates": candidates,
        "warnings": warnings,
        "plain_advice": (
            "只把通过 OOS/邻近稳定性初筛的参数作为下一轮人工研究候选；不要把单窗口最好曲线直接写入 B/v16 实盘配置。"
        ),
        "auto_apply_allowed": False,
        "automatic_upgrade_allowed": False,
    }


def fmt(value: Any, digits: int = 2, signed: bool = False) -> str:
    try:
        number = float(value)
    except Exception:
        return "-"
    if not math.isfinite(number):
        return "-"
    prefix = "+" if signed and number > 0 else ""
    return f"{prefix}{number:.{digits}f}"


def h(value: Any) -> str:
    return escape(str(value if value is not None else ""))


def params_text(params: Any) -> str:
    if not isinstance(params, dict) or not params:
        return "baseline/default"
    selected = ["score_threshold", "overheat_cap", "atr_stop_multiplier", "trailing_pullback_atr", "tp_mult"]
    parts = [f"{key}={params[key]}" for key in selected if key in params]
    return ", ".join(parts) or ", ".join(f"{key}={value}" for key, value in params.items())


def metric_cells(summary: dict[str, Any]) -> str:
    return (
        f"<td>{h(fmt(summary.get('net_profit_usdt'), 2, True))}</td>"
        f"<td>{h(fmt(summary.get('return_pct'), 2, True))}%</td>"
        f"<td>{h(fmt(summary.get('max_drawdown_pct'), 2))}%</td>"
        f"<td>{h(fmt(summary.get('profit_factor'), 2))}</td>"
        f"<td>{h(fmt(summary.get('win_rate_pct'), 2))}%</td>"
        f"<td>{h(summary.get('trades') or 0)}</td>"
    )


def render_svg_curve(points: list[dict[str, Any]]) -> str:
    return shared.render_svg_curve(points)


def render_variant_table(rows: list[dict[str, Any]]) -> str:
    body = []
    for row in sorted(rows, key=lambda item: backtest_engine.safe_float((item.get("full") or {}).get("net_profit_usdt")), reverse=True):
        full = row.get("full") if isinstance(row.get("full"), dict) else {}
        validation = row.get("validation") if isinstance(row.get("validation"), dict) else {}
        test = row.get("test") if isinstance(row.get("test"), dict) else {}
        body.append(
            "<tr>"
            f"<td>{h(row.get('name'))}<small>{h(params_text(row.get('params')))}</small></td>"
            f"{metric_cells(full)}"
            f"<td>{h(fmt(validation.get('net_profit_usdt'), 2, True))}</td>"
            f"<td>{h(fmt(test.get('net_profit_usdt'), 2, True))}</td>"
            f"<td>{'通过' if row.get('anti_fit_pass') else '未过'}<small>{h(', '.join(row.get('anti_fit_reasons') or []) or '-')}</small></td>"
            "</tr>"
        )
    return (
        '<div class="scroll-table"><table><thead><tr><th>参数</th><th>净收益</th><th>收益率</th>'
        "<th>最大回撤</th><th>PF</th><th>胜率</th><th>交易数</th><th>验证净收益</th><th>测试净收益</th><th>反拟合</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table></div>"
    )


def render_symbol_table(rows: list[dict[str, Any]]) -> str:
    usable = [row for row in rows if row.get("usable")]
    usable.sort(key=lambda item: backtest_engine.safe_float((item.get("summary") or {}).get("net_profit_usdt")))
    selected = usable[:8] + usable[-8:]
    seen: set[str] = set()
    body = []
    for row in selected:
        symbol = str(row.get("symbol") or "")
        if symbol in seen:
            continue
        seen.add(symbol)
        summary = row.get("summary") if isinstance(row.get("summary"), dict) else {}
        body.append(f"<tr><td>{h(symbol)}</td><td>{h(row.get('bars'))}</td>{metric_cells(summary)}</tr>")
    if not body:
        return '<p class="empty">无可用币种。</p>'
    return (
        '<div class="scroll-table compact"><table><thead><tr><th>币种</th><th>K线数</th><th>净收益</th><th>收益率</th>'
        "<th>最大回撤</th><th>PF</th><th>胜率</th><th>交易数</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table></div>"
    )


def render_trade_table(trades: list[dict[str, Any]]) -> str:
    body = []
    for trade in trades[-160:]:
        body.append(
            "<tr>"
            f"<td>{h(trade.get('symbol'))}<small>{h(trade.get('side'))}</small></td>"
            f"<td>{h(trade.get('interval'))}</td>"
            f"<td>{h(trade.get('entry_ts'))}<small>{h(fmt(trade.get('entry_price'), 8))}</small></td>"
            f"<td>{h(trade.get('exit_ts'))}<small>{h(fmt(trade.get('exit_price'), 8))}</small></td>"
            f"<td>{h(fmt(trade.get('net_pnl_usdt'), 4, True))}<small>fee {h(fmt(trade.get('fee_usdt'), 4))}</small></td>"
            f"<td>{h(trade.get('exit_reason'))}<small>score {h(fmt(trade.get('score'), 2))} / threshold {h(fmt(trade.get('threshold'), 2))}</small></td>"
            "</tr>"
        )
    if not body:
        return '<p class="empty">无交易明细。</p>'
    return (
        '<div class="trade-scroll"><table><thead><tr><th>币种</th><th>周期</th><th>开仓</th><th>平仓</th><th>盈亏</th><th>原因</th></tr></thead>'
        f"<tbody>{''.join(body)}</tbody></table></div>"
    )


def render_html(payload: dict[str, Any]) -> str:
    portfolio = payload.get("portfolio_summary") if isinstance(payload.get("portfolio_summary"), dict) else {}
    coverage = payload.get("coverage") if isinstance(payload.get("coverage"), dict) else {}
    operator = payload.get("operator_summary") if isinstance(payload.get("operator_summary"), dict) else {}
    interval_sections = []
    for interval, result in (payload.get("interval_results") or {}).items():
        baseline = result.get("baseline") if isinstance(result.get("baseline"), dict) else {}
        best_full = result.get("best_full") if isinstance(result.get("best_full"), dict) else {}
        best_robust = result.get("best_robust") if isinstance(result.get("best_robust"), dict) else {}
        decision = result.get("research_decision") if isinstance(result.get("research_decision"), dict) else {}
        variants = result.get("variants") if isinstance(result.get("variants"), list) else []
        chart = render_svg_curve((best_robust.get("charts") or {}).get("equity_curve") or [])
        interval_sections.append(
            f"""
<section>
  <h2>{h(interval)} 周期</h2>
  <div class="metrics">
    <div><span>可用币种</span><b>{h(result.get('usable_symbol_count'))}/{h(result.get('target_symbol_count'))}</b></div>
    <div><span>baseline 净收益</span><b>{h(fmt((baseline.get('full') or {}).get('net_profit_usdt'), 2, True))} USDT</b></div>
    <div><span>全窗口最好</span><b>{h(best_full.get('name'))}</b><small>{h(params_text(best_full.get('params')))}</small></div>
    <div><span>稳健候选</span><b>{h(best_robust.get('name'))}</b><small>{h(params_text(best_robust.get('params')))}</small></div>
  </div>
  <p class="advice">判断：{h(decision.get('action'))}；原因：{h(decision.get('reason'))}；相对 baseline：{h(fmt(decision.get('net_improvement_usdt'), 2, True))} USDT。邻近稳定性：{h((best_robust.get('neighbor_stability') or {}).get('status') or '-')}。周期说明：{h(result.get('interval_note') or '')}。</p>
  <div class="chart">{chart}</div>
  <h3>参数比较</h3>
  {render_variant_table(variants)}
  <h3>币种贡献：baseline 最差/最好</h3>
  {render_symbol_table(result.get('per_symbol_baseline') if isinstance(result.get('per_symbol_baseline'), list) else [])}
  <h3>详细开平仓记录：稳健候选最近 160 笔</h3>
  {render_trade_table(best_robust.get('trades') if isinstance(best_robust.get('trades'), list) else [])}
</section>
""".strip()
        )
    lines = "".join(f"<li>{h(line)}</li>" for line in operator.get("lines", []))
    warnings = "".join(f"<li>{h(line)}</li>" for line in operator.get("warnings", []))
    candidates = "".join(f"<li>{h(line)}</li>" for line in operator.get("research_candidates", []))
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{TITLE}</title>
<style>
:root {{
  --bg:#071019; --panel:#0d1825; --panel2:#101f30; --text:#e7eef8; --muted:#91a4bb;
  --line:#223247; --up:#22c55e; --down:#ef4444; --cyan:#38bdf8; --warn:#f59e0b;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; font-family:Inter,Segoe UI,Arial,"Microsoft YaHei",sans-serif; background:var(--bg); color:var(--text); }}
header {{ padding:28px 32px 18px; border-bottom:1px solid var(--line); background:#091522; }}
h1 {{ margin:0 0 8px; font-size:28px; letter-spacing:0; }}
h2 {{ margin:0 0 14px; font-size:21px; }}
h3 {{ margin:18px 0 10px; font-size:16px; color:#dbeafe; }}
p {{ color:var(--muted); line-height:1.6; }}
main {{ padding:22px 32px 42px; max-width:1420px; margin:0 auto; }}
section {{ margin:0 0 22px; padding:18px; background:var(--panel); border:1px solid var(--line); border-radius:8px; }}
.metrics {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin:12px 0; }}
.metrics div,.summary-card {{ padding:13px; background:var(--panel2); border:1px solid var(--line); border-radius:8px; }}
.metrics span,.summary-card span,small {{ display:block; color:var(--muted); font-size:12px; }}
.metrics b,.summary-card b {{ display:block; color:#f8fbff; font-size:18px; margin-top:4px; }}
.summary-grid {{ display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:12px; margin-top:16px; }}
.advice {{ padding:10px 12px; border-left:4px solid var(--cyan); background:#0a1725; color:#cbd5e1; }}
.warn {{ border-left-color:var(--warn); }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th,td {{ border-bottom:1px solid var(--line); padding:9px 10px; text-align:left; vertical-align:top; }}
th {{ color:#bfdbfe; background:#0b1724; position:sticky; top:0; z-index:1; }}
td small {{ margin-top:3px; }}
.scroll-table {{ max-height:390px; overflow:auto; border:1px solid var(--line); border-radius:8px; }}
.scroll-table.compact {{ max-height:320px; }}
.trade-scroll {{ max-height:420px; overflow:auto; border:1px solid var(--line); border-radius:8px; }}
.chart {{ background:#081420; border:1px solid var(--line); border-radius:8px; padding:10px; margin:12px 0; }}
.chart svg {{ width:100%; height:190px; display:block; }}
.empty,.empty-chart {{ color:var(--muted); padding:18px; }}
ul {{ margin:8px 0 0 18px; padding:0; color:#cbd5e1; }}
code {{ color:#e0f2fe; }}
@media (max-width:900px) {{
  header,main {{ padding-left:14px; padding-right:14px; }}
  .metrics,.summary-grid {{ grid-template-columns:1fr; }}
  table {{ min-width:980px; }}
}}
</style>
</head>
<body>
<header>
  <h1>{TITLE}</h1>
  <p>生成时间：{h(payload.get('generated_at'))}；周期：{h((payload.get('period') or {}).get('start'))} 至 {h((payload.get('period') or {}).get('end'))}。口径：research adapter，不是实盘 scanner 逐行复刻。</p>
</header>
<main>
  <section>
    <h2>总体结论</h2>
    <div class="summary-grid">
      <div class="summary-card"><span>baseline 总净收益</span><b>{h(fmt(portfolio.get('baseline_net_profit_usdt'), 2, True))} USDT</b></div>
      <div class="summary-card"><span>baseline 交易数</span><b>{h(portfolio.get('baseline_trades'))}</b></div>
      <div class="summary-card"><span>候选总净收益</span><b>{h(fmt(portfolio.get('candidate_net_profit_usdt'), 2, True))} USDT</b></div>
      <div class="summary-card"><span>可用覆盖</span><b>{h(coverage.get('usable_symbol_intervals'))}/{h(coverage.get('target_symbol_intervals'))}</b></div>
      <div class="summary-card"><span>自动应用</span><b>禁止</b></div>
    </div>
    <p class="advice">{h(operator.get('plain_advice'))}</p>
    <ul>{lines}</ul>
    <h3>通过本地 OOS 初筛的研究候选</h3>
    <ul>{candidates or '<li>无。维持 baseline 研究观察，不写入实盘配置。</li>'}</ul>
    <h3>需要警惕</h3>
    <ul>{warnings or '<li>未发现单窗口明显优于 baseline 但 OOS 明显失败的参数。</li>'}</ul>
  </section>
  <section>
    <h2>安全边界</h2>
    <p>本报告只读 Tencent 一年历史 K线仓；不调用 Binance；不发订单；不改 A/B/C 扫描频率；不改 <code>config/v16.toml</code>；不触发自动调参、自动回滚、自动升级。</p>
    <p>本轮只测试 <code>score_threshold</code>、<code>overheat_cap</code>、<code>atr_stop_multiplier</code>。<code>ofi_threshold</code> 已登记但当前一年仓没有对齐的一年期 OFI/CVD/盘口特征序列，因此不拿它做调参依据，避免假拟合。</p>
    <p>当前 B/v16 live 主路径是 1h 入场 + 15m 确认；本报告的 15m/30m/4h 是横向研究，不代表这些周期已进入实盘入场路径。</p>
  </section>
  {''.join(interval_sections)}
</main>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run B/v16 historical research report")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--intervals", default=",".join(DEFAULT_INTERVALS))
    parser.add_argument("--symbols", default="")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--capital-usdt", type=float, default=10_000.0)
    parser.add_argument("--fee-bps", type=float, default=4.0)
    parser.add_argument("--slippage-bps", type=float, default=0.0)
    parser.add_argument("--max-variants", type=int, default=16)
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    payload = run_research(
        root=root,
        intervals=shared.parse_csv(args.intervals, DEFAULT_INTERVALS),
        symbols=shared.parse_csv(args.symbols, []) if args.symbols else None,
        period_days=args.days,
        capital_usdt=args.capital_usdt,
        fee_bps=args.fee_bps,
        slippage_bps=args.slippage_bps,
        max_variants=args.max_variants,
    )
    print(json.dumps({"status": payload.get("status"), "json": str(latest_json_path(root)), "html": str(latest_html_path(root))}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
