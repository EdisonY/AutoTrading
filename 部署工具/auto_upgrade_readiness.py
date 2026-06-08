"""Report-only readiness guard for any future automatic strategy upgrade.

This tool never changes strategy config, deploys code, restarts services,
submits orders, or enables automation. It separates sample-dependent blockers
from non-sample blockers so the waiting-period backlog is explicit.
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

REQUIRED_POLICY_FIELDS = (
    "approved",
    "automatic_upgrade_enabled",
    "approved_by",
    "approved_at",
    "scope",
    "procedure_version",
)
AUTO_READY_STATUSES = {"ready_for_operator_review", "ready", "ok"}
UPGRADE_READY_STATUSES = {"verified_upgrade_ready"}
PRIORITY_READY = {"P0", "P1"}
SAMPLE_REPLAY_CATEGORIES = {"sample_gap", "context_gap"}


def now() -> datetime:
    return datetime.now(CST)


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST)
        return dt.astimezone(CST)
    except Exception:
        return None


def age_seconds(value: Any) -> float | None:
    dt = parse_dt(value)
    if not dt:
        return None
    return max(0.0, (now() - dt).total_seconds())


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


def read_json_result(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"payload": None, "status": "missing", "path": str(path), "error": ""}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        payload = json.loads(text)
        return {
            "payload": payload,
            "status": "ok" if isinstance(payload, dict) else "not_object",
            "path": str(path),
            "error": "" if isinstance(payload, dict) else "json root is not an object",
        }
    except Exception as exc:
        return {
            "payload": None,
            "status": "parse_error",
            "path": str(path),
            "error": f"{type(exc).__name__}: {exc}",
        }


def read_json(path: Path) -> Any:
    return read_json_result(path).get("payload")


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def condition(
    name: str,
    *,
    ready: bool,
    status: str,
    detail: str,
    blockers: list[str] | None = None,
    blocker_type: str = "non_sample",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out = {
        "name": name,
        "ready": bool(ready),
        "status": status,
        "detail": detail,
        "blocker_type": blocker_type,
        "blockers": blockers or [],
    }
    if extra:
        out.update(extra)
    return out


def policy_condition(policy: Any, policy_path: Path) -> dict[str, Any]:
    if not isinstance(policy, dict):
        return condition(
            "explicit_auto_upgrade_policy",
            ready=False,
            status="missing",
            detail=f"missing {policy_path}",
            blockers=["explicit_auto_upgrade_policy_missing"],
        )
    enabled = bool(policy.get("approved") is True and policy.get("automatic_upgrade_enabled") is True)
    missing = [field for field in REQUIRED_POLICY_FIELDS if policy.get(field) in (None, "", [], {})] if enabled else []
    blockers = [f"missing_policy_field:{field}" for field in missing]
    if not enabled:
        blockers.append("policy_not_approved_or_not_enabled")
    return condition(
        "explicit_auto_upgrade_policy",
        ready=enabled and not missing,
        status="approved" if enabled and not missing else "disabled_or_not_approved",
        detail="explicit automatic upgrade policy present" if enabled and not missing else "policy exists but automatic upgrade is disabled or not approved",
        blockers=blockers,
        extra={
            "policy": {
                "approved_by": policy.get("approved_by"),
                "approved_at": policy.get("approved_at"),
                "scope": policy.get("scope"),
                "procedure_version": policy.get("procedure_version"),
            }
        },
    )


def strategy_json_condition(result: dict[str, Any], max_age_hours: float) -> dict[str, Any]:
    status = str(result.get("status") or "missing")
    payload = result.get("payload")
    if status != "ok" or not isinstance(payload, dict):
        return condition(
            "strategy_evolution_json",
            ready=False,
            status=status,
            detail=result.get("error") or f"strategy evolution json is {status}",
            blockers=[f"strategy_evolution_json:{status}"],
            extra={"path": result.get("path"), "error": result.get("error") or ""},
        )
    age = age_seconds(payload.get("generated_at"))
    if age is None:
        return condition(
            "strategy_evolution_json",
            ready=False,
            status="missing_generated_at",
            detail="strategy evolution json has no generated_at",
            blockers=["strategy_evolution_json_missing_generated_at"],
            extra={"path": result.get("path")},
        )
    fresh = age <= max_age_hours * 3600
    return condition(
        "strategy_evolution_json",
        ready=fresh,
        status="fresh" if fresh else "stale",
        detail=f"generated_at={payload.get('generated_at')} age_hours={age / 3600:.2f}",
        blockers=[] if fresh else ["strategy_evolution_json_stale"],
        extra={"path": result.get("path"), "age_hours": round(age / 3600, 3)},
    )


def replay_condition(replay: Any) -> dict[str, Any]:
    if not isinstance(replay, dict):
        return condition(
            "replay_readiness",
            ready=False,
            status="missing",
            detail="replay readiness report missing",
            blockers=["replay_readiness:missing"],
        )
    status = str(replay.get("status") or "missing")
    components = replay.get("components") if isinstance(replay.get("components"), list) else []
    blockers: list[str] = []
    sample_blockers: list[str] = []
    non_sample_blockers: list[str] = []
    for component in components:
        if not isinstance(component, dict) or component.get("ready"):
            continue
        name = str(component.get("name") or "unknown")
        category = str(component.get("category") or component.get("status") or "unknown")
        blocker = f"replay:{name}:{category}"
        blockers.append(blocker)
        if category in SAMPLE_REPLAY_CATEGORIES:
            sample_blockers.append(blocker)
        else:
            non_sample_blockers.append(blocker)
    if status in AUTO_READY_STATUSES and not non_sample_blockers:
        ready = True
    else:
        ready = False
        if not blockers:
            blockers.append(f"replay_readiness:{status}")
            if status in SAMPLE_REPLAY_CATEGORIES:
                sample_blockers.append(f"replay_readiness:{status}")
            else:
                non_sample_blockers.append(f"replay_readiness:{status}")
    blocker_type = "sample" if sample_blockers and not non_sample_blockers else "non_sample"
    return condition(
        "replay_readiness",
        ready=ready,
        status=status,
        detail="replay/fill evidence ready" if ready else f"replay readiness is {status}",
        blockers=blockers,
        blocker_type=blocker_type,
        extra={
            "sample_blockers": sample_blockers,
            "non_sample_blockers": non_sample_blockers,
            "summary": replay.get("summary") if isinstance(replay.get("summary"), dict) else {},
        },
    )


def extract_upgrade_candidates(evolution: Any) -> list[dict[str, Any]]:
    decisions = evolution.get("decisions") if isinstance(evolution, dict) and isinstance(evolution.get("decisions"), list) else []
    candidates = []
    for item in decisions:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "")
        priority = str(item.get("priority") or "")
        if status in UPGRADE_READY_STATUSES and priority in PRIORITY_READY:
            candidates.append(item)
    return candidates


def candidate_condition(evolution: Any) -> dict[str, Any]:
    if not isinstance(evolution, dict):
        return condition(
            "candidate_maturity",
            ready=False,
            status="missing",
            detail="strategy evolution payload missing",
            blockers=["candidate_maturity:strategy_evolution_missing"],
        )
    summary = evolution.get("summary") if isinstance(evolution.get("summary"), dict) else {}
    expansion = summary.get("expansion_readiness") if isinstance(summary.get("expansion_readiness"), dict) else {}
    ready_count = as_int(expansion.get("ready_count"))
    candidates = extract_upgrade_candidates(evolution)
    ready = ready_count > 0 or bool(candidates)
    blockers = [] if ready else ["no_verified_upgrade_ready_candidate"]
    return condition(
        "candidate_maturity",
        ready=ready,
        status="ready_candidate_found" if ready else "no_ready_candidate",
        detail=f"ready_count={ready_count}; verified P0/P1={len(candidates)}",
        blockers=blockers,
        extra={
            "ready_count": ready_count,
            "candidate_count": len(candidates),
            "candidates": [
                {
                    "candidate_id": item.get("candidate_id") or "",
                    "strategy": item.get("strategy") or "",
                    "priority": item.get("priority") or "",
                    "status": item.get("status") or "",
                    "evidence_score": item.get("evidence_score"),
                    "risk_score": item.get("risk_score"),
                }
                for item in candidates[:12]
            ],
        },
    )


def rollback_pressure_condition(rollback_watch: Any, evolution: Any) -> dict[str, Any]:
    p0 = p1 = 0
    worst = ""
    if isinstance(rollback_watch, dict):
        summary = rollback_watch.get("summary") if isinstance(rollback_watch.get("summary"), dict) else {}
        p0 = as_int(summary.get("p0"))
        p1 = as_int(summary.get("p1"))
        worst = str(summary.get("worst_candidate") or "")
    if p0 == 0 and p1 == 0 and isinstance(evolution, dict):
        for item in evolution.get("decisions") if isinstance(evolution.get("decisions"), list) else []:
            if not isinstance(item, dict) or str(item.get("status") or "") not in {"rollback_required", "rollback_watch"}:
                continue
            if str(item.get("priority") or "") == "P0":
                p0 += 1
            elif str(item.get("priority") or "") == "P1":
                p1 += 1
            if not worst:
                worst = str(item.get("candidate_id") or "")
    ready = p0 == 0 and p1 == 0
    blockers: list[str] = []
    if p0:
        blockers.append(f"active_p0_rollback_pressure:{p0}")
    if p1:
        blockers.append(f"active_p1_rollback_pressure:{p1}")
    return condition(
        "negative_watch_clear",
        ready=ready,
        status="clear" if ready else "rollback_pressure",
        detail="no active P0/P1 rollback pressure" if ready else f"P0={p0}; P1={p1}; worst={worst or '-'}",
        blockers=blockers,
        extra={"p0": p0, "p1": p1, "worst_candidate": worst},
    )


def sample_gate_condition(replay: Any, a_rollout: Any, b_rollout: Any) -> dict[str, Any]:
    details: list[str] = []
    blockers: list[str] = []
    ready = True
    if isinstance(replay, dict):
        for component in replay.get("components") if isinstance(replay.get("components"), list) else []:
            if not isinstance(component, dict) or component.get("ready"):
                continue
            category = str(component.get("category") or component.get("status") or "")
            if category not in SAMPLE_REPLAY_CATEGORIES:
                continue
            name = str(component.get("name") or "unknown")
            detail = str(component.get("detail") or category)
            details.append(f"{name}: {detail}")
            blockers.append(f"sample_gate:{name}:{category}")
            ready = False
    for label, payload in (("a_v11_rollout", a_rollout), ("b_v16_rollout", b_rollout)):
        if not isinstance(payload, dict):
            continue
        replay72 = ((payload.get("replay_fill_comparison") or {}).get("72h") or {})
        paired = as_int(replay72.get("paired_trades"))
        completed = as_int(replay72.get("completed"))
        status_counts = replay72.get("status_counts") if isinstance(replay72.get("status_counts"), dict) else {}
        if paired and completed < paired:
            details.append(f"{label}: replay completed {completed}/{paired}; gaps={status_counts}")
    return condition(
        "fresh_contextual_samples",
        ready=ready,
        status="sufficient" if ready else "waiting_for_fresh_contextual_samples",
        detail="sample gate is satisfied" if ready else "; ".join(details) or "waiting for more paired samples",
        blockers=[] if ready else blockers or ["sample_gate:waiting_for_samples"],
        blocker_type="sample",
        extra={"details": details},
    )


def paper_real_calibration_condition(calibration: Any, calibration_path: Path) -> dict[str, Any]:
    if not isinstance(calibration, dict):
        return condition(
            "paper_real_calibration",
            ready=False,
            status="missing",
            detail=f"missing {calibration_path}",
            blockers=["paper_real_calibration_missing"],
        )
    approved = bool(calibration.get("approved") is True or calibration.get("status") in {"approved", "ready"})
    min_pairs = as_int(calibration.get("min_pairs"), 10)
    pairs = as_int(calibration.get("pairs"))
    max_abs_slippage_bps = as_float(calibration.get("max_abs_slippage_bps"), 999999.0)
    allowed_slippage_bps = as_float(calibration.get("allowed_slippage_bps"), 20.0)
    ready = approved and pairs >= min_pairs and max_abs_slippage_bps <= allowed_slippage_bps
    blockers: list[str] = []
    if not approved:
        blockers.append("paper_real_calibration_not_approved")
    if pairs < min_pairs:
        blockers.append(f"paper_real_calibration_pairs:{pairs}/{min_pairs}")
    if max_abs_slippage_bps > allowed_slippage_bps:
        blockers.append(f"paper_real_calibration_slippage_bps:{max_abs_slippage_bps:.2f}>{allowed_slippage_bps:.2f}")
    return condition(
        "paper_real_calibration",
        ready=ready,
        status="ready" if ready else "insufficient",
        detail=f"pairs={pairs}/{min_pairs}; max_abs_slippage_bps={max_abs_slippage_bps:.2f}/{allowed_slippage_bps:.2f}",
        blockers=blockers,
        extra={"pairs": pairs, "min_pairs": min_pairs, "approved": approved},
    )


def apply_disabled_condition() -> dict[str, Any]:
    return condition(
        "apply_disabled",
        ready=True,
        status="enforced",
        detail="this guard is report-only and cannot apply strategy upgrades",
        blockers=[],
    )


def build_candidate(item: dict[str, Any], blockers: list[str], sample_blockers: list[str], non_sample_blockers: list[str]) -> dict[str, Any]:
    if not blockers:
        automation_status = "preconditions_met_report_only"
    elif sample_blockers and not non_sample_blockers:
        automation_status = "waiting_for_samples_report_only"
    else:
        automation_status = "blocked"
    return {
        "candidate_id": item.get("candidate_id") or "",
        "strategy": item.get("strategy") or "",
        "priority": item.get("priority") or "",
        "status": item.get("status") or "",
        "evidence_score": item.get("evidence_score"),
        "risk_score": item.get("risk_score"),
        "automation_status": automation_status,
        "automatic_upgrade_allowed": False,
        "apply_enabled": False,
        "sample_blockers": sample_blockers,
        "non_sample_blockers": non_sample_blockers,
        "blockers": blockers,
    }


def split_blockers(conditions: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    sample: list[str] = []
    non_sample: list[str] = []
    for item in conditions:
        blockers = [str(v) for v in (item.get("blockers") or [])]
        if not blockers:
            continue
        if item.get("name") == "replay_readiness":
            sample.extend(str(v) for v in (item.get("sample_blockers") or []))
            non_sample.extend(str(v) for v in (item.get("non_sample_blockers") or []))
            continue
        if item.get("blocker_type") == "sample":
            sample.extend(blockers)
        else:
            non_sample.extend(blockers)
    return sorted(set(sample)), sorted(set(non_sample))


def build_payload(
    *,
    strategy_evolution_result: dict[str, Any],
    replay_readiness: Any,
    rollback_watch: Any,
    a_rollout: Any,
    b_rollout: Any,
    policy: Any,
    policy_path: Path,
    paper_real_calibration: Any,
    calibration_path: Path,
    max_age_hours: float = 4.0,
) -> dict[str, Any]:
    evolution = strategy_evolution_result.get("payload")
    conditions = [
        policy_condition(policy, policy_path),
        strategy_json_condition(strategy_evolution_result, max_age_hours),
        replay_condition(replay_readiness),
        candidate_condition(evolution),
        rollback_pressure_condition(rollback_watch, evolution),
        sample_gate_condition(replay_readiness, a_rollout, b_rollout),
        paper_real_calibration_condition(paper_real_calibration, calibration_path),
        apply_disabled_condition(),
    ]
    sample_blockers, non_sample_blockers = split_blockers(conditions)
    all_blockers = sorted(set(sample_blockers + non_sample_blockers))
    candidates = [
        build_candidate(item, all_blockers, sample_blockers, non_sample_blockers)
        for item in extract_upgrade_candidates(evolution)
    ]
    preconditions_met = not all_blockers and bool(candidates)
    waiting_samples_only = bool(sample_blockers) and not non_sample_blockers
    if preconditions_met:
        status = "preconditions_met_report_only"
        next_action = "manual_operator_review_before_any_upgrade"
    elif waiting_samples_only:
        status = "waiting_for_samples_report_only"
        next_action = "collect_fresh_contextual_paired_samples"
    else:
        status = "blocked_non_sample_gaps"
        next_action = "clear_non_sample_blockers_then_wait_for_samples"
    ready_conditions = sum(1 for item in conditions if item.get("ready"))
    return {
        "generated_at": now().isoformat(timespec="seconds"),
        "mode": "report_only",
        "status": status,
        "next_action": next_action,
        "automatic_upgrade_allowed": False,
        "apply_enabled": False,
        "preconditions_met": preconditions_met,
        "waiting_samples_only": waiting_samples_only,
        "summary": {
            "conditions": len(conditions),
            "ready_conditions": ready_conditions,
            "blockers": len(all_blockers),
            "sample_blockers": len(sample_blockers),
            "non_sample_blockers": len(non_sample_blockers),
            "candidates": len(candidates),
        },
        "conditions": conditions,
        "sample_blockers": sample_blockers,
        "non_sample_blockers": non_sample_blockers,
        "blockers": all_blockers,
        "candidates": candidates,
        "rule": "No automatic upgrade, deploy, service restart, order, or config mutation may run from this report.",
    }


def render_md(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    lines = [
        "# Automatic Upgrade Readiness",
        "",
        f"- Generated: `{payload.get('generated_at')}`",
        f"- Status: `{payload.get('status')}`",
        f"- Mode: `{payload.get('mode')}`",
        f"- Automatic upgrade allowed: `{bool(payload.get('automatic_upgrade_allowed'))}`",
        f"- Apply enabled: `{bool(payload.get('apply_enabled'))}`",
        f"- Conditions ready: `{as_int(summary.get('ready_conditions'))}/{as_int(summary.get('conditions'))}`",
        f"- Blockers: `{as_int(summary.get('blockers'))}`",
        f"- Non-sample blockers: `{as_int(summary.get('non_sample_blockers'))}`",
        f"- Sample blockers: `{as_int(summary.get('sample_blockers'))}`",
        f"- Next action: `{payload.get('next_action')}`",
        "",
        "## Conditions",
        "",
        "| Condition | Ready | Type | Status | Detail | Blockers |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in payload.get("conditions") or []:
        lines.append(
            "| {name} | {ready} | {kind} | {status} | {detail} | {blockers} |".format(
                name=item.get("name") or "",
                ready="yes" if item.get("ready") else "no",
                kind=item.get("blocker_type") or "",
                status=item.get("status") or "",
                detail=str(item.get("detail") or "").replace("|", "/"),
                blockers="; ".join(str(v) for v in (item.get("blockers") or [])) or "-",
            )
        )
    lines.extend(
        [
            "",
            "## Remaining Blockers",
            "",
            "### Non-sample",
            "",
        ]
    )
    non_sample = payload.get("non_sample_blockers") or []
    lines.extend([f"- `{item}`" for item in non_sample] or ["- none"])
    lines.extend(["", "### Sample", ""])
    sample = payload.get("sample_blockers") or []
    lines.extend([f"- `{item}`" for item in sample] or ["- none"])
    lines.extend(["", "## Candidates", "", "| Priority | Strategy | Candidate | Status | Automation | Blockers |", "| --- | --- | --- | --- | --- | --- |"])
    for item in payload.get("candidates") or []:
        lines.append(
            "| {priority} | {strategy} | {candidate} | {status} | {automation} | {blockers} |".format(
                priority=item.get("priority") or "",
                strategy=item.get("strategy") or "",
                candidate=item.get("candidate_id") or "",
                status=item.get("status") or "",
                automation=item.get("automation_status") or "",
                blockers="; ".join(str(v) for v in (item.get("blockers") or [])) or "-",
            )
        )
    if not payload.get("candidates"):
        lines.append("| - | - | - | - | blocked | no verified P0/P1 upgrade candidate |")
    lines.extend(["", "## Rule", ""])
    lines.append("- This guard is report-only.")
    lines.append("- It never runs automatic upgrade, deploy, service restart, order, or config mutation.")
    lines.append("- `preconditions_met_report_only` still requires a separate human-approved procedure.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build report-only automatic upgrade readiness guard")
    parser.add_argument("--runtime-dir", default=str(ROOT / "runtime"))
    parser.add_argument("--reports-dir", default=str(ROOT / "reports"))
    parser.add_argument("--strategy-evolution-json", default="")
    parser.add_argument("--replay-readiness-json", default="")
    parser.add_argument("--rollback-watch-json", default="")
    parser.add_argument("--a-rollout-json", default="")
    parser.add_argument("--b-rollout-json", default="")
    parser.add_argument("--approval-json", default=str(ROOT / "research_memory" / "approvals" / "auto_upgrade_policy.json"))
    parser.add_argument("--paper-real-calibration-json", default="")
    parser.add_argument("--max-age-hours", type=float, default=4.0)
    args = parser.parse_args(argv)

    runtime_dir = Path(args.runtime_dir)
    reports_dir = Path(args.reports_dir)
    strategy_path = Path(args.strategy_evolution_json) if args.strategy_evolution_json else runtime_dir / "strategy_evolution_latest.json"
    replay_path = Path(args.replay_readiness_json) if args.replay_readiness_json else runtime_dir / "replay_readiness_latest.json"
    rollback_path = Path(args.rollback_watch_json) if args.rollback_watch_json else runtime_dir / "rollback_watch_review_latest.json"
    a_path = Path(args.a_rollout_json) if args.a_rollout_json else runtime_dir / "a_v11_rollout_review_latest.json"
    b_path = Path(args.b_rollout_json) if args.b_rollout_json else runtime_dir / "b_v16_rollout_review_latest.json"
    policy_path = Path(args.approval_json)
    calibration_path = Path(args.paper_real_calibration_json) if args.paper_real_calibration_json else runtime_dir / "paper_real_calibration_latest.json"

    payload = build_payload(
        strategy_evolution_result=read_json_result(strategy_path),
        replay_readiness=read_json(replay_path),
        rollback_watch=read_json(rollback_path),
        a_rollout=read_json(a_path),
        b_rollout=read_json(b_path),
        policy=read_json(policy_path),
        policy_path=policy_path,
        paper_real_calibration=read_json(calibration_path),
        calibration_path=calibration_path,
        max_age_hours=args.max_age_hours,
    )
    write_text_atomic(runtime_dir / "auto_upgrade_readiness_latest.json", json.dumps(payload, ensure_ascii=False, indent=2))
    write_text_atomic(reports_dir / "auto_upgrade_readiness_latest.md", render_md(payload))
    print(json.dumps(payload.get("summary") or {}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
