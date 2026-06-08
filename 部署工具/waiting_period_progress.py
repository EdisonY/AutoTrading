"""Report-only progress ledger for work that can advance while samples mature.

This tool is deliberately read-only. It reads existing runtime/report artifacts
and writes operator-facing progress reports. It never calls exchange APIs,
submits queue work, changes strategy config, restarts services, or enables
automatic upgrade/rollback/tuning.
"""

from __future__ import annotations

import argparse
import json
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

FORBIDDEN_ACTIONS = [
    "strategy_config_mutation",
    "scanner_restart",
    "release_apply",
    "binance_signed_request",
    "binance_queue_submit",
    "account_snapshot_start",
    "user_stream_start",
    "real_order",
    "real_close_cancel",
    "automatic_upgrade",
    "automatic_rollback",
    "automatic_tuning",
]


def now() -> datetime:
    return datetime.now(CST)


def read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def as_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except Exception:
        return default


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def safe_text(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "/").strip()


def generated_age_seconds(payload: Any) -> float | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get("generated_at") or payload.get("ts")
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST)
        return max(0.0, (now() - dt.astimezone(CST)).total_seconds())
    except Exception:
        return None


def age_label(payload: Any) -> str:
    seconds = generated_age_seconds(payload)
    if seconds is None:
        return "missing"
    if seconds < 90:
        return f"{int(seconds)}s"
    minutes = int(seconds // 60)
    if minutes < 90:
        return f"{minutes}m"
    return f"{int(minutes // 60)}h"


def sample_contract_summary(governance: Any) -> dict[str, Any]:
    contract = governance.get("sample_acceptance_contract") if isinstance(governance, dict) else {}
    if not isinstance(contract, dict):
        contract = {}
    components = contract.get("components") if isinstance(contract.get("components"), list) else []
    rows = []
    for row in components:
        if not isinstance(row, dict):
            continue
        rows.append(
            {
                "name": row.get("name") or "unknown",
                "ready": bool(row.get("ready")),
                "category": row.get("category") or "",
                "status": row.get("status") or "",
                "paired_trades": as_int(row.get("paired_trades")),
                "completed": as_int(row.get("completed")),
                "completion_rate": as_float(row.get("completion_rate")),
                "status_counts": row.get("status_counts") if isinstance(row.get("status_counts"), dict) else {},
                "detail": row.get("detail") or row.get("gap_detail") or "",
            }
        )
    return {
        "status": contract.get("status") or "missing",
        "required_fields": contract.get("required_fields") if isinstance(contract.get("required_fields"), list) else [],
        "blockers": contract.get("blockers") if isinstance(contract.get("blockers"), list) else [],
        "components": rows,
        "acceptance_rule": contract.get("acceptance_rule") or "",
    }


def rollout_72h(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    replay = payload.get("replay_fill_comparison")
    if isinstance(replay, dict) and isinstance(replay.get("72h"), dict):
        return replay.get("72h") or {}
    decision = payload.get("decision") if isinstance(payload.get("decision"), dict) else {}
    return decision.get("replay_fill_comparison_72h") if isinstance(decision.get("replay_fill_comparison_72h"), dict) else {}


def b_v16_context_gap(b_rollout: Any, sample_contract: dict[str, Any]) -> dict[str, Any]:
    replay = rollout_72h(b_rollout)
    counts = replay.get("status_counts") if isinstance(replay.get("status_counts"), dict) else {}
    if not counts:
        for row in sample_contract.get("components") or []:
            if row.get("name") == "b_v16_rollout":
                counts = row.get("status_counts") if isinstance(row.get("status_counts"), dict) else {}
                break
    examples = replay.get("incomplete_examples") if isinstance(replay.get("incomplete_examples"), list) else []
    return {
        "status": "context_gap" if counts else "unknown_or_clear",
        "missing_open": as_int(counts.get("missing_open")),
        "missing_atr": as_int(counts.get("missing_atr")),
        "status_counts": counts,
        "paired_trades": as_int(replay.get("paired_trades")),
        "completed": as_int(replay.get("completed")),
        "completion_rate": as_float(replay.get("completion_rate")),
        "examples": examples[:12],
        "root_cause": (
            "Historical paper/replay rows still lack open pairing or ATR context; "
            "do not mark old rows ready by inference. Future OPEN/CLOSE rows must carry source context."
        ),
    }


def api_disk_guard(alerts: Any, market: Any, micro: Any) -> dict[str, Any]:
    alerts = alerts if isinstance(alerts, dict) else {}
    market = market if isinstance(market, dict) else {}
    micro = micro if isinstance(micro, dict) else {}
    disk = alerts.get("disk") if isinstance(alerts.get("disk"), dict) else {}
    api_rate = alerts.get("api_rate_limits") if isinstance(alerts.get("api_rate_limits"), dict) else {}
    api_guard = alerts.get("api_guard") if isinstance(alerts.get("api_guard"), dict) else {}
    market_age = None
    if market.get("unix_ts"):
        try:
            market_age = max(0.0, datetime.now(timezone.utc).timestamp() - float(market.get("unix_ts")))
        except Exception:
            market_age = None
    micro_age = None
    if micro.get("unix_ts"):
        try:
            micro_age = max(0.0, datetime.now(timezone.utc).timestamp() - float(micro.get("unix_ts")))
        except Exception:
            micro_age = None
    disk_used = as_float(disk.get("used_pct"))
    disk_level = "ok"
    if disk_used >= 85:
        disk_level = "bad"
    elif disk_used >= 75:
        disk_level = "watch"
    api_level = "ok"
    if api_guard.get("in_cooldown") or api_rate.get("ban_until"):
        api_level = "bad"
    elif as_int(api_rate.get("total")) > 0:
        api_level = "watch"
    market_level = "ok" if market_age is not None and market_age <= 180 else "watch"
    micro_level = "ok" if as_int(micro.get("fresh_symbols_240s")) >= 80 else "watch"
    return {
        "status": "ok" if {disk_level, api_level, market_level, micro_level} <= {"ok"} else "watch",
        "disk": {
            "level": disk_level,
            "used_pct": disk_used,
            "free_gb": as_float(disk.get("free_gb")),
            "used_gb": as_float(disk.get("used_gb")),
        },
        "api": {
            "level": api_level,
            "rate_limit_total": as_int(api_rate.get("total")),
            "in_cooldown": bool(api_guard.get("in_cooldown")),
            "rolling_count_60s": as_int(api_guard.get("rolling_count_60s")),
            "public_rolling_count_60s": as_int(api_guard.get("public_rolling_count_60s")),
            "latest": api_rate.get("latest") or "",
            "ban_until": api_rate.get("ban_until") or api_guard.get("banned_until") or "",
        },
        "market": {
            "level": market_level,
            "age_sec": round(market_age, 1) if market_age is not None else None,
            "sources": market.get("sources") if isinstance(market.get("sources"), list) else [],
            "available_symbols": len(market.get("available_symbols") or []),
            "top_symbols": len(market.get("top_symbols") or []),
        },
        "microstructure": {
            "level": micro_level,
            "age_sec": round(micro_age, 1) if micro_age is not None else None,
            "coverage_symbols": as_int(micro.get("coverage_symbols")),
            "fresh_symbols_240s": as_int(micro.get("fresh_symbols_240s")),
            "retention_days": as_int(micro.get("retention_days")),
        },
    }


def calibration_plan_payload() -> dict[str, Any]:
    return {
        "generated_at": now().isoformat(timespec="seconds"),
        "mode": "report_only_plan",
        "status": "plan_only_not_calibrated",
        "approved": False,
        "apply_enabled": False,
        "pairs": 0,
        "min_pairs": 20,
        "thresholds": {
            "median_abs_slippage_bps_max": 10.0,
            "p95_abs_slippage_bps_max": 30.0,
            "median_fill_ratio_min": 0.95,
            "p95_confirmation_lag_sec_max": 15.0,
            "symbol_side_mismatch_allowed": 0,
        },
        "evidence_schema": [
            "signal_id",
            "strategy",
            "symbol",
            "side",
            "requested_qty",
            "paper_executed_qty",
            "real_executed_qty",
            "paper_price",
            "real_avg_price",
            "paper_slippage_bps",
            "real_slippage_bps",
            "fill_ratio_delta",
            "submit_ts",
            "paper_fill_ts",
            "real_fill_ts",
            "confirmation_lag_sec",
            "fee_rate_paper",
            "fee_rate_real",
        ],
        "rule": "This is a calibration plan only. It must not start real trading or mark auto-upgrade ready.",
    }


def policy_status(policy: Any, policy_path: Path) -> dict[str, Any]:
    if not isinstance(policy, dict):
        return {
            "status": "missing",
            "path": str(policy_path),
            "approved": False,
            "automatic_upgrade_enabled": False,
            "detail": "auto-upgrade policy template is missing",
        }
    enabled = bool(policy.get("approved") is True and policy.get("automatic_upgrade_enabled") is True)
    return {
        "status": "enabled" if enabled else "installed_disabled",
        "path": str(policy_path),
        "approved": bool(policy.get("approved") is True),
        "automatic_upgrade_enabled": bool(policy.get("automatic_upgrade_enabled") is True),
        "procedure_version": policy.get("procedure_version") or "",
        "scope": policy.get("scope") or "",
        "detail": "policy exists but automatic upgrade is intentionally disabled" if not enabled else "policy enabled",
    }


def task(
    task_id: str,
    title: str,
    status: str,
    evidence: str,
    next_action: str,
    *,
    level: str = "ok",
    links: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": task_id,
        "title": title,
        "status": status,
        "level": level,
        "evidence": evidence,
        "next_action": next_action,
        "links": links or [],
        "apply_enabled": False,
    }


def build_payload(
    *,
    governance: Any,
    auto_upgrade: Any,
    rollback_execution: Any,
    b_rollout: Any,
    a_rollout: Any,
    alerts: Any,
    market: Any,
    micro: Any,
    policy: Any,
    policy_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    sample_contract = sample_contract_summary(governance)
    b_gap = b_v16_context_gap(b_rollout, sample_contract)
    api_disk = api_disk_guard(alerts, market, micro)
    policy_info = policy_status(policy, policy_path)
    calibration_plan = calibration_plan_payload()
    auto_summary = auto_upgrade.get("summary") if isinstance(auto_upgrade, dict) and isinstance(auto_upgrade.get("summary"), dict) else {}
    rollback_summary = (
        rollback_execution.get("summary")
        if isinstance(rollback_execution, dict) and isinstance(rollback_execution.get("summary"), dict)
        else {}
    )
    required_fields = set(str(v) for v in sample_contract.get("required_fields") or [])
    contract_has_context = {"source_timeframe", "atr", "entry_time", "paper_fill", "close_reason"} <= required_fields
    tasks = [
        task(
            "sample_quality_contract",
            "样本质检闭环",
            "active_report_only" if contract_has_context else "missing_required_fields",
            (
                f"contract={sample_contract.get('status')}; required_fields={len(required_fields)}; "
                f"blockers={len(sample_contract.get('blockers') or [])}"
            ),
            "继续收 fresh contextual OPEN/CLOSE；缺字段样本不得进入自动升级证据。",
            level="watch" if sample_contract.get("status") != "accepted" else "ok",
            links=["reports/strategy_candidate_governance_latest.md"],
        ),
        task(
            "b_v16_context_gap",
            "B/v16 context gap 专项",
            "waiting_fresh_context" if b_gap.get("missing_open") or b_gap.get("missing_atr") else "clear_or_no_gap",
            (
                f"paired={b_gap.get('paired_trades')}; completed={b_gap.get('completed')}; "
                f"missing_open={b_gap.get('missing_open')}; missing_atr={b_gap.get('missing_atr')}"
            ),
            "只修未来采集链路；旧样本不硬补 ATR/open。",
            level="watch" if b_gap.get("missing_open") or b_gap.get("missing_atr") else "ok",
            links=["reports/b_v16_rollout_review_latest.md", "reports/replay_readiness_latest.md"],
        ),
        task(
            "auto_upgrade_policy_template",
            "自动升级 policy 模板",
            policy_info["status"],
            (
                f"approved={policy_info.get('approved')}; "
                f"automatic_upgrade_enabled={policy_info.get('automatic_upgrade_enabled')}; "
                f"version={policy_info.get('procedure_version') or '-'}"
            ),
            "保持 disabled；只有另一个人工审批流程才能改 enabled。",
            level="bad" if policy_info["status"] == "enabled" else "ok" if policy_info["status"] == "installed_disabled" else "watch",
            links=["research_memory/approvals/auto_upgrade_policy.json"],
        ),
        task(
            "rollback_dry_run_preview",
            "半自动回滚 dry-run 预览",
            "built_report_only" if isinstance(rollback_execution, dict) else "missing",
            (
                f"plans={as_int(rollback_summary.get('plans'))}; "
                f"actionable={as_int(rollback_summary.get('actionable_plans'))}; "
                f"apply_enabled={bool(rollback_execution.get('apply_enabled')) if isinstance(rollback_execution, dict) else False}"
            ),
            "继续只出 freeze/证据/dry-run 命令/终止条件；不自动 rollback/apply。",
            level="ok" if isinstance(rollback_execution, dict) and not rollback_execution.get("apply_enabled") else "bad",
            links=["reports/rollback_execution_plan_latest.md"],
        ),
        task(
            "paper_real_calibration_plan",
            "paper-vs-small-real 校准计划",
            "plan_only_not_calibrated",
            (
                f"pairs=0/{calibration_plan['min_pairs']}; "
                f"approved={calibration_plan['approved']}; apply_enabled={calibration_plan['apply_enabled']}"
            ),
            "先保留 schema 和门槛；不启动真实交易，不解除 readiness 的 calibration blocker。",
            level="watch",
            links=["reports/paper_real_calibration_plan_latest.md"],
        ),
        task(
            "operator_report_focus",
            "report 主屏收敛",
            "linked_report_only",
            "新增等待期推进入口；主屏继续显示三策略、涨跌榜、样本成熟度、自动升级差距、API/磁盘。",
            "后续只保留高信号字段，深钻细节放完整 portal/markdown。",
            level="ok",
            links=["reports/waiting_period_progress_latest.md", "reports/index.html"],
        ),
        task(
            "api_disk_guard",
            "API/磁盘守护",
            "watching" if api_disk.get("status") == "watch" else "ok",
            (
                f"disk={api_disk['disk']['used_pct']:.1f}% used/{api_disk['disk']['free_gb']:.1f}GB free; "
                f"api_total={api_disk['api']['rate_limit_total']}; market_age={api_disk['market']['age_sec']}; "
                f"micro_fresh={api_disk['microstructure']['fresh_symbols_240s']}"
            ),
            "继续只读展示；若 API/disk 进入红线，先降频/retention，不先动策略。",
            level="watch" if api_disk.get("status") == "watch" else "ok",
            links=["reports/alerts_latest.md"],
        ),
        task(
            "accidental_apply_guard",
            "防误开安全测试",
            "guarded_by_report_flags",
            (
                f"auto_upgrade_allowed={bool(auto_upgrade.get('automatic_upgrade_allowed')) if isinstance(auto_upgrade, dict) else False}; "
                f"upgrade_apply={bool(auto_upgrade.get('apply_enabled')) if isinstance(auto_upgrade, dict) else False}; "
                f"forbidden_actions={len(FORBIDDEN_ACTIONS)}"
            ),
            "测试必须证明本报告和升级/回滚报告不会启用 apply、Binance queue、user-stream 或订单路径。",
            level="ok",
            links=["tests/test_waiting_period_progress.py"],
        ),
    ]
    bad = [row for row in tasks if row.get("level") == "bad"]
    missing = [row for row in tasks if "missing" in str(row.get("status") or "")]
    status = "safety_violation_report_only" if bad else "implementation_gaps" if missing else "waiting_for_samples_backlog_ready_report_only"
    payload = {
        "generated_at": now().isoformat(timespec="seconds"),
        "mode": "report_only",
        "status": status,
        "automatic_upgrade_allowed": False,
        "automatic_rollback_allowed": False,
        "automatic_tuning_allowed": False,
        "apply_enabled": False,
        "binance_requests_enabled": False,
        "summary": {
            "tasks": len(tasks),
            "ready_or_active": sum(1 for row in tasks if row.get("level") in {"ok", "watch"}),
            "watch": sum(1 for row in tasks if row.get("level") == "watch"),
            "bad": len(bad),
            "missing": len(missing),
            "sample_blockers": as_int(auto_summary.get("sample_blockers")),
            "non_sample_blockers": as_int(auto_summary.get("non_sample_blockers")),
            "b_v16_missing_open": as_int(b_gap.get("missing_open")),
            "b_v16_missing_atr": as_int(b_gap.get("missing_atr")),
        },
        "tasks": tasks,
        "sample_quality": sample_contract,
        "b_v16_context_gap": b_gap,
        "policy": policy_info,
        "paper_real_calibration_plan": calibration_plan,
        "api_disk_guard": api_disk,
        "safety": {
            "forbidden_actions": FORBIDDEN_ACTIONS,
            "rule": "Report-only. No automatic upgrade, rollback, tuning, deploy, restart, order, queue submit, or config mutation.",
        },
    }
    return payload, calibration_plan


def render_calibration_md(payload: dict[str, Any]) -> str:
    lines = [
        "# Paper Vs Small Real Calibration Plan",
        "",
        f"- Generated: `{payload.get('generated_at')}`",
        f"- Status: `{payload.get('status')}`",
        f"- Approved: `{bool(payload.get('approved'))}`",
        f"- Apply enabled: `{bool(payload.get('apply_enabled'))}`",
        f"- Pairs: `{as_int(payload.get('pairs'))}/{as_int(payload.get('min_pairs'))}`",
        "",
        "## Thresholds",
        "",
        "| Metric | Limit |",
        "| --- | ---: |",
    ]
    thresholds = payload.get("thresholds") if isinstance(payload.get("thresholds"), dict) else {}
    for key, value in thresholds.items():
        lines.append(f"| {safe_text(key)} | {safe_text(value)} |")
    lines.extend(["", "## Evidence Schema", ""])
    for field in payload.get("evidence_schema") or []:
        lines.append(f"- `{field}`")
    lines.extend(["", "## Rule", "", f"- {payload.get('rule')}"])
    return "\n".join(lines) + "\n"


def render_md(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    lines = [
        "# Waiting Period Progress",
        "",
        f"- Generated: `{payload.get('generated_at')}`",
        f"- Mode: `{payload.get('mode')}`",
        f"- Status: `{payload.get('status')}`",
        f"- Automatic upgrade allowed: `{bool(payload.get('automatic_upgrade_allowed'))}`",
        f"- Automatic rollback allowed: `{bool(payload.get('automatic_rollback_allowed'))}`",
        f"- Automatic tuning allowed: `{bool(payload.get('automatic_tuning_allowed'))}`",
        f"- Apply enabled: `{bool(payload.get('apply_enabled'))}`",
        f"- Binance requests enabled: `{bool(payload.get('binance_requests_enabled'))}`",
        f"- Tasks: `{as_int(summary.get('ready_or_active'))}/{as_int(summary.get('tasks'))}` active or ready",
        f"- Watch: `{as_int(summary.get('watch'))}` / Bad: `{as_int(summary.get('bad'))}` / Missing: `{as_int(summary.get('missing'))}`",
        "",
        "## Task Ledger",
        "",
        "| ID | Title | Level | Status | Evidence | Next Action | Apply |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in payload.get("tasks") or []:
        lines.append(
            "| {id} | {title} | {level} | {status} | {evidence} | {next} | {apply} |".format(
                id=safe_text(row.get("id")),
                title=safe_text(row.get("title")),
                level=safe_text(row.get("level")),
                status=safe_text(row.get("status")),
                evidence=safe_text(row.get("evidence")),
                next=safe_text(row.get("next_action")),
                apply="yes" if row.get("apply_enabled") else "no",
            )
        )
    sample = payload.get("sample_quality") if isinstance(payload.get("sample_quality"), dict) else {}
    lines.extend(
        [
            "",
            "## Sample Quality",
            "",
            f"- Contract status: `{sample.get('status') or 'missing'}`",
            f"- Required fields: `{', '.join(str(v) for v in (sample.get('required_fields') or []))}`",
            "",
            "| Component | Ready | Category | Paired | Completed | Detail |",
            "| --- | --- | --- | ---: | ---: | --- |",
        ]
    )
    for row in sample.get("components") or []:
        lines.append(
            "| {name} | {ready} | {category} | {paired} | {completed} | {detail} |".format(
                name=safe_text(row.get("name")),
                ready="yes" if row.get("ready") else "no",
                category=safe_text(row.get("category")),
                paired=as_int(row.get("paired_trades")),
                completed=as_int(row.get("completed")),
                detail=safe_text(row.get("detail")),
            )
        )
    b_gap = payload.get("b_v16_context_gap") if isinstance(payload.get("b_v16_context_gap"), dict) else {}
    lines.extend(
        [
            "",
            "## B/v16 Context Gap",
            "",
            f"- Missing open: `{as_int(b_gap.get('missing_open'))}`",
            f"- Missing ATR: `{as_int(b_gap.get('missing_atr'))}`",
            f"- Root cause: {safe_text(b_gap.get('root_cause'))}",
            "",
            "| Status | Symbol | Side | Timeframe | Close TS | Entry Time |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in b_gap.get("examples") or []:
        lines.append(
            "| {status} | {symbol} | {side} | {tf} | {close_ts} | {entry_time} |".format(
                status=safe_text(row.get("status")),
                symbol=safe_text(row.get("symbol")),
                side=safe_text(row.get("side")),
                tf=safe_text(row.get("timeframe")),
                close_ts=safe_text(row.get("close_ts")),
                entry_time=safe_text(row.get("entry_time")),
            )
        )
    if not b_gap.get("examples"):
        lines.append("| - | - | - | - | - | - |")
    api = payload.get("api_disk_guard") if isinstance(payload.get("api_disk_guard"), dict) else {}
    disk = api.get("disk") if isinstance(api.get("disk"), dict) else {}
    api_row = api.get("api") if isinstance(api.get("api"), dict) else {}
    market = api.get("market") if isinstance(api.get("market"), dict) else {}
    micro = api.get("microstructure") if isinstance(api.get("microstructure"), dict) else {}
    lines.extend(
        [
            "",
            "## API / Disk Guard",
            "",
            f"- Disk: `{disk.get('used_pct', 0):.1f}% used`, free `{disk.get('free_gb', 0):.1f}GB`",
            f"- API: level `{api_row.get('level')}`, rate_limit_total `{api_row.get('rate_limit_total')}`, cooldown `{bool(api_row.get('in_cooldown'))}`",
            f"- Market: `{market.get('available_symbols', 0)}` symbols, age `{market.get('age_sec')}` sec",
            f"- Microstructure: fresh `{micro.get('fresh_symbols_240s', 0)}/{micro.get('coverage_symbols', 0)}`",
            "",
            "## Safety",
            "",
        ]
    )
    safety = payload.get("safety") if isinstance(payload.get("safety"), dict) else {}
    lines.append(f"- {safe_text(safety.get('rule'))}")
    for action in safety.get("forbidden_actions") or []:
        lines.append(f"- forbidden: `{action}`")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build waiting-period report-only progress ledger")
    parser.add_argument("--runtime-dir", default=str(ROOT / "runtime"))
    parser.add_argument("--reports-dir", default=str(ROOT / "reports"))
    parser.add_argument("--policy-json", default=str(ROOT / "research_memory" / "approvals" / "auto_upgrade_policy.json"))
    args = parser.parse_args(argv)

    runtime_dir = Path(args.runtime_dir)
    reports_dir = Path(args.reports_dir)
    policy_path = Path(args.policy_json)
    payload, calibration_plan = build_payload(
        governance=read_json(runtime_dir / "strategy_candidate_governance_latest.json"),
        auto_upgrade=read_json(runtime_dir / "auto_upgrade_readiness_latest.json"),
        rollback_execution=read_json(runtime_dir / "rollback_execution_plan_latest.json"),
        b_rollout=read_json(runtime_dir / "b_v16_rollout_review_latest.json"),
        a_rollout=read_json(runtime_dir / "a_v11_rollout_review_latest.json"),
        alerts=read_json(runtime_dir / "alerts_latest.json"),
        market=read_json(runtime_dir / "market_data_cache.json"),
        micro=read_json(runtime_dir / "market_microstructure_latest.json"),
        policy=read_json(policy_path),
        policy_path=policy_path,
    )
    write_text_atomic(runtime_dir / "waiting_period_progress_latest.json", json.dumps(payload, ensure_ascii=False, indent=2))
    write_text_atomic(reports_dir / "waiting_period_progress_latest.md", render_md(payload))
    write_text_atomic(runtime_dir / "paper_real_calibration_plan_latest.json", json.dumps(calibration_plan, ensure_ascii=False, indent=2))
    write_text_atomic(reports_dir / "paper_real_calibration_plan_latest.md", render_calibration_md(calibration_plan))
    print(json.dumps(payload.get("summary") or {}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
