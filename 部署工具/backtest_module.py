"""Backtest module job/spec ledger.

Accepts frontend historical backtest jobs, audits anti-overfit constraints, and
runs a read-only research adapter against the historical Kline warehouse. The
adapter returns real computed metrics, but it is not byte-for-byte live scanner
parity and never applies strategy changes automatically.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import subprocess
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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
CST = timezone(timedelta(hours=8))

import backtest_engine

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
REMOTE_DELEGATE_TIMEOUT_SEC = 900

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

PARAMETER_LABELS: dict[str, dict[str, str]] = {
    "A/v11": {
        "entry_threshold": "入场分数阈值",
        "strong_signal_threshold": "强信号阈值",
        "evict_score_gap": "替换弱仓分差",
        "trailing_pullback_atr": "ATR 回撤止盈",
    },
    "B/v16": {
        "score_threshold": "入场分数阈值",
        "overheat_cap": "过热上限",
        "atr_stop_multiplier": "ATR 止损倍数",
        "ofi_threshold": "OFI 门槛",
    },
    "C/v14": {
        "long_score_threshold": "多头分数阈值",
        "short_score_threshold": "空头分数阈值",
        "atr_stop_multiplier": "ATR 止损倍数",
        "btc_trend_filter": "BTC 趋势过滤",
    },
}

PARAMETER_NOTES: dict[str, dict[str, str]] = {
    "A/v11": {
        "entry_threshold": "越高越少开仓，过滤更严。",
        "strong_signal_threshold": "越高越难触发强信号和替换逻辑。",
        "evict_score_gap": "越高越难用新信号替换旧仓。",
        "trailing_pullback_atr": "越高越能容忍回撤，出场更慢。",
    },
    "B/v16": {
        "score_threshold": "越高越少开仓，过滤更严。",
        "overheat_cap": "越低越容易挡住过热行情。",
        "atr_stop_multiplier": "越高止损更宽，单笔波动更大。",
        "ofi_threshold": "订单流门槛；负值更宽松，正值更严格。",
    },
    "C/v14": {
        "long_score_threshold": "越高多头入场更少。",
        "short_score_threshold": "越高空头入场更少。",
        "atr_stop_multiplier": "越高止损更宽，单笔波动更大。",
        "btc_trend_filter": "0 关闭，1 开启；用于过滤 BTC 大方向冲突。",
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


def local_historical_store_available(root: Path = ROOT) -> bool:
    table = root / "research_store" / "historical_klines"
    return table.exists() and any(table.glob("date=*/data.jsonl"))


def should_delegate_to_tencent(root: Path = ROOT) -> bool:
    if os.environ.get("BACKTEST_DISABLE_REMOTE_DELEGATE", "").strip() in {"1", "true", "TRUE"}:
        return False
    if os.environ.get("BACKTEST_REMOTE_DELEGATE", "").strip() in {"1", "true", "TRUE"}:
        return True
    return str(root).rstrip("/") == "/opt/crypto-shadow-lab"


def remote_delegate_job(payload: dict[str, Any], *, root: Path = ROOT, user: str = "portal") -> dict[str, Any]:
    host = os.environ.get("TENCENT_HOST", "129.226.151.144")
    remote_user = os.environ.get("TENCENT_USER", "ubuntu")
    remote_root = os.environ.get("TENCENT_ROOT", "/opt/crypto-auto-trader")
    encoded = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("ascii")
    remote_cmd = (
        f"cd {shell_quote(remote_root)} && "
        "PY=.venv/bin/python; [ -x \"$PY\" ] || PY=$(command -v python3); "
        f"\"$PY\" backtest_module.py --no-remote-delegate --user {shell_quote(user)} --create-job-json-b64 {shell_quote(encoded)}"
    )
    cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=10",
        "-o",
        "ServerAliveCountMax=2",
        f"{remote_user}@{host}",
        remote_cmd,
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=REMOTE_DELEGATE_TIMEOUT_SEC)
    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        return {
            "ok": False,
            "status": "remote_delegate_failed",
            "errors": [f"tencent_delegate_failed:{(stderr or stdout)[-400:]}"],
            "anti_overfit": anti_overfit_payload(),
        }
    try:
        result = json.loads(stdout)
    except Exception:
        return {
            "ok": False,
            "status": "remote_delegate_bad_response",
            "errors": ["tencent_delegate_bad_json"],
            "raw": stdout[-400:],
            "anti_overfit": anti_overfit_payload(),
        }
    if isinstance(result, dict) and result.get("ok"):
        job_id = str(result.get("job_id") or "")
        if job_id:
            write_json(jobs_dir(root) / f"{job_id}.json", result)
        status = status_payload(root=root, latest_job=result)
        write_json(latest_json_path(root), status)
        write_markdown(status, root=root)
    return result if isinstance(result, dict) else {"ok": False, "status": "remote_delegate_bad_response"}


def shell_quote(value: str) -> str:
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


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


def parameter_schema() -> dict[str, Any]:
    strategies: dict[str, Any] = {}
    for strategy, ranges in PARAMETER_RANGES.items():
        labels = PARAMETER_LABELS.get(strategy, {})
        notes = PARAMETER_NOTES.get(strategy, {})
        rows = []
        for key, (lo, hi) in ranges.items():
            span = abs(hi - lo)
            step = 1.0 if span >= 10 else 0.1 if span >= 2 else 0.01
            rows.append(
                {
                    "key": key,
                    "label": labels.get(key, key),
                    "min": lo,
                    "max": hi,
                    "step": step,
                    "note": notes.get(key, ""),
                }
            )
        strategies[strategy] = rows
    return {
        "max_tuned_parameters": MAX_TUNED_PARAMETERS,
        "max_parameter_variants": MAX_PARAMETER_VARIANTS,
        "strategies": strategies,
    }


def compact_job_summary(job: dict[str, Any]) -> dict[str, Any]:
    spec = job.get("spec") if isinstance(job.get("spec"), dict) else {}
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    sweep = result.get("parameter_sweep") if isinstance(result.get("parameter_sweep"), dict) else {}
    review = sweep.get("anti_overfit_review") if isinstance(sweep.get("anti_overfit_review"), dict) else {}
    recommendation = result.get("recommendation") if isinstance(result.get("recommendation"), dict) else {}
    return {
        "job_id": job.get("job_id") or "",
        "created_at": job.get("created_at") or "",
        "updated_at": job.get("updated_at") or "",
        "created_by": job.get("created_by") or "",
        "status": job.get("status") or result.get("status") or "",
        "execution_state": job.get("execution_state") or "",
        "strategy": spec.get("strategy") or "",
        "symbols": spec.get("symbols") if isinstance(spec.get("symbols"), list) else [],
        "interval": spec.get("interval") or "",
        "period_days": spec.get("period_days") or 0,
        "net_profit_usdt": summary.get("net_profit_usdt", 0.0),
        "return_pct": summary.get("return_pct", 0.0),
        "max_drawdown_pct": summary.get("max_drawdown_pct", 0.0),
        "profit_factor": summary.get("profit_factor", 0.0),
        "win_rate_pct": summary.get("win_rate_pct", 0.0),
        "trades": summary.get("trades", 0),
        "recommendation_action": recommendation.get("action") or "",
        "recommendation_reason": recommendation.get("reason") or "",
        "anti_overfit_status": review.get("status") or "",
        "engine_parity": result.get("engine_parity") or "research_adapter",
    }


def list_jobs(*, root: Path = ROOT, limit: int = 20, latest_job: dict[str, Any] | None = None) -> dict[str, Any]:
    directory = jobs_dir(root)
    paths = sorted(directory.glob("bt-*.json"), reverse=True) if directory.exists() else []
    rows: list[dict[str, Any]] = []
    for path in paths[: max(0, limit)]:
        payload = read_json(path)
        if payload:
            rows.append(compact_job_summary(payload))
    latest_id = str((latest_job or {}).get("job_id") or "")
    if latest_id and not any(str(row.get("job_id") or "") == latest_id for row in rows):
        rows.insert(0, compact_job_summary(latest_job or {}))
    return {
        "storage": "runtime/backtest_jobs/*.json",
        "full_history_retained": True,
        "display_limit": max(0, limit),
        "total_jobs": max(len(paths), len(rows)),
        "jobs": rows,
    }


def execute_job_result(spec: dict[str, Any], warnings: list[str], *, root: Path = ROOT) -> dict[str, Any]:
    try:
        result = backtest_engine.run_backtest(spec, root=root)
    except Exception as exc:
        return {
            "status": "engine_error",
            "summary": {
                "net_profit_usdt": 0.0,
                "return_pct": 0.0,
                "max_drawdown_pct": 0.0,
                "profit_factor": 0.0,
                "win_rate_pct": 0.0,
                "trades": 0,
                "fees_usdt": 0.0,
                "slippage_usdt": 0.0,
            },
            "charts": {"equity_curve": [], "drawdown": [], "monthly_returns": []},
            "trades": [],
            "benchmark": {"status": "error", "buy_hold_return_pct": None},
            "coverage": [],
            "recommendation": {
                "action": "no_parameter_change",
                "reason": f"engine_error:{str(exc)[:160]}",
                "warnings": warnings,
                "auto_apply_allowed": False,
                "automatic_upgrade_allowed": False,
            },
            "parameter_sweep": {"enabled": False, "variants": [], "anti_overfit_review": {"status": "engine_error"}},
            "safety": backtest_engine.safety_payload(),
        }
    recommendation = result.get("recommendation") if isinstance(result.get("recommendation"), dict) else {}
    recommendation["warnings"] = warnings
    result["recommendation"] = recommendation
    return result


def create_job(payload: dict[str, Any], *, root: Path = ROOT, user: str = "portal") -> dict[str, Any]:
    if should_delegate_to_tencent(root):
        return remote_delegate_job(payload, root=root, user=user)

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
    result = execute_job_result(spec, warnings, root=root)
    job_status = str(result.get("status") or "completed")
    job = {
        "ok": True,
        "job_id": jid,
        "created_at": created_at,
        "updated_at": created_at,
        "created_by": user,
        "status": job_status,
        "execution_state": "completed_research_only" if job_status == "completed" else job_status,
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
        "result": result,
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
        "status": "backtest_engine_ready",
        "module": "historical_backtest",
        "frontend_callable": True,
        "compute_node": "tencent_or_local_read_only",
        "storage_policy": "historical_warehouse_stays_on_tencent; engine_reads_local_or_tencent_warehouse_only",
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
            "historical_store_query": True,
            "strategy_replay_adapter": True,
            "strategy_pnl_metrics": True,
            "parameter_sweep": True,
        },
        "anti_overfit": anti_overfit_payload(),
        "parameter_schema": parameter_schema(),
        "latest_job": job,
        "recent_jobs": list_jobs(root=root, limit=20, latest_job=job),
        "next_steps": [
            "harden_live_scanner_byte_for_byte_parity",
            "add_async_tencent_job_queue_for_large_runs",
            "add_depth_snapshot_replay_when_available",
            "feed_research_results_into_governance_without_auto_apply",
        ],
    }
    return status


def write_markdown(payload: dict[str, Any], *, root: Path = ROOT) -> None:
    job = payload.get("latest_job") if isinstance(payload.get("latest_job"), dict) else {}
    hist = payload.get("historical_baseline") if isinstance(payload.get("historical_baseline"), dict) else {}
    caps = payload.get("capabilities") if isinstance(payload.get("capabilities"), dict) else {}
    recent = payload.get("recent_jobs") if isinstance(payload.get("recent_jobs"), dict) else {}
    schema = payload.get("parameter_schema") if isinstance(payload.get("parameter_schema"), dict) else parameter_schema()

    def md_value(value: Any, digits: int = 2, signed: bool = False) -> str:
        try:
            number = float(value)
        except Exception:
            return "-"
        prefix = "+" if signed and number > 0 else ""
        text = f"{prefix}{number:.{digits}f}"
        return text.rstrip("0").rstrip(".")

    def job_conclusion(summary: dict[str, Any], review: dict[str, Any]) -> tuple[str, str]:
        trades = int(summary.get("trades") or 0)
        net = finite_float(summary.get("net_profit_usdt"), 0.0)
        pf = finite_float(summary.get("profit_factor"), 0.0)
        dd = finite_float(summary.get("max_drawdown_pct"), 0.0)
        if trades < 30:
            return "样本偏少", "扩大币种/周期后再判断；不能用于升级。"
        if net > 0 and pf >= 1.1 and dd <= 20 and review.get("status") == "passed_research_only":
            return "可进入人工研究复核", "仍需 fresh paper 样本、跨周期稳定性和人工审批。"
        if net > 0:
            return "全窗口盈利但门控未过", "重点检查 OOS、邻近参数稳定性和跨币种表现。"
        return "当前不建议应用", "先定位亏损来源，不要用单窗口参数搜索硬拟合。"

    lines = [
        "# 历史回测模块报告",
        "",
        f"- 更新时间: `{payload.get('generated_at', '-')}`",
        f"- 模块状态: `{payload.get('status', '-')}`",
        f"- 历史基线: `{hist.get('status', '-')}` / `{hist.get('percent', 0):.2f}%` / `{hist.get('written_rows', 0)}` 行",
        f"- 覆盖: `{hist.get('covered_symbol_count', 0)}/{hist.get('target_symbol_count', 0)}` 币，`{hist.get('covered_symbol_interval_count', 0)}/{hist.get('target_symbol_interval_count', 0)}` 币种周期",
        f"- 任务历史: 保留在 `runtime/backtest_jobs/*.json`；报告默认展示最近 `{((payload.get('recent_jobs') or {}).get('display_limit') if isinstance(payload.get('recent_jobs'), dict) else 20)}` 条",
        "",
        "## 已落地",
        "",
        f"- 前端提交 API: `{caps.get('job_submit_api')}`",
        f"- 参数审计: `{caps.get('parameter_audit')}`",
        f"- 反拟合门控: `{caps.get('anti_overfit_gate')}`",
        f"- 历史仓查询层: `{caps.get('historical_store_query')}`",
        f"- A/B/C 策略 replay adapter: `{caps.get('strategy_replay_adapter')}`",
        f"- 策略 PnL 指标: `{caps.get('strategy_pnl_metrics')}`",
        f"- 参数复测/调优: `{caps.get('parameter_sweep')}`",
        f"- 计算口径: `{((job.get('result') or {}).get('engine_parity') if isinstance(job.get('result'), dict) else 'research_adapter') or 'research_adapter'}`",
        "",
        "## 仍需强化",
        "",
        "- live scanner byte-for-byte parity 仍需独立验收。",
        "- 大任务异步队列、深度盘口 replay、治理证据自动汇总仍需继续强化。",
        "- 回测建议只作为研究证据，不自动调参、升级或回滚。",
        "",
        "## 最近任务",
        "",
    ]
    if job:
        spec = job.get("spec") if isinstance(job.get("spec"), dict) else {}
        result = job.get("result") if isinstance(job.get("result"), dict) else {}
        summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
        sweep = result.get("parameter_sweep") if isinstance(result.get("parameter_sweep"), dict) else {}
        review = sweep.get("anti_overfit_review") if isinstance(sweep.get("anti_overfit_review"), dict) else {}
        recommendation = result.get("recommendation") if isinstance(result.get("recommendation"), dict) else {}
        benchmark = result.get("benchmark") if isinstance(result.get("benchmark"), dict) else {}
        charts = result.get("charts") if isinstance(result.get("charts"), dict) else {}
        equity = charts.get("equity_curve") if isinstance(charts.get("equity_curve"), list) else []
        trades = result.get("trades") if isinstance(result.get("trades"), list) else []
        conclusion, advice = job_conclusion(summary, review)
        lines.extend(
            [
                f"- job_id: `{job.get('job_id', '-')}`",
                f"- 状态: `{job.get('status', '-')}`",
                f"- 策略/币种/周期: `{spec.get('strategy', '-')}` / `{','.join(spec.get('symbols') or [])}` / `{spec.get('interval', '-')}`",
                f"- 结果: `{result.get('status', '-')}` / 净收益 `{summary.get('net_profit_usdt', 0)}` USDT / 交易 `{summary.get('trades', 0)}` / 回撤 `{summary.get('max_drawdown_pct', 0)}`%",
                f"- 建议: `{recommendation.get('action', '-')}` / `{recommendation.get('reason', '-')}`",
                f"- 反拟合/OOS: `{review.get('status', '-')}` / auto_apply `{review.get('auto_apply_allowed', False)}`",
                "",
                "### 总结与建议",
                "",
                f"- 结论: `{conclusion}`",
                f"- 建议: {advice}",
                f"- 收益/风险: 净收益 `{md_value(summary.get('net_profit_usdt'), 2, True)}` USDT；收益率 `{md_value(summary.get('return_pct'), 2, True)}%`；最大回撤 `{md_value(summary.get('max_drawdown_pct'), 2)}%`；PF `{md_value(summary.get('profit_factor'), 2)}`；胜率 `{md_value(summary.get('win_rate_pct'), 2)}%`。",
                f"- 基准: 买入持有平均 `{md_value(benchmark.get('buy_hold_return_pct'), 2, True)}%`。",
            ]
        )
        if equity:
            first = equity[0] if isinstance(equity[0], dict) else {}
            last = equity[-1] if isinstance(equity[-1], dict) else {}
            lines.extend(
                [
                    "",
                    "### 权益 / PnL 曲线摘要",
                    "",
                    f"- 起点权益: `{md_value(first.get('equity'), 2)}`",
                    f"- 终点权益: `{md_value(last.get('equity'), 2)}`",
                    f"- 曲线点数: `{len(equity)}`",
                ]
            )
        if trades:
            lines.extend(
                [
                    "",
                    "### 最近开平仓记录",
                    "",
                    "| 标的 | 方向 | 开仓时间 | 开仓价 | 平仓时间 | 平仓价 | 净盈亏 | 出场原因 |",
                    "| --- | --- | --- | ---: | --- | ---: | ---: | --- |",
                ]
            )
            for trade in trades[-40:]:
                if not isinstance(trade, dict):
                    continue
                lines.append(
                    "| {symbol} | {side} | {entry_ts} | {entry_price} | {exit_ts} | {exit_price} | {pnl} | {reason} |".format(
                        symbol=str(trade.get("symbol") or "-"),
                        side=str(trade.get("side") or "-"),
                        entry_ts=str(trade.get("entry_ts") or "-"),
                        entry_price=md_value(trade.get("entry_price"), 8),
                        exit_ts=str(trade.get("exit_ts") or "-"),
                        exit_price=md_value(trade.get("exit_price"), 8),
                        pnl=md_value(trade.get("net_pnl_usdt"), 4, True),
                        reason=str(trade.get("exit_reason") or "-"),
                    )
                )
    else:
        lines.append("- 暂无前端提交任务。")
    lines.extend(
        [
            "",
            "## 历史任务保留",
            "",
            f"- 任务文件: `{recent.get('storage', 'runtime/backtest_jobs/*.json')}`",
            f"- 全量历史保留: `{recent.get('full_history_retained', True)}`",
            f"- 报告展示最近: `{recent.get('display_limit', 20)}` 条",
            f"- 当前任务总数: `{recent.get('total_jobs', 0)}`",
        ]
    )
    recent_jobs = recent.get("jobs") if isinstance(recent.get("jobs"), list) else []
    if recent_jobs:
        lines.extend(
            [
                "",
                "| job_id | 时间 | 策略 | 周期 | 状态 | 净收益 | 交易 | OOS |",
                "| --- | --- | --- | --- | --- | ---: | ---: | --- |",
            ]
        )
        for row in recent_jobs[:20]:
            if not isinstance(row, dict):
                continue
            lines.append(
                "| {job_id} | {created_at} | {strategy} | {interval} | {status} | {net} | {trades} | {oos} |".format(
                    job_id=str(row.get("job_id") or "-"),
                    created_at=str(row.get("created_at") or "-"),
                    strategy=str(row.get("strategy") or "-"),
                    interval=str(row.get("interval") or "-"),
                    status=str(row.get("status") or "-"),
                    net=md_value(row.get("net_profit_usdt"), 2, True),
                    trades=str(row.get("trades") or 0),
                    oos=str(row.get("anti_overfit_status") or "-"),
                )
            )
    lines.extend(["", "## 可调参数接口", ""])
    for strategy, rows in (schema.get("strategies") if isinstance(schema.get("strategies"), dict) else {}).items():
        lines.extend([f"### {strategy}", "", "| 参数 | 中文名 | 范围 | 步长 | 说明 |", "| --- | --- | --- | ---: | --- |"])
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            lines.append(
                "| {key} | {label} | {lo} - {hi} | {step} | {note} |".format(
                    key=str(row.get("key") or "-"),
                    label=str(row.get("label") or "-"),
                    lo=md_value(row.get("min"), 4),
                    hi=md_value(row.get("max"), 4),
                    step=md_value(row.get("step"), 4),
                    note=str(row.get("note") or "-"),
                )
            )
        lines.append("")
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
    parser.add_argument("--create-job-json-b64", default="")
    parser.add_argument("--user", default="cli")
    parser.add_argument("--no-remote-delegate", action="store_true")
    args = parser.parse_args(argv)
    if args.no_remote_delegate:
        os.environ["BACKTEST_DISABLE_REMOTE_DELEGATE"] = "1"
    if args.create_job_json or args.create_job_json_b64:
        if args.create_job_json_b64:
            payload = json.loads(base64.b64decode(args.create_job_json_b64).decode("utf-8"))
        else:
            payload = json.loads(args.create_job_json)
        result = create_job(payload, root=args.root, user=args.user)
    else:
        result = refresh_status_files(root=args.root)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
