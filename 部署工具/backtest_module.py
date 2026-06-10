"""Backtest module job/spec ledger.

This is the first landing layer for historical backtests. It accepts and
audits frontend job requests, records anti-overfit constraints, and exposes
report-ready status. It does not pretend to calculate strategy PnL before the
A/B/C replay adapters are connected.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from datetime import datetime, timedelta, timezone
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
CST = timezone(timedelta(hours=8))

STRATEGIES = {
    "A": "A/v11",
    "A/V11": "A/v11",
    "A_V11": "A/v11",
    "A/V11": "A/v11",
    "B": "B/v16",
    "B/V16": "B/v16",
    "B_V16": "B/v16",
    "C": "C/v14",
    "C/V14": "C/v14",
    "C_V14": "C/v14",
    "A/v11": "A/v11",
    "B/v16": "B/v16",
    "C/v14": "C/v14",
}
ALLOWED_INTERVALS = {"15m", "30m", "1h", "4h"}
ALLOWED_DIRECTIONS = {"long", "short", "both", "strategy_default"}
SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,30}USDT$")
PARAM_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MAX_SYMBOLS_PER_JOB = 30
MAX_TUNED_PARAMETERS = 3
MAX_PARAMETER_VARIANTS = 24

ANTI_OVERFIT_RULES = [
    "no_single_window_promotion",
    "train_validation_test_split_required",
    "walk_forward_required",
    "out_of_sample_required",
    "pre_registered_parameter_ranges_only",
    "max_three_tuned_parameters",
    "max_twenty_four_parameter_variants",
    "cross_symbol_timeframe_regime_validation_required",
    "minimum_trade_count_required",
    "neighbor_parameter_stability_required",
    "complexity_penalty_required",
    "research_only_no_auto_apply",
]

PARAMETER_RANGES: dict[str, dict[str, tuple[float, float]]] = {
    "A/v11": {
        "entry_threshold": (80.0, 140.0),
        "strong_signal_threshold": (90.0, 160.0),
        "evict_score_gap": (5.0, 60.0),
        "trailing_pullback_atr": (0.2, 2.5),
    },
    "B/v16": {
        "score_threshold": (40.0, 95.0),
        "overheat_cap": (60.0, 100.0),
        "atr_stop_multiplier": (0.5, 5.0),
        "ofi_threshold": (-1.0, 1.0),
    },
    "C/v14": {
        "long_score_threshold": (45.0, 85.0),
        "short_score_threshold": (45.0, 85.0),
        "atr_stop_multiplier": (0.5, 5.0),
        "btc_trend_filter": (0.0, 1.0),
    },
}


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def runtime_dir(root: Path = ROOT) -> Path:
    return root / "runtime"


def reports_dir(root: Path = ROOT) -> Path:
    return root / "reports"


def jobs_dir(root: Path = ROOT) -> Path:
    return runtime_dir(root) / "backtest_jobs"


def latest_json_path(root: Path = ROOT) -> Path:
    return runtime_dir(root) / "backtest_module_latest.json"


def latest_md_path(root: Path = ROOT) -> Path:
    return reports_dir(root) / "backtest_module_latest.md"


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def normalize_strategy(value: Any) -> str:
    raw = str(value or "").strip()
    key = raw.upper().replace("\\", "/")
    return STRATEGIES.get(raw) or STRATEGIES.get(key) or raw


def normalize_symbol(value: Any) -> str:
    return str(value or "").upper().strip().replace("/", "").replace("-", "")


def parse_symbols(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("symbols", payload.get("symbol", "BTCUSDT"))
    if isinstance(raw, str):
        values = [item.strip() for item in raw.split(",") if item.strip()]
    elif isinstance(raw, list):
        values = [str(item).strip() for item in raw if str(item).strip()]
    else:
        values = []
    out: list[str] = []
    for item in values:
        symbol = normalize_symbol(item)
        if symbol and symbol not in out:
            out.append(symbol)
    return out[:MAX_SYMBOLS_PER_JOB]


def finite_float(value: Any, default: float) -> float:
    try:
        result = float(value)
    except Exception:
        return default
    if not math.isfinite(result):
        return default
    return result


def parse_params(raw: Any) -> dict[str, Any]:
    if raw in (None, ""):
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except Exception:
            raise ValueError("params must be a JSON object")
        if isinstance(payload, dict):
            return payload
    raise ValueError("params must be a JSON object")


def validate_params(strategy: str, params: dict[str, Any]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    numeric_keys: list[str] = []
    ranges = PARAMETER_RANGES.get(strategy, {})
    if len(params) > 12:
        errors.append("too_many_parameters:max_12")
    for key, value in sorted(params.items()):
        if not PARAM_RE.match(str(key)):
            errors.append(f"invalid_parameter_name:{key}")
            continue
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            if not math.isfinite(float(value)):
                errors.append(f"non_finite_parameter:{key}")
                continue
            numeric_keys.append(str(key))
            if key in ranges:
                lo, hi = ranges[key]
                if not (lo <= float(value) <= hi):
                    errors.append(f"parameter_out_of_range:{key}:{lo:g}-{hi:g}")
            else:
                warnings.append(f"unregistered_parameter:{key}")
            continue
        if isinstance(value, str) and len(value) <= 80:
            warnings.append(f"non_numeric_parameter_recorded_only:{key}")
            continue
        errors.append(f"unsupported_parameter_value:{key}")
    if len(numeric_keys) > MAX_TUNED_PARAMETERS:
        errors.append(f"too_many_tuned_parameters:max_{MAX_TUNED_PARAMETERS}")
    return errors, warnings


def historical_payload(root: Path = ROOT) -> dict[str, Any]:
    candidates = [
        runtime_dir(root) / "historical_kline_backfill_latest.json",
        root / "server_logs_tencent" / "runtime" / "historical_kline_backfill_latest.json",
    ]
    best: tuple[tuple[float, float, float], dict[str, Any]] | None = None
    for path in candidates:
        payload = read_json(path)
        if not payload:
            continue
        progress = payload.get("progress") if isinstance(payload.get("progress"), dict) else {}
        try:
            mtime = path.stat().st_mtime
        except Exception:
            mtime = 0.0
        rank = (
            finite_float(progress.get("written_rows"), 0.0),
            finite_float(progress.get("percent"), 0.0),
            mtime,
        )
        if best is None or rank > best[0]:
            best = (rank, payload)
    return best[1] if best else {}


def historical_complete(root: Path = ROOT) -> bool:
    payload = historical_payload(root)
    progress = payload.get("progress") if isinstance(payload.get("progress"), dict) else {}
    return str(payload.get("status") or "") == "complete" and int(progress.get("pending_tasks") or 0) == 0


def normalize_job_spec(payload: dict[str, Any], *, root: Path = ROOT) -> tuple[dict[str, Any], list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    strategy = normalize_strategy(payload.get("strategy", "A/v11"))
    if strategy not in {"A/v11", "B/v16", "C/v14"}:
        errors.append("strategy_must_be_A_v11_B_v16_or_C_v14")

    interval = str(payload.get("interval", payload.get("timeframe", "1h")) or "1h").strip()
    if interval not in ALLOWED_INTERVALS:
        errors.append("interval_must_be_15m_30m_1h_or_4h")

    symbols = parse_symbols(payload)
    if not symbols:
        errors.append("symbols_required")
    invalid_symbols = [symbol for symbol in symbols if not SYMBOL_RE.match(symbol)]
    if invalid_symbols:
        errors.append("invalid_symbols:" + ",".join(invalid_symbols[:5]))

    direction = str(payload.get("direction") or "strategy_default").strip().lower()
    if direction not in ALLOWED_DIRECTIONS:
        errors.append("direction_must_be_long_short_both_or_strategy_default")

    period_days = int(finite_float(payload.get("period_days", payload.get("days", 365)), 365.0))
    period_days = max(7, min(365, period_days))
    end_dt = datetime.now(CST)
    start_dt = end_dt - timedelta(days=period_days)

    try:
        params = parse_params(payload.get("params", {}))
    except ValueError as exc:
        params = {}
        errors.append(str(exc))
    param_errors, param_warnings = validate_params(strategy, params)
    errors.extend(param_errors)
    warnings.extend(param_warnings)

    variants = int(finite_float(payload.get("parameter_variants", 1), 1.0))
    if variants > MAX_PARAMETER_VARIANTS:
        errors.append(f"too_many_parameter_variants:max_{MAX_PARAMETER_VARIANTS}")

    capital = max(10.0, finite_float(payload.get("capital_usdt", 10_000.0), 10_000.0))
    fee_bps = max(0.0, min(25.0, finite_float(payload.get("fee_bps", 4.0), 4.0)))
    slippage_bps = max(0.0, min(100.0, finite_float(payload.get("slippage_bps", 0.0), 0.0)))

    hist = historical_payload(root)
    hist_progress = hist.get("progress") if isinstance(hist.get("progress"), dict) else {}
    hist_quality = hist.get("quality") if isinstance(hist.get("quality"), dict) else {}
    if not historical_complete(root):
        warnings.append("historical_baseline_not_complete")

    spec = {
        "strategy": strategy,
        "symbols": symbols,
        "interval": interval,
        "direction": direction,
        "period_days": period_days,
        "start": start_dt.isoformat(timespec="seconds"),
        "end": end_dt.isoformat(timespec="seconds"),
        "capital_usdt": round(capital, 4),
        "fee_bps": round(fee_bps, 4),
        "slippage_bps": round(slippage_bps, 4),
        "fill_model": "paper_fill_model_v2",
        "params": params,
        "parameter_variants": variants,
        "historical_baseline": {
            "status": hist.get("status") or "missing",
            "complete": historical_complete(root),
            "written_rows": int(hist_progress.get("written_rows") or 0),
            "covered_symbol_count": int(hist_quality.get("covered_symbol_count") or 0),
            "covered_symbol_interval_count": int(hist_quality.get("covered_symbol_interval_count") or 0),
            "quality_status": hist_quality.get("status") or "missing",
        },
    }
    return spec, errors, warnings


def job_id_for(spec: dict[str, Any], created_at: str) -> str:
    digest = hashlib.sha256(
        json.dumps({"spec": spec, "created_at": created_at}, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:10]
    stamp = datetime.now(CST).strftime("%Y%m%d-%H%M%S")
    return f"bt-{stamp}-{digest}"


def anti_overfit_payload() -> dict[str, Any]:
    return {
        "enabled": True,
        "rules": list(ANTI_OVERFIT_RULES),
        "max_tuned_parameters": MAX_TUNED_PARAMETERS,
        "max_parameter_variants": MAX_PARAMETER_VARIANTS,
        "auto_apply_allowed": False,
        "automatic_upgrade_allowed": False,
        "recommendation_policy": "research_suggestion_only_until_walk_forward_oos_and_fresh_paper_samples_pass",
    }


def build_pending_result(spec: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    return {
        "status": "replay_adapter_pending",
        "summary": {
            "net_profit_usdt": None,
            "return_pct": None,
            "max_drawdown_pct": None,
            "profit_factor": None,
            "win_rate_pct": None,
            "trades": 0,
            "fees_usdt": None,
            "slippage_usdt": None,
        },
        "charts": {
            "equity_curve": [],
            "drawdown": [],
            "monthly_returns": [],
        },
        "trades": [],
        "benchmark": {
            "status": "pending_historical_query_layer",
            "buy_hold_return_pct": None,
        },
        "recommendation": {
            "action": "no_parameter_change",
            "reason": "strategy_replay_adapter_not_connected",
            "warnings": warnings,
        },
        "next_engine_steps": [
            "historical_store_query",
            "strategy_replay_adapter",
            "backtest_engine_event_loop",
            "tradingview_style_metrics",
            "walk_forward_parameter_comparison",
        ],
        "requested": {
            "strategy": spec["strategy"],
            "symbols": spec["symbols"],
            "interval": spec["interval"],
            "period_days": spec["period_days"],
        },
    }


def create_job(payload: dict[str, Any], *, root: Path = ROOT, user: str = "portal") -> dict[str, Any]:
    spec, errors, warnings = normalize_job_spec(payload, root=root)
    created_at = now_iso()
    if errors:
        return {
            "ok": False,
            "status": "rejected",
            "errors": errors,
            "warnings": warnings,
            "anti_overfit": anti_overfit_payload(),
        }

    jid = job_id_for(spec, created_at)
    job = {
        "ok": True,
        "job_id": jid,
        "created_at": created_at,
        "updated_at": created_at,
        "created_by": user,
        "status": "replay_adapter_pending",
        "execution_state": "accepted_report_only",
        "safety": {
            "binance_requests_enabled": False,
            "strategy_frequency_change": False,
            "live_scanner_impact": "none",
            "auto_apply_allowed": False,
            "paper_or_real_orders": False,
        },
        "spec": spec,
        "warnings": warnings,
        "anti_overfit": anti_overfit_payload(),
        "result": build_pending_result(spec, warnings),
    }
    write_json(jobs_dir(root) / f"{jid}.json", job)
    latest = status_payload(root=root, latest_job=job)
    write_json(latest_json_path(root), latest)
    write_markdown(latest, root=root)
    return job


def load_latest_job(root: Path = ROOT) -> dict[str, Any]:
    latest = read_json(latest_json_path(root))
    job = latest.get("latest_job") if isinstance(latest.get("latest_job"), dict) else {}
    if job:
        return job
    candidates = sorted(jobs_dir(root).glob("bt-*.json"), reverse=True) if jobs_dir(root).exists() else []
    for path in candidates[:1]:
        payload = read_json(path)
        if payload:
            return payload
    return {}


def load_job(job_id: str, *, root: Path = ROOT) -> dict[str, Any]:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "", str(job_id or ""))
    if not safe:
        return {}
    return read_json(jobs_dir(root) / f"{safe}.json")


def status_payload(*, root: Path = ROOT, latest_job: dict[str, Any] | None = None) -> dict[str, Any]:
    hist = historical_payload(root)
    hist_progress = hist.get("progress") if isinstance(hist.get("progress"), dict) else {}
    hist_quality = hist.get("quality") if isinstance(hist.get("quality"), dict) else {}
    job = latest_job if isinstance(latest_job, dict) else load_latest_job(root)
    status = {
        "generated_at": now_iso(),
        "status": "phase1_job_api_ready",
        "module": "historical_backtest",
        "frontend_callable": True,
        "compute_node": "tencent_planned",
        "storage_policy": "historical_warehouse_stays_on_tencent; current_api_is_report_only_control_plane",
        "historical_baseline": {
            "status": hist.get("status") or "missing",
            "complete": historical_complete(root),
            "pending_tasks": int(hist_progress.get("pending_tasks") or 0),
            "percent": finite_float(hist_progress.get("percent"), 0.0),
            "written_rows": int(hist_progress.get("written_rows") or 0),
            "quality_status": hist_quality.get("status") or "missing",
            "covered_symbol_count": int(hist_quality.get("covered_symbol_count") or 0),
            "target_symbol_count": int(hist_quality.get("target_symbol_count") or 0),
            "covered_symbol_interval_count": int(hist_quality.get("covered_symbol_interval_count") or 0),
            "target_symbol_interval_count": int(hist_quality.get("target_symbol_interval_count") or 0),
        },
        "capabilities": {
            "job_submit_api": True,
            "job_status_api": True,
            "parameter_audit": True,
            "anti_overfit_gate": True,
            "historical_store_query": False,
            "strategy_replay_adapter": False,
            "strategy_pnl_metrics": False,
            "parameter_sweep": False,
        },
        "anti_overfit": anti_overfit_payload(),
        "latest_job": job,
        "next_steps": [
            "connect_read_only_historical_store_query",
            "connect_A_B_C_replay_adapters",
            "emit_equity_drawdown_trades_metrics",
            "add_walk_forward_parameter_comparison",
        ],
    }
    return status


def write_markdown(payload: dict[str, Any], *, root: Path = ROOT) -> None:
    job = payload.get("latest_job") if isinstance(payload.get("latest_job"), dict) else {}
    hist = payload.get("historical_baseline") if isinstance(payload.get("historical_baseline"), dict) else {}
    caps = payload.get("capabilities") if isinstance(payload.get("capabilities"), dict) else {}
    lines = [
        "# 历史回测模块报告",
        "",
        f"- 更新时间: `{payload.get('generated_at', '-')}`",
        f"- 模块状态: `{payload.get('status', '-')}`",
        f"- 历史基线: `{hist.get('status', '-')}` / `{hist.get('percent', 0):.2f}%` / `{hist.get('written_rows', 0)}` 行",
        f"- 覆盖: `{hist.get('covered_symbol_count', 0)}/{hist.get('target_symbol_count', 0)}` 币，`{hist.get('covered_symbol_interval_count', 0)}/{hist.get('target_symbol_interval_count', 0)}` 币种周期",
        "",
        "## 已落地",
        "",
        f"- 前端提交 API: `{caps.get('job_submit_api')}`",
        f"- 参数审计: `{caps.get('parameter_audit')}`",
        f"- 反拟合门控: `{caps.get('anti_overfit_gate')}`",
        "",
        "## 未完成",
        "",
        f"- 历史仓查询层: `{caps.get('historical_store_query')}`",
        f"- A/B/C 策略 replay adapter: `{caps.get('strategy_replay_adapter')}`",
        f"- 策略 PnL 指标: `{caps.get('strategy_pnl_metrics')}`",
        f"- 参数复测/调优: `{caps.get('parameter_sweep')}`",
        "",
        "## 最近任务",
        "",
    ]
    if job:
        spec = job.get("spec") if isinstance(job.get("spec"), dict) else {}
        lines.extend(
            [
                f"- job_id: `{job.get('job_id', '-')}`",
                f"- 状态: `{job.get('status', '-')}`",
                f"- 策略/币种/周期: `{spec.get('strategy', '-')}` / `{','.join(spec.get('symbols') or [])}` / `{spec.get('interval', '-')}`",
                f"- 结果: `{((job.get('result') or {}).get('status') if isinstance(job.get('result'), dict) else '-')}`",
            ]
        )
    else:
        lines.append("- 暂无前端提交任务。")
    lines.extend(
        [
            "",
            "## 反拟合硬规则",
            "",
            "- 不按单一窗口晋级。",
            "- 必须 train/validation/test 与 walk-forward。",
            "- 必须 out-of-sample。",
            "- 参数范围预注册，最多 3 个可调参数，最多 24 组变体。",
            "- 跨币种、周期、行情阶段验证。",
            "- 回测建议只作为研究证据，不自动改策略。",
        ]
    )
    latest_md_path(root).parent.mkdir(parents=True, exist_ok=True)
    latest_md_path(root).write_text("\n".join(lines) + "\n", encoding="utf-8")


def refresh_status_files(*, root: Path = ROOT) -> dict[str, Any]:
    payload = status_payload(root=root)
    write_json(latest_json_path(root), payload)
    write_markdown(payload, root=root)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backtest module status/job ledger")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--create-job-json", default="")
    args = parser.parse_args(argv)
    if args.create_job_json:
        payload = json.loads(args.create_job_json)
        result = create_job(payload, root=args.root, user="cli")
    else:
        result = refresh_status_files(root=args.root)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
