"""Build a report-only rollback execution checklist from rollback-watch review."""

from __future__ import annotations

import argparse
import json
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

STRATEGY_COMPONENTS = {
    "A/v11": "strategy-a",
    "B/v16": "strategy-b",
    "C/v14": "strategy-c",
}

OPERATOR_DECISION_STATUSES = {
    "narrow_b_v16_requested": "operator_requested_narrow_dry_run",
    "rollback_prepare_requested": "operator_requested_rollback_dry_run",
}
ACTIONABLE_STATUSES = {
    "manual_rollback_ready",
    "rollback_review_ready",
    "narrow_or_pause_ready",
    "operator_requested_narrow_dry_run",
    "operator_requested_rollback_dry_run",
}
EXP_RE = re.compile(r"(EXP-[A-Za-z0-9._-]+)", re.IGNORECASE)


def read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def as_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except Exception:
        return default


def slug(value: Any) -> str:
    text = str(value or "").strip().lower()
    out = []
    for ch in text:
        if ch.isalnum():
            out.append(ch)
        elif ch in {"/", "-", "_", ".", " "}:
            out.append("-")
    return "-".join(part for part in "".join(out).split("-") if part) or "unknown"


def normalize_id(value: Any) -> str:
    return str(value or "").strip().lower()


def extract_candidate_id(item: dict[str, Any]) -> str:
    for key in ("candidate_id", "item_id", "title", "evidence"):
        match = EXP_RE.search(str(item.get(key) or ""))
        if match:
            return match.group(1)
    return ""


def load_operator_decisions(path: Path) -> list[dict[str, Any]]:
    payload = read_json(path)
    items = payload.get("items") if isinstance(payload, dict) else []
    out: list[dict[str, Any]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "")
        if status not in OPERATOR_DECISION_STATUSES:
            continue
        candidate_id = extract_candidate_id(item)
        out.append({
            "item_id": item.get("item_id") or "",
            "candidate_id": candidate_id,
            "status": status,
            "requested_execution_status": OPERATOR_DECISION_STATUSES[status],
            "priority": item.get("priority") or "",
            "title": item.get("title") or "",
            "acknowledged_at": item.get("acknowledged_at") or "",
            "acknowledged_reason": item.get("acknowledged_reason") or "",
            "source": str(path),
        })
    return out


