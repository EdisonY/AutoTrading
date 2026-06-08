"""Report-only governance view for strategy evolution candidates.

This tool is deliberately read-only. It creates one registry-style view that
ties together candidates, controlled parameters, sample acceptance contracts,
and champion/challenger lifecycle state. It never changes strategy config,
deploys code, restarts services, submits orders, or enables automation.
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

UPGRADE_READY_STATUSES = {"verified_upgrade_ready", "ready_for_review", "ready_for_operator_review"}
ROLLBACK_STATUSES = {"rollback_watch", "rollback_required"}
LIVE_MONITORING_STATUSES = {"full_live_monitoring", "small_live_monitoring"}
SAMPLE_GAP_CATEGORIES = {"sample_gap", "context_gap"}


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


def parameter_registry() -> list[dict[str, Any]]:
    return [
        {
            "parameter_key": "a_v11.trailing_pullback.15m_atr",
            "strategy": "A/v11",
            "family_id": "FAM-A-v11-trailing-pullback",
            "parameter_version": "v11-20260529-trailing-full-live",
            "current_value": "1.0 ATR",
            "bounds": "0.8-1.2 ATR review band",
            "owner": "strategy_evolution_gate",
            "candidate_ids": [
                "EXP-20260527-v11-trailing-pullback-0p8",
                "EXP-20260527-v11-trailing-pullback-1p0",
            ],
            "apply_enabled": False,
            "notes": "full-live monitoring only; no automatic parameter mutation",
        },
        {
            "parameter_key": "a_v11.trailing_pullback.30m_atr",
            "strategy": "A/v11",
            "family_id": "FAM-A-v11-trailing-pullback",
            "parameter_version": "v11-20260529-trailing-full-live",
            "current_value": "0.8 ATR",
            "bounds": "0.8-1.2 ATR review band",
            "owner": "strategy_evolution_gate",
            "candidate_ids": [
                "EXP-20260527-v11-trailing-pullback-0p8",
                "EXP-20260527-v11-trailing-pullback-1p0",
            ],
            "apply_enabled": False,
            "notes": "full-live monitoring only; no automatic parameter mutation",
        },
        {
            "parameter_key": "a_v11.replacement_quality",
            "strategy": "A/v11",
            "family_id": "FAM-A-v11-replacement-quality",
            "parameter_version": "v11-replacement-quality-small-live",
            "current_value": "strong>=112; score_gap>=25; protect_profit>=2%",
            "bounds": "operator review required",
            "owner": "strategy_evolution_gate",
            "candidate_ids": ["EXP-20260523-v11-replacement-quality"],
            "apply_enabled": False,
            "notes": "small-live monitoring; not full-live verified",
        },
        {
            "parameter_key": "b_v16.atr_stop_bands",
            "strategy": "B/v16",
            "family_id": "FAM-B-v16-atr-stop-bands",
            "parameter_version": "v16-20260531-atr-stop-bands",
            "current_value": "2.5 / 2.0 / 1.5 by volatility band",
            "bounds": "rollback-watch; pause expansion before further change",
            "owner": "strategy_evolution_gate",
            "candidate_ids": ["EXP-20260527-v16-atr-stop-bands"],
            "apply_enabled": False,
            "notes": "current P1 rollback-watch evidence; not an upgrade candidate",
        },
        {
            "parameter_key": "b_v16.score_max",
            "strategy": "B/v16",
            "family_id": "FAM-B-v16-overheat-cap",
            "parameter_version": "v16-20260531-overheat-cap-85",
            "current_value": "85",
            "bounds": "rollback-watch; pause expansion before further change",
            "owner": "strategy_evolution_gate",
            "candidate_ids": ["EXP-20260527-v16-overheat-cap-85"],
            "apply_enabled": False,
            "notes": "current P1 rollback-watch evidence; not an upgrade candidate",
        },
        {
            "parameter_key": "b_v16.confirm_soft_pass",
            "strategy": "B/v16",
            "family_id": "FAM-B-v16-confirmation-policy",
            "parameter_version": "v16-confirm-soft-pass-shadow",
            "current_value": "shadow/counterfactual only",
            "bounds": "needs paper/live PnL evidence",
            "owner": "strategy_evolution_gate",
            "candidate_ids": ["EXP-20260523-v16-confirm-soft-pass"],
            "apply_enabled": False,
            "notes": "missing real/paper fill PnL; no promotion",
        },
        {
            "parameter_key": "c_v14.sample_expansion_thresholds",
            "strategy": "C/v14",
            "family_id": "FAM-C-v14-sample-expansion",
            "parameter_version": "v14-20260531-controlled-sample-expansion",
            "current_value": "1h threshold 50; 15m confirm 25; short penalty 10",
            "bounds": "controlled sampling only",
            "owner": "strategy_evolution_gate",
            "candidate_ids": [
                "EXP-20260523-v14-tail-guard",
                "EXP-20260527-v14-filter-ablation-btc-trend",
            ],
            "apply_enabled": False,
            "notes": "sample expansion remains gated by hard risk checks",
        },
    ]


def parameter_keys_for(candidate: dict[str, Any], registry: list[dict[str, Any]]) -> list[str]:
    candidate_id = str(candidate.get("candidate_id") or "")
    family_id = str(candidate.get("family_id") or "")
    keys = []
    for item in registry:
        if candidate_id and candidate_id in (item.get("candidate_ids") or []):
            keys.append(str(item.get("parameter_key")))
        elif family_id and family_id == item.get("family_id"):
            keys.append(str(item.get("parameter_key")))
    return sorted(set(keys))


def expansion_index(evolution: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    summary = evolution.get("summary") if isinstance(evolution, dict) else {}
    expansion = summary.get("expansion_readiness") if isinstance(summary, dict) else {}
    rows = expansion.get("items") if isinstance(expansion, dict) and isinstance(expansion.get("items"), list) else []
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if isinstance(row, dict) and row.get("candidate_id"):
            out[str(row.get("candidate_id"))] = row
    return out


def lifecycle_for(status: str, priority: str, quality: str) -> tuple[str, str, str]:
    status_l = status.lower()
    if status_l in ROLLBACK_STATUSES:
        return "rollback_watch", "deployed_challenger_under_pressure", "pause_expansion_review_quality"
    if status_l in UPGRADE_READY_STATUSES and priority in {"P0", "P1"}:
        return "operator_review_candidate", "challenger_ready_for_human_review", "manual_review_required"
    if status_l in LIVE_MONITORING_STATUSES:
        if quality == "maturing":
            return "maturing", "deployed_challenger_collecting_samples", "continue_sampling"
        return "live_monitoring", "deployed_challenger", "continue_sampling"
    if "shadow" in status_l or status_l in {"observe", "counterfactual_supported"}:
        return "research_backlog", "offline_challenger", "continue_shadow_validation"
    return "tracked", "candidate", "continue_observation"


def build_candidate_registry(evolution: Any, registry: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(evolution, dict):
        return []
    decisions = evolution.get("decisions") if isinstance(evolution.get("decisions"), list) else []
    expansion = expansion_index(evolution)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for decision in decisions:
        if not isinstance(decision, dict):
            continue
        candidate_id = str(decision.get("candidate_id") or decision.get("family_id") or "")
        if not candidate_id or candidate_id in seen:
            continue
        seen.add(candidate_id)
        exp = expansion.get(candidate_id, {})
        status = str(decision.get("status") or "")
        priority = str(decision.get("priority") or "")
        quality = str(exp.get("quality") or ("bad" if status in ROLLBACK_STATUSES else "unknown"))
        lifecycle, role, action = lifecycle_for(status, priority, quality)
        blockers = [str(v) for v in (decision.get("blockers") or [])]
        rows.append(
            {
                "candidate_id": candidate_id,
                "family_id": decision.get("family_id") or "",
                "strategy": decision.get("strategy") or "",
                "priority": priority,
                "status": status,
                "lifecycle": lifecycle,
                "champion_challenger_role": role,
                "quality": quality,
                "recommended_action": exp.get("action") or decision.get("recommended_action") or action,
                "evidence_score": as_int(decision.get("evidence_score")),
                "risk_score": as_int(decision.get("risk_score")),
                "closed_samples_24h": as_int(exp.get("closed_samples_24h")),
                "required_samples_24h": as_int(exp.get("required_samples_24h")),
                "missing_samples_24h": as_int(exp.get("missing_samples_24h")),
                "pnl_after_cost_24h": as_float(exp.get("pnl_after_cost_24h")),
                "parameter_keys": parameter_keys_for(decision, registry),
                "blockers": blockers,
                "automatic_upgrade_allowed": False,
                "apply_enabled": False,
            }
        )
    return rows


def lifecycle_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        key = str(row.get("lifecycle") or "unknown")
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


def sample_acceptance_contract(replay: Any, a_rollout: Any, b_rollout: Any) -> dict[str, Any]:
    required_fields = [
        "candidate_id",
        "parameter_version",
        "source_event_ts",
        "source_event_type",
        "source_timeframe",
        "atr",
        "entry_time",
        "paper_fill",
        "close_reason",
        "replay_pair_key",
    ]
    components = replay.get("components") if isinstance(replay, dict) and isinstance(replay.get("components"), list) else []
    rows = []
    blockers: list[str] = []
    for component in components:
        if not isinstance(component, dict):
            continue
        name = str(component.get("name") or "unknown")
        category = str(component.get("category") or component.get("status") or "unknown")
        metrics = component.get("metrics") if isinstance(component.get("metrics"), dict) else {}
        ready = bool(component.get("ready"))
        status_counts = metrics.get("status_counts") if isinstance(metrics.get("status_counts"), dict) else {}
        gap_detail = "; ".join(f"{k}={v}" for k, v in sorted(status_counts.items())) or str(component.get("detail") or "")
        if not ready and category in SAMPLE_GAP_CATEGORIES:
            blockers.append(f"{name}:{category}")
        rows.append(
            {
                "name": name,
                "ready": ready,
                "category": category,
                "status": component.get("status") or "",
                "detail": component.get("detail") or "",
                "paired_trades": as_int(metrics.get("paired_trades")),
                "completed": as_int(metrics.get("completed")),
                "completion_rate": as_float(metrics.get("completion_rate")),
                "min_paired_trades": as_int(metrics.get("min_paired_trades")),
                "min_completion_rate": as_float(metrics.get("min_completion_rate")),
                "status_counts": status_counts,
                "gap_detail": gap_detail,
            }
        )
    return {
        "status": "accepted" if rows and not blockers else "not_ready",
        "required_fields": required_fields,
        "components": rows,
        "blockers": blockers,
        "a_v11_decision": a_rollout.get("decision") if isinstance(a_rollout, dict) else {},
        "b_v16_decision": b_rollout.get("decision") if isinstance(b_rollout, dict) else {},
        "acceptance_rule": "Fresh contextual OPEN/CLOSE pairs must carry source timeframe, ATR, parameter version, entry time, close reason, and paper fill audit.",
    }


def pruning_hints(candidates: list[dict[str, Any]], sample_contract: dict[str, Any]) -> list[dict[str, Any]]:
    hints = []
    for row in candidates:
        lifecycle = str(row.get("lifecycle") or "")
        candidate_id = str(row.get("candidate_id") or "")
        if lifecycle == "rollback_watch":
            hints.append(
                {
                    "candidate_id": candidate_id,
                    "strategy": row.get("strategy") or "",
                    "hint": "pause_expansion_review_quality",
                    "reason": "candidate is under rollback-watch evidence, so it must not be treated as an upgrade candidate",
                }
            )
        elif lifecycle == "maturing":
            hints.append(
                {
                    "candidate_id": candidate_id,
                    "strategy": row.get("strategy") or "",
                    "hint": "continue_sampling",
                    "reason": "live monitoring has not met sample/robustness requirements",
                }
            )
    for blocker in sample_contract.get("blockers") or []:
        hints.append(
            {
                "candidate_id": str(blocker),
                "strategy": "",
                "hint": "wait_for_fresh_contextual_samples",
                "reason": "sample acceptance contract is not met",
            }
        )
    return hints


def build_payload(
    *,
    strategy_evolution: Any,
    auto_upgrade: Any,
    replay_readiness: Any,
    rollback_automation: Any,
    a_rollout: Any,
    b_rollout: Any,
) -> dict[str, Any]:
    registry = parameter_registry()
    candidates = build_candidate_registry(strategy_evolution, registry)
    sample_contract = sample_acceptance_contract(replay_readiness, a_rollout, b_rollout)
    lifecycle_summary = lifecycle_counts(candidates)
    upgrade_ready = [
        row for row in candidates
        if row.get("status") in UPGRADE_READY_STATUSES and row.get("priority") in {"P0", "P1"}
    ]
    rollback_watch = [row for row in candidates if row.get("lifecycle") == "rollback_watch"]
    auto_summary = auto_upgrade.get("summary") if isinstance(auto_upgrade, dict) and isinstance(auto_upgrade.get("summary"), dict) else {}
    rollback_summary = (
        rollback_automation.get("summary")
        if isinstance(rollback_automation, dict) and isinstance(rollback_automation.get("summary"), dict)
        else {}
    )
    status = "blocked_report_only"
    if sample_contract.get("status") == "not_ready" and not rollback_watch and not upgrade_ready:
        status = "waiting_for_samples_report_only"
    if upgrade_ready and sample_contract.get("status") == "accepted" and not rollback_watch:
        status = "operator_review_report_only"
    return {
        "generated_at": now().isoformat(timespec="seconds"),
        "mode": "report_only",
        "status": status,
        "automatic_upgrade_allowed": False,
        "apply_enabled": False,
        "summary": {
            "candidates": len(candidates),
            "parameters": len(registry),
            "upgrade_ready_candidates": len(upgrade_ready),
            "rollback_watch_candidates": len(rollback_watch),
            "sample_contract_status": sample_contract.get("status"),
            "sample_contract_blockers": len(sample_contract.get("blockers") or []),
            "auto_upgrade_blockers": as_int(auto_summary.get("blockers")),
            "rollback_automation_blockers": as_int(rollback_summary.get("blockers")),
        },
        "lifecycle_summary": lifecycle_summary,
        "candidate_registry": candidates,
        "parameter_registry": registry,
        "sample_acceptance_contract": sample_contract,
        "pruning_hints": pruning_hints(candidates, sample_contract),
        "rule": "Report-only governance. No automatic upgrade, rollback, tuning, deploy, restart, order, or config mutation is enabled.",
    }


def render_md(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    lifecycle = payload.get("lifecycle_summary") or {}
    lines = [
        "# Strategy Candidate Governance",
        "",
        f"- Generated: `{payload.get('generated_at')}`",
        f"- Mode: `{payload.get('mode')}`",
        f"- Status: `{payload.get('status')}`",
        f"- Automatic upgrade allowed: `{bool(payload.get('automatic_upgrade_allowed'))}`",
        f"- Apply enabled: `{bool(payload.get('apply_enabled'))}`",
        f"- Candidates: `{as_int(summary.get('candidates'))}`",
        f"- Parameters: `{as_int(summary.get('parameters'))}`",
        f"- Rollback-watch candidates: `{as_int(summary.get('rollback_watch_candidates'))}`",
        f"- Upgrade-ready candidates: `{as_int(summary.get('upgrade_ready_candidates'))}`",
        f"- Sample contract: `{summary.get('sample_contract_status')}` / blockers `{as_int(summary.get('sample_contract_blockers'))}`",
        "",
        "## Lifecycle",
        "",
        "| Lifecycle | Count |",
        "| --- | ---: |",
    ]
    for key, value in lifecycle.items():
        lines.append(f"| {safe_text(key)} | {as_int(value)} |")
    if not lifecycle:
        lines.append("| none | 0 |")
    lines.extend(
        [
            "",
            "## Candidate Registry",
            "",
            "| Priority | Strategy | Candidate | Lifecycle | Role | Quality | Action | Parameter Keys | Blockers |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in payload.get("candidate_registry") or []:
        lines.append(
            "| {priority} | {strategy} | {candidate} | {lifecycle} | {role} | {quality} | {action} | {params} | {blockers} |".format(
                priority=safe_text(row.get("priority")),
                strategy=safe_text(row.get("strategy")),
                candidate=safe_text(row.get("candidate_id")),
                lifecycle=safe_text(row.get("lifecycle")),
                role=safe_text(row.get("champion_challenger_role")),
                quality=safe_text(row.get("quality")),
                action=safe_text(row.get("recommended_action")),
                params=", ".join(str(v) for v in (row.get("parameter_keys") or [])) or "-",
                blockers="; ".join(str(v) for v in (row.get("blockers") or [])) or "-",
            )
        )
    if not payload.get("candidate_registry"):
        lines.append("| - | - | - | - | - | - | - | - | no candidates |")
    lines.extend(
        [
            "",
            "## Parameter Registry",
            "",
            "| Strategy | Parameter Key | Version | Current Value | Bounds | Apply | Notes |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in payload.get("parameter_registry") or []:
        lines.append(
            "| {strategy} | {key} | {version} | {value} | {bounds} | {apply} | {notes} |".format(
                strategy=safe_text(row.get("strategy")),
                key=safe_text(row.get("parameter_key")),
                version=safe_text(row.get("parameter_version")),
                value=safe_text(row.get("current_value")),
                bounds=safe_text(row.get("bounds")),
                apply="yes" if row.get("apply_enabled") else "no",
                notes=safe_text(row.get("notes")),
            )
        )
    contract = payload.get("sample_acceptance_contract") if isinstance(payload.get("sample_acceptance_contract"), dict) else {}
    lines.extend(
        [
            "",
            "## Sample Acceptance Contract",
            "",
            f"- Status: `{contract.get('status') or 'missing'}`",
            f"- Required fields: `{', '.join(str(v) for v in (contract.get('required_fields') or []))}`",
            "",
            "| Component | Ready | Category | Detail | Paired | Completed | Gaps |",
            "| --- | --- | --- | --- | ---: | ---: | --- |",
        ]
    )
    for row in contract.get("components") or []:
        lines.append(
            "| {name} | {ready} | {category} | {detail} | {paired} | {completed} | {gaps} |".format(
                name=safe_text(row.get("name")),
                ready="yes" if row.get("ready") else "no",
                category=safe_text(row.get("category")),
                detail=safe_text(row.get("detail")),
                paired=as_int(row.get("paired_trades")),
                completed=as_int(row.get("completed")),
                gaps=safe_text(row.get("gap_detail") or "-"),
            )
        )
    lines.extend(
        [
            "",
            "## Pruning / Promotion Hints",
            "",
            "| Strategy | Candidate | Hint | Reason |",
            "| --- | --- | --- | --- |",
        ]
    )
    for row in payload.get("pruning_hints") or []:
        lines.append(
            "| {strategy} | {candidate} | {hint} | {reason} |".format(
                strategy=safe_text(row.get("strategy")),
                candidate=safe_text(row.get("candidate_id")),
                hint=safe_text(row.get("hint")),
                reason=safe_text(row.get("reason")),
            )
        )
    if not payload.get("pruning_hints"):
        lines.append("| - | - | - | no pruning hint |")
    lines.extend(["", "## Rule", "", f"- {payload.get('rule')}"])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build report-only strategy candidate governance registry")
    parser.add_argument("--runtime-dir", default=str(ROOT / "runtime"))
    parser.add_argument("--reports-dir", default=str(ROOT / "reports"))
    args = parser.parse_args(argv)

    runtime_dir = Path(args.runtime_dir)
    reports_dir = Path(args.reports_dir)
    payload = build_payload(
        strategy_evolution=read_json(runtime_dir / "strategy_evolution_latest.json"),
        auto_upgrade=read_json(runtime_dir / "auto_upgrade_readiness_latest.json"),
        replay_readiness=read_json(runtime_dir / "replay_readiness_latest.json"),
        rollback_automation=read_json(runtime_dir / "rollback_automation_guard_latest.json"),
        a_rollout=read_json(runtime_dir / "a_v11_rollout_review_latest.json"),
        b_rollout=read_json(runtime_dir / "b_v16_rollout_review_latest.json"),
    )
    write_text_atomic(runtime_dir / "strategy_candidate_governance_latest.json", json.dumps(payload, ensure_ascii=False, indent=2))
    write_text_atomic(reports_dir / "strategy_candidate_governance_latest.md", render_md(payload))
    print(json.dumps(payload.get("summary") or {}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
