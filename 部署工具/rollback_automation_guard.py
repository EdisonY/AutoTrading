"""Report-only guard for any future automatic rollback procedure.

This tool never executes rollback, deploy, service restart, order, or config
mutation. It only states whether future automation is still blocked and why.
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

REQUIRED_POLICY_FIELDS = ("approved", "automatic_rollback_enabled", "approved_by", "approved_at", "scope", "procedure_version")
ACTIONABLE_PLAN_STATUSES = {
    "manual_rollback_ready",
    "rollback_review_ready",
    "narrow_or_pause_ready",
    "operator_requested_narrow_dry_run",
    "operator_requested_rollback_dry_run",
}


def read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def as_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except Exception:
        return default


def is_placeholder_command(command: Any) -> bool:
    text = str(command or "")
    return "<release-id" in text or "<" in text and "release" in text.lower()


def policy_condition(policy: Any, policy_path: Path) -> dict[str, Any]:
    if not isinstance(policy, dict):
        return {
            "name": "explicit_policy_approval",
            "ready": False,
            "status": "missing",
            "detail": f"missing {policy_path}",
            "blockers": ["explicit_rollback_automation_policy_missing"],
        }
    missing = [field for field in REQUIRED_POLICY_FIELDS if policy.get(field) in (None, "", [], {})]
    enabled = bool(policy.get("approved") is True and policy.get("automatic_rollback_enabled") is True)
    blockers = [f"missing_policy_field:{field}" for field in missing]
    if not enabled:
        blockers.append("policy_not_approved_or_not_enabled")
    return {
        "name": "explicit_policy_approval",
        "ready": enabled and not missing,
        "status": "approved" if enabled and not missing else "incomplete",
        "detail": "explicit automatic rollback policy present" if enabled and not missing else "policy exists but is incomplete or disabled",
        "blockers": blockers,
        "policy": {
            "approved_by": policy.get("approved_by"),
            "approved_at": policy.get("approved_at"),
            "scope": policy.get("scope"),
            "procedure_version": policy.get("procedure_version"),
        },
    }


def replay_condition(replay: Any) -> dict[str, Any]:
    status = replay.get("status") if isinstance(replay, dict) else "missing"
    summary = replay.get("summary") if isinstance(replay, dict) and isinstance(replay.get("summary"), dict) else {}
    ready = status == "ready_for_operator_review"
    return {
        "name": "replay_readiness",
        "ready": ready,
        "status": status or "missing",
        "detail": "replay/fill evidence ready" if ready else f"replay readiness is {status or 'missing'}",
        "blockers": [] if ready else [f"replay_readiness:{status or 'missing'}"],
        "blocker_count": as_int(summary.get("blockers")),
    }


def execution_condition(execution: Any) -> dict[str, Any]:
    if not isinstance(execution, dict):
        return {
            "name": "rollback_execution_plan",
            "ready": False,
            "status": "missing",
            "detail": "rollback execution plan missing",
            "blockers": ["rollback_execution_plan_missing"],
        }
    summary = execution.get("summary") if isinstance(execution.get("summary"), dict) else {}
    actionable = as_int(summary.get("actionable_plans"))
    status = str(execution.get("status") or "missing")
    ready = status in {"ready_for_dry_run_review", "operator_requested_dry_run_review"} and actionable > 0
    blockers: list[str] = []
    if actionable <= 0:
        blockers.append("no_actionable_dry_run_plan")
    if status not in {"ready_for_dry_run_review", "operator_requested_dry_run_review"}:
        blockers.append(f"rollback_execution_status:{status}")
    return {
        "name": "rollback_execution_plan",
        "ready": ready,
        "status": status,
        "detail": f"actionable dry-run plans {actionable}",
        "blockers": blockers,
        "actionable_plans": actionable,
    }


def release_id_condition(plans: list[Any]) -> dict[str, Any]:
    actionable = [
        plan
        for plan in plans
        if isinstance(plan, dict)
        and plan.get("execution_status") in ACTIONABLE_PLAN_STATUSES
    ]
    unresolved = []
    for plan in actionable:
        commands = list(plan.get("dry_run_commands") or []) + list(plan.get("apply_commands_disabled") or [])
        if not plan.get("reviewed_release_id") or any(is_placeholder_command(command) for command in commands):
            unresolved.append(plan.get("candidate_id") or plan.get("plan_id") or "unknown")
    ready = bool(actionable) and not unresolved
    return {
        "name": "reviewed_release_id",
        "ready": ready,
        "status": "ready" if ready else "unresolved",
        "detail": "all actionable plans have reviewed release ids" if ready else "one or more actionable plans still use placeholder release ids",
        "blockers": [f"release_id_unresolved:{item}" for item in unresolved],
        "actionable_plans": len(actionable),
        "unresolved": unresolved,
    }


def build_candidate(plan: dict[str, Any], conditions: list[dict[str, Any]]) -> dict[str, Any]:
    blockers = [blocker for condition in conditions for blocker in (condition.get("blockers") or [])]
    if plan.get("execution_status") not in ACTIONABLE_PLAN_STATUSES:
        blockers.append("plan_not_actionable")
    return {
        "candidate_id": plan.get("candidate_id") or "",
        "strategy": plan.get("strategy") or "",
        "priority": plan.get("priority") or "",
        "execution_status": plan.get("execution_status") or "",
        "component": plan.get("component") or "",
        "automation_status": "blocked" if blockers else "preconditions_met_report_only",
        "automatic_rollback_allowed": False,
        "apply_enabled": False,
        "blockers": sorted(set(str(item) for item in blockers)),
    }


def build_payload(
    *,
    rollback_watch: Any,
    rollback_execution: Any,
    replay_readiness: Any,
    policy: Any,
    policy_path: Path,
) -> dict[str, Any]:
    plans = rollback_execution.get("plans") if isinstance(rollback_execution, dict) and isinstance(rollback_execution.get("plans"), list) else []
    conditions = [
        policy_condition(policy, policy_path),
        replay_condition(replay_readiness),
        execution_condition(rollback_execution),
        release_id_condition(plans),
        {
            "name": "apply_disabled",
            "ready": True,
            "status": "enforced",
            "detail": "this guard is report-only and cannot apply rollback",
            "blockers": [],
        },
    ]
    candidates = [build_candidate(plan, conditions) for plan in plans if isinstance(plan, dict)]
    condition_blockers = [blocker for condition in conditions for blocker in (condition.get("blockers") or [])]
    candidate_blockers = [blocker for candidate in candidates for blocker in (candidate.get("blockers") or []) if blocker == "plan_not_actionable"]
    blockers = condition_blockers + candidate_blockers
    ready_conditions = sum(1 for condition in conditions if condition.get("ready"))
    all_preconditions = not blockers and bool(candidates)
    status = "preconditions_met_report_only" if all_preconditions else "blocked"
    watch_summary = rollback_watch.get("summary") if isinstance(rollback_watch, dict) and isinstance(rollback_watch.get("summary"), dict) else {}
    return {
        "generated_at": datetime.now(CST).isoformat(timespec="seconds"),
        "mode": "report_only",
        "status": status,
        "automatic_rollback_allowed": False,
        "apply_enabled": False,
        "preconditions_met": all_preconditions,
        "summary": {
            "conditions": len(conditions),
            "ready_conditions": ready_conditions,
            "blockers": len(blockers),
            "candidates": len(candidates),
            "blocked_candidates": sum(1 for item in candidates if item.get("automation_status") == "blocked"),
            "operator_ready_watch_items": as_int(watch_summary.get("operator_ready")),
        },
        "conditions": conditions,
        "blockers": sorted(set(str(item) for item in blockers)),
        "candidates": candidates,
        "rule": "No automatic rollback, deploy, service restart, order, or config mutation may run from this report.",
    }


def render_md(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    lines = [
        "# Rollback Automation Guard",
        "",
        f"- Generated: `{payload.get('generated_at')}`",
        f"- Status: `{payload.get('status')}`",
        f"- Mode: `{payload.get('mode')}`",
        f"- Automatic rollback allowed: `{bool(payload.get('automatic_rollback_allowed'))}`",
        f"- Apply enabled: `{bool(payload.get('apply_enabled'))}`",
        f"- Conditions ready: `{as_int(summary.get('ready_conditions'))}/{as_int(summary.get('conditions'))}`",
        f"- Blockers: `{as_int(summary.get('blockers'))}`",
        "",
        "## Conditions",
        "",
        "| Condition | Ready | Status | Detail | Blockers |",
        "| --- | --- | --- | --- | --- |",
    ]
    for condition in payload.get("conditions") or []:
        lines.append(
            "| {name} | {ready} | {status} | {detail} | {blockers} |".format(
                name=condition.get("name") or "",
                ready="yes" if condition.get("ready") else "no",
                status=condition.get("status") or "",
                detail=condition.get("detail") or "",
                blockers="; ".join(str(item) for item in (condition.get("blockers") or [])) or "-",
            )
        )
    lines.extend(["", "## Candidates", "", "| Priority | Strategy | Candidate | Execution | Automation | Blockers |", "| --- | --- | --- | --- | --- | --- |"])
    for candidate in payload.get("candidates") or []:
        lines.append(
            "| {priority} | {strategy} | {candidate} | {execution} | {automation} | {blockers} |".format(
                priority=candidate.get("priority") or "",
                strategy=candidate.get("strategy") or "",
                candidate=candidate.get("candidate_id") or "",
                execution=candidate.get("execution_status") or "",
                automation=candidate.get("automation_status") or "",
                blockers="; ".join(str(item) for item in (candidate.get("blockers") or [])) or "-",
            )
        )
    if not payload.get("candidates"):
        lines.append("| - | - | - | - | blocked | no rollback candidates |")
    lines.extend(["", "## Rule", ""])
    lines.append("- This guard is report-only.")
    lines.append("- It never runs rollback, deploy, service restart, order, or config mutation.")
    lines.append("- `preconditions_met_report_only` still does not apply anything; it only means evidence is ready for a separate human-approved procedure.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build report-only automatic rollback guard")
    parser.add_argument("--runtime-dir", default=str(ROOT / "runtime"))
    parser.add_argument("--reports-dir", default=str(ROOT / "reports"))
    parser.add_argument("--rollback-watch-json", default="")
    parser.add_argument("--rollback-execution-json", default="")
    parser.add_argument("--replay-readiness-json", default="")
    parser.add_argument("--approval-json", default=str(ROOT / "research_memory" / "approvals" / "rollback_automation_policy.json"))
    args = parser.parse_args(argv)

    runtime_dir = Path(args.runtime_dir)
    reports_dir = Path(args.reports_dir)
    watch_path = Path(args.rollback_watch_json) if args.rollback_watch_json else runtime_dir / "rollback_watch_review_latest.json"
    execution_path = Path(args.rollback_execution_json) if args.rollback_execution_json else runtime_dir / "rollback_execution_plan_latest.json"
    replay_path = Path(args.replay_readiness_json) if args.replay_readiness_json else runtime_dir / "replay_readiness_latest.json"
    policy_path = Path(args.approval_json)

    payload = build_payload(
        rollback_watch=read_json(watch_path),
        rollback_execution=read_json(execution_path),
        replay_readiness=read_json(replay_path),
        policy=read_json(policy_path),
        policy_path=policy_path,
    )
    runtime_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "rollback_automation_guard_latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (reports_dir / "rollback_automation_guard_latest.md").write_text(render_md(payload), encoding="utf-8")
    print(json.dumps(payload.get("summary") or {}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