def operator_decision_index(decisions: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    indexed: dict[str, list[dict[str, Any]]] = {}
    for decision in decisions:
        candidate_id = normalize_id(decision.get("candidate_id"))
        if candidate_id:
            indexed.setdefault(candidate_id, []).append(decision)
    return indexed


def classify_execution(item: dict[str, Any]) -> tuple[str, str]:
    readiness = item.get("operator_readiness") if isinstance(item.get("operator_readiness"), dict) else {}
    if readiness.get("status") != "operator_ready":
        return "not_actionable", "operator readiness is not complete"
    action = str(readiness.get("action") or item.get("action") or "")
    if action == "prepare_manual_rollback":
        return "manual_rollback_ready", "P0 or unresolved close-failure item needs manual rollback preparation"
    if action == "prepare_rollback_review":
        return "rollback_review_ready", "mature enough to prepare rollback review packet"
    if action == "pause_expansion_review_quality":
        return "narrow_or_pause_ready", "quality pressure is enough to pause expansion and prepare narrowing review"
    return "monitoring_only", "operator-ready packet exists, but action is observation"


def command_hints(strategy: str, candidate_id: str) -> dict[str, Any]:
    component = STRATEGY_COMPONENTS.get(strategy, "research")
    candidate_slug = slug(candidate_id)
    return {
        "component": component,
        "dry_run_commands": [
            f"python 部署工具\\release_manager.py list --target tencent",
            f"python 部署工具\\release_manager.py rollback --target tencent --release-id <release-id-for-{candidate_slug}>",
            f"python 部署工具\\release_manager.py deploy --target tencent --component {component} --dry-run",
        ],
        "apply_commands_disabled": [
            f"python 部署工具\\release_manager.py rollback --target tencent --release-id <release-id-for-{candidate_slug}> --apply",
            f"python 部署工具\\release_manager.py deploy --target tencent --component {component} --apply",
        ],
    }


def build_checklist(item: dict[str, Any], execution_status: str) -> list[dict[str, Any]]:
    candidate_id = str(item.get("candidate_id") or "")
    strategy = str(item.get("strategy") or "")
    packet = item.get("decision_packet") if isinstance(item.get("decision_packet"), dict) else {}
    readiness = item.get("operator_readiness") if isinstance(item.get("operator_readiness"), dict) else {}
    q24 = item.get("quality_24h") if isinstance(item.get("quality_24h"), dict) else {}
    q72 = item.get("quality_72h") if isinstance(item.get("quality_72h"), dict) else {}
    window = item.get("window_24h") if isinstance(item.get("window_24h"), dict) else {}

    return [
        {
            "step": "operator_decision",
            "status": "ready" if execution_status in ACTIONABLE_STATUSES else "blocked",
            "detail": (
                "Operator requested dry-run review from report button."
                if execution_status in {"operator_requested_narrow_dry_run", "operator_requested_rollback_dry_run"}
                else "No report-side rollback/narrow request is attached yet."
            ),
        },
        {
            "step": "freeze_expansion",
            "status": "ready" if execution_status != "not_actionable" else "blocked",
            "detail": f"Keep {strategy} candidate {candidate_id} from expanding while evidence is reviewed.",
        },
        {
            "step": "capture_evidence",
            "status": "ready",
            "detail": (
                f"Record priority={item.get('priority')}, governance={readiness.get('status')}, "
                f"maturity={readiness.get('maturity')}, 24h after-cost PnL={as_float(q24.get('realized_pnl_after_cost')):+.2f}, "
                f"72h after-cost PnL={as_float(q72.get('realized_pnl_after_cost')):+.2f}, "
                f"close_failed_24h={as_int(window.get('close_failed'))}."
            ),
        },
        {
            "step": "verify_rollback_path",
            "status": "ready" if packet.get("rollback_path") else "blocked",
            "detail": "; ".join(str(v) for v in (packet.get("rollback_path") or [])) or "rollback_path missing",
        },
        {
            "step": "dry_run_release_command",
            "status": "ready" if execution_status != "not_actionable" else "blocked",
            "detail": "Run release_manager list/rollback/deploy in dry-run mode only, then attach output to operator review.",
        },
        {
            "step": "post_check_plan",
            "status": "ready",
            "detail": "After any future approved apply: verify service state, report freshness, no new OPEN_FAILED/CLOSE_FAILED, and no 418/429/-1003.",
        },
        {
            "step": "abort_criteria",
            "status": "ready",
            "detail": "Abort before apply if replay readiness is data_gap, decision packet changes, close-failure attribution is stale, or release-id is ambiguous.",
        },
    ]


def build_plan(item: dict[str, Any], operator_decisions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    execution_status, reason = classify_execution(item)
    candidate_id = str(item.get("candidate_id") or "")
    strategy = str(item.get("strategy") or "")
    operator_decisions = operator_decisions or []
    operator_request = operator_decisions[-1] if operator_decisions else {}
    if operator_request:
        execution_status = str(operator_request.get("requested_execution_status") or execution_status)
        reason = (
            "operator requested rollback dry-run from report"
            if execution_status == "operator_requested_rollback_dry_run"
            else "operator requested B/v16 narrowing dry-run from report"
        )
    hints = command_hints(strategy, candidate_id)
    checklist = build_checklist(item, execution_status)
    return {
        "plan_id": f"rollback-{slug(strategy)}-{slug(candidate_id)}",
        "candidate_id": candidate_id,
        "strategy": strategy,
        "priority": item.get("priority") or "",
        "watch_status": item.get("status") or "",
        "execution_status": execution_status,
        "reason": reason,
        "operator_request": operator_request,
        "operator_decisions": operator_decisions,
        "dry_run_only": True,
        "apply_enabled": False,
        "component": hints["component"],
        "dry_run_commands": hints["dry_run_commands"],
        "apply_commands_disabled": hints["apply_commands_disabled"],
        "checklist": checklist,
        "evidence": {
            "action": item.get("action") or "",
            "operator_readiness": item.get("operator_readiness") or {},
            "quality_24h": item.get("quality_24h") or {},
            "quality_72h": item.get("quality_72h") or {},
            "window_24h": item.get("window_24h") or {},
            "decision_packet": item.get("decision_packet") or {},
        },
    }


def build_payload_from_items(
    items: list[Any],
    source: str = "",
    operator_decisions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    decisions_by_candidate = operator_decision_index(operator_decisions or [])
    plans = [
        build_plan(item, decisions_by_candidate.get(normalize_id(item.get("candidate_id")), []))
        for item in items
        if isinstance(item, dict)
    ]
    actionable = [p for p in plans if p.get("execution_status") in ACTIONABLE_STATUSES]
    requested = [p for p in plans if p.get("operator_request")]
    status = (
        "operator_requested_dry_run_review"
        if requested
        else "ready_for_dry_run_review"
        if actionable
        else "waiting_for_operator_ready_items"
        if plans
        else "no_rollback_watch_items"
    )
    return {
        "generated_at": datetime.now(CST).isoformat(timespec="seconds"),
        "source": source,
        "operator_decision_count": len(operator_decisions or []),
        "mode": "report_only",
        "apply_enabled": False,
        "status": status,
        "summary": {
            "plans": len(plans),
            "actionable_plans": len(actionable),
            "operator_requested_plans": len(requested),
            "operator_requested_rollback": sum(1 for p in plans if p.get("execution_status") == "operator_requested_rollback_dry_run"),
            "operator_requested_narrow": sum(1 for p in plans if p.get("execution_status") == "operator_requested_narrow_dry_run"),
            "manual_rollback_ready": sum(1 for p in plans if p.get("execution_status") == "manual_rollback_ready"),
            "rollback_review_ready": sum(1 for p in plans if p.get("execution_status") == "rollback_review_ready"),
            "narrow_or_pause_ready": sum(1 for p in plans if p.get("execution_status") == "narrow_or_pause_ready"),
            "not_actionable": sum(1 for p in plans if p.get("execution_status") == "not_actionable"),
            "monitoring_only": sum(1 for p in plans if p.get("execution_status") == "monitoring_only"),
        },
        "plans": plans,
    }


def build_payload(review_path: Path, attention_path: Path | None = None) -> dict[str, Any]:
    review = read_json(review_path)
    items = review.get("items") if isinstance(review, dict) else []
    operator_decisions = load_operator_decisions(attention_path) if attention_path else []
    return build_payload_from_items(items, str(review_path), operator_decisions)


def render_md(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    lines = [
        "# Rollback Execution Plan",
        "",
        f"- Generated: `{payload.get('generated_at')}`",
        f"- Status: `{payload.get('status')}`",
        f"- Mode: `{payload.get('mode')}`",
        f"- Apply enabled: `{bool(payload.get('apply_enabled'))}`",
        f"- Plans: `{as_int(summary.get('plans'))}`",
        f"- Actionable dry-run plans: `{as_int(summary.get('actionable_plans'))}`",
        f"- Operator requested plans: `{as_int(summary.get('operator_requested_plans'))}`",
        "",
        "## Plans",
        "",
        "| Priority | Strategy | Candidate | Status | Operator request | Component | Dry-run | Reason |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for plan in payload.get("plans") or []:
        request = plan.get("operator_request") if isinstance(plan.get("operator_request"), dict) else {}
        lines.append(
            "| {priority} | {strategy} | {candidate} | {status} | {request} | {component} | {dry_run} | {reason} |".format(
                priority=plan.get("priority") or "",
                strategy=plan.get("strategy") or "",
                candidate=plan.get("candidate_id") or "",
                status=plan.get("execution_status") or "",
                request=request.get("status") or "-",
                component=plan.get("component") or "",
                dry_run="yes" if plan.get("dry_run_only") else "no",
                reason=plan.get("reason") or "",
            )
        )
    if not payload.get("plans"):
        lines.append("| - | - | - | no_rollback_watch_items | - | yes | No active P0/P1 rollback-watch items. |")

    for plan in payload.get("plans") or []:
        lines.extend(["", f"## {plan.get('candidate_id')}", ""])
        lines.append(f"- Strategy: `{plan.get('strategy')}`")
        lines.append(f"- Execution status: `{plan.get('execution_status')}`")
        lines.append(f"- Component: `{plan.get('component')}`")
        request = plan.get("operator_request") if isinstance(plan.get("operator_request"), dict) else {}
        if request:
            lines.append(f"- Operator request: `{request.get('status')}` from `{request.get('item_id')}` at `{request.get('acknowledged_at') or '-'}`")
        lines.append("- Dry-run commands:")
        for cmd in plan.get("dry_run_commands") or []:
            lines.append(f"  - `{cmd}`")
        lines.append("- Disabled apply commands:")
        for cmd in plan.get("apply_commands_disabled") or []:
            lines.append(f"  - `{cmd}`")
        lines.append("")
        lines.append("| Step | Status | Detail |")
        lines.append("| --- | --- | --- |")
        for step in plan.get("checklist") or []:
            lines.append(f"| {step.get('step')} | {step.get('status')} | {step.get('detail')} |")

    lines.extend(["", "## Rule", ""])
    lines.append("- This report never runs rollback, deploy, service restart, order, or config mutation.")
    lines.append("- Commands are hints for future approved operator action and must run dry-run first.")
    lines.append("- Apply commands are shown as disabled references only.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build report-only rollback execution checklist")
    parser.add_argument("--rollback-watch-json", default="")
    parser.add_argument("--attention-json", default="")
    parser.add_argument("--runtime-dir", default=str(ROOT / "runtime"))
    parser.add_argument("--reports-dir", default=str(ROOT / "reports"))
    args = parser.parse_args(argv)

    runtime_dir = Path(args.runtime_dir)
    reports_dir = Path(args.reports_dir)
    watch_path = Path(args.rollback_watch_json) if args.rollback_watch_json else runtime_dir / "rollback_watch_review_latest.json"
    attention_path = Path(args.attention_json) if args.attention_json else ROOT / "research_memory" / "attention" / "open_items.json"
    payload = build_payload(watch_path, attention_path)

    runtime_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "rollback_execution_plan_latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (reports_dir / "rollback_execution_plan_latest.md").write_text(render_md(payload), encoding="utf-8")
    print(json.dumps(payload.get("summary") or {}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
