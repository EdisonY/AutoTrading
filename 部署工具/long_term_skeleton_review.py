"""Build a bone-only long-term task skeleton review.

The report answers one question: does each long-term module have input,
main processing, output, portal visibility, sync/deploy wiring, and a smoke
or test path? It does not run services or call Binance.
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

CATEGORIES = ("inputs", "main", "outputs", "portal", "sync", "tests")


def bone(label: str, path: str, *, contains: str | list[str] | None = None) -> dict[str, Any]:
    markers = []
    if isinstance(contains, str):
        markers = [contains]
    elif isinstance(contains, list):
        markers = contains
    return {"label": label, "path": path, "contains": markers}


def module_spec(
    *,
    item_id: str,
    priority: str,
    name: str,
    objective: str,
    validation_blockers: list[str] | None = None,
    post_launch_backlog: list[str] | None = None,
    **bones: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "id": item_id,
        "priority": priority,
        "name": name,
        "objective": objective,
        "validation_blockers": validation_blockers or [],
        "post_launch_backlog": post_launch_backlog or [],
        "bones": {category: bones.get(category, []) for category in CATEGORIES},
    }


DEFAULT_SPECS: list[dict[str, Any]] = [
    module_spec(
        item_id="P0-A",
        priority="P0",
        name="Binance API root fix",
        objective="Central account state, user stream, queue executor/client, and staged clean-run validation.",
        inputs=[
            bone("central account-state model", "core/account_state.py", contains="ACCOUNT_STATE_FILENAME"),
            bone("API queue store", "core/binance_api_queue.py", contains="DEFAULT_DB_PATH"),
            bone("user-stream state", "core/binance_user_stream.py", contains="listen"),
        ],
        main=[
            bone("account-state service", "部署工具/account_state_service.py"),
            bone("API queue executor service", "部署工具/binance_api_queue_service.py", contains="--execute"),
            bone("user-stream service", "部署工具/binance_user_stream_service.py"),
            bone("queue client fail-closed path", "core/binance_api_queue_client.py", contains="fail"),
        ],
        outputs=[
            bone("account-state output declared", "core/account_state.py", contains="account_state_latest.json"),
            bone("queue DB output declared", "core/binance_api_queue.py", contains="binance_api_queue.sqlite3"),
            bone("user-stream event log declared", "core/binance_user_stream_runtime.py", contains="binance_user_stream_events.jsonl"),
        ],
        portal=[
            bone("API cooldown/rate visibility", "部署工具/portal_dashboard.py", contains=["api_rate_limits", "cooldown"]),
        ],
        sync=[
            bone("account-state release component", "部署工具/release_manager.py", contains="account-state"),
            bone("api-queue release component", "部署工具/release_manager.py", contains="api-queue"),
            bone("user-stream release component", "部署工具/release_manager.py", contains="user-stream"),
            bone("account-state systemd unit", "部署工具/systemd/crypto-account-state.service"),
            bone("API queue systemd unit", "部署工具/systemd/crypto-binance-api-queue.service"),
            bone("user-stream systemd units", "部署工具/systemd/crypto-binance-user-stream.service"),
        ],
        tests=[
            bone("account-state tests", "tests/test_account_state.py"),
            bone("queue tests", "tests/test_binance_api_queue.py"),
            bone("queue client tests", "tests/test_binance_api_queue_client.py"),
            bone("user-stream tests", "tests/test_binance_user_stream.py"),
        ],
        validation_blockers=[
            "Run staged cache -> sentinel -> A/B/C fresh-run for 30 minutes with no 418/429/-1003.",
            "Prove account state stays fresh naturally and cooldown/source/top paths remain visible.",
        ],
    ),
    module_spec(
        item_id="P0-B",
        priority="P0",
        name="Replay/live same path",
        objective="Live scanners persist exact gate cases and replay audits the same pure gates.",
        inputs=[
            bone("pure gates", "core/strategy_gates.py"),
            bone("serializable gate cases", "core/strategy_gate_cases.py", contains="evaluate_strategy_gate_case"),
            bone("scanner exact case persistence A", "策略文件/scanner.py", contains="strategy_gate_case"),
            bone("scanner exact case persistence B", "策略文件/scanner_v16.py", contains="strategy_gate_case"),
            bone("scanner exact case persistence C", "策略文件/scanner_v14.py", contains="strategy_gate_case"),
        ],
        main=[bone("exact parity audit", "部署工具/replay_live_parity_audit.py", contains="strategy_gate_case")],
        outputs=[
            bone("parity JSON output", "部署工具/replay_live_parity_audit.py", contains="replay_live_parity_latest.json"),
            bone("parity Markdown output", "部署工具/replay_live_parity_audit.py", contains="replay_live_parity_latest.md"),
        ],
        portal=[bone("parity portal route", "部署工具/portal_dashboard.py", contains="REPLAY_PARITY_JSON")],
        sync=[
            bone("Aliyun refresh parity run", "部署工具/aliyun_analysis_refresh.sh", contains="replay_live_parity_audit.py"),
            bone("Aliyun shadow parity run", "部署工具/aliyun_shadow_review.sh", contains="replay_live_parity_audit.py"),
            bone("reverse sync parity report", "部署工具/sync_aliyun_reports_to_tencent.py", contains="replay_live_parity_latest"),
            bone("live-context pulls parity report", "部署工具/pull_live_context.py", contains="replay_live_parity_latest"),
        ],
        tests=[
            bone("pure gate tests", "tests/test_strategy_gates.py"),
            bone("case runner tests", "tests/test_strategy_gate_cases.py"),
            bone("parity audit tests", "tests/test_replay_live_parity_audit.py"),
        ],
        validation_blockers=[
            "Final staged fresh-run must create post-instrumentation rows with exact cases.",
            "Old rows without exact cases remain coverage gaps, not accepted parity.",
        ],
    ),
    module_spec(
        item_id="P1-A",
        priority="P1",
        name="A/v11 rollout quality decision",
        objective="A/v11 trailing rollout has a decision packet, attribution, replay comparison, and portal visibility.",
        inputs=[bone("event-store input", "部署工具/a_v11_rollout_review.py", contains="--db")],
        main=[bone("A/v11 rollout review", "部署工具/a_v11_rollout_review.py", contains="decision_packet")],
        outputs=[
            bone("A/v11 rollout JSON", "部署工具/a_v11_rollout_review.py", contains="a_v11_rollout_review_latest.json"),
            bone("A/v11 rollout Markdown", "部署工具/a_v11_rollout_review.py", contains="a_v11_rollout_review_latest.md"),
        ],
        portal=[bone("A/v11 portal section", "部署工具/portal_dashboard.py", contains="A_V11_ROLLOUT_JSON")],
        sync=[
            bone("Aliyun refresh A review", "部署工具/aliyun_analysis_refresh.sh", contains="a_v11_rollout_review.py"),
            bone("reverse sync A report", "部署工具/sync_aliyun_reports_to_tencent.py", contains="a_v11_rollout_review_latest"),
            bone("live-context A report", "部署工具/pull_live_context.py", contains="a_v11_rollout_review_latest"),
        ],
        tests=[bone("A/v11 packet tests", "tests/test_a_v11_rollout_review_packet.py")],
        validation_blockers=["Needs enough fresh post-refresh samples and operator-quality continue/narrow/rollback review."],
    ),
    module_spec(
        item_id="P1-B",
        priority="P1",
        name="B/v16 rollout quality decision",
        objective="B/v16 full-live candidates have attribution, replay comparison, decision packet, and portal visibility.",
        inputs=[bone("event-store input", "部署工具/b_v16_rollout_review.py", contains="--db")],
        main=[bone("B/v16 rollout review", "部署工具/b_v16_rollout_review.py", contains="decision_packet")],
        outputs=[
            bone("B/v16 rollout JSON", "部署工具/b_v16_rollout_review.py", contains="b_v16_rollout_review_latest.json"),
            bone("B/v16 rollout Markdown", "部署工具/b_v16_rollout_review.py", contains="b_v16_rollout_review_latest.md"),
        ],
        portal=[bone("B/v16 portal section", "部署工具/portal_dashboard.py", contains="B_V16_ROLLOUT_JSON")],
        sync=[
            bone("Aliyun refresh B review", "部署工具/aliyun_analysis_refresh.sh", contains="b_v16_rollout_review.py"),
            bone("reverse sync B report", "部署工具/sync_aliyun_reports_to_tencent.py", contains="b_v16_rollout_review_latest"),
            bone("live-context B report", "部署工具/pull_live_context.py", contains="b_v16_rollout_review_latest"),
        ],
        tests=[bone("B/v16 review tests", "tests/test_b_v16_rollout_review.py")],
        validation_blockers=["Needs mature post-refresh PF/cost/failure samples before decision."],
    ),
    module_spec(
        item_id="P1-C",
        priority="P1",
        name="Replay/fill engine",
        objective="Shared fill kernel feeds OPEN_SKIPPED, rollout, recovery, depth, stress, and readiness reports.",
        inputs=[
            bone("local Kline source", "core/replay_kline_source.py"),
            bone("local depth source", "core/replay_depth_cache.py"),
        ],
        main=[
            bone("fill kernel", "core/replay_fill.py", contains="replay_fill"),
            bone("counterfactual fill consumer", "部署工具/counterfactual_open_skips.py", contains="replay_fill"),
            bone("replay readiness gate", "部署工具/replay_readiness_review.py"),
        ],
        outputs=[
            bone("counterfactual report", "部署工具/counterfactual_open_skips.py", contains="counterfactual_open_skips_latest"),
            bone("readiness JSON", "部署工具/replay_readiness_review.py", contains="replay_readiness_latest.json"),
            bone("readiness Markdown", "部署工具/replay_readiness_review.py", contains="replay_readiness_latest.md"),
        ],
        portal=[bone("replay readiness portal", "部署工具/portal_dashboard.py", contains="REPLAY_READINESS_JSON")],
        sync=[
            bone("Aliyun refresh readiness", "部署工具/aliyun_analysis_refresh.sh", contains="replay_readiness_review.py"),
            bone("reverse sync readiness", "部署工具/sync_aliyun_reports_to_tencent.py", contains="replay_readiness_latest"),
            bone("live-context readiness", "部署工具/pull_live_context.py", contains="replay_readiness_latest"),
        ],
        tests=[
            bone("fill tests", "tests/test_replay_fill.py"),
            bone("counterfactual fill tests", "tests/test_counterfactual_replay_fill.py"),
            bone("readiness tests", "tests/test_replay_readiness_review.py"),
        ],
        validation_blockers=[
            "Staged Kline/depth ingest must move replay readiness from data_gap/waiting_for_samples to ready.",
        ],
        post_launch_backlog=["True queue-priority and time-varying market-impact simulation beyond static assumptions."],
    ),
    module_spec(
        item_id="P1-D",
        priority="P1",
        name="Gray/rollback gate",
        objective="Promotion gate, rollback watch, dry-run plan, and automation guard are visible but report-only.",
        inputs=[bone("strategy evolution input", "部署工具/strategy_evolution_gate.py", contains="decision_packet")],
        main=[
            bone("rollback watch", "部署工具/rollback_watch_review.py"),
            bone("rollback execution plan", "部署工具/rollback_execution_plan.py"),
            bone("rollback automation guard", "部署工具/rollback_automation_guard.py"),
        ],
        outputs=[
            bone("watch output", "部署工具/rollback_watch_review.py", contains="rollback_watch_review_latest"),
            bone("execution output", "部署工具/rollback_execution_plan.py", contains="rollback_execution_plan_latest"),
            bone("automation output", "部署工具/rollback_automation_guard.py", contains="rollback_automation_guard_latest"),
        ],
        portal=[bone("rollback portal sections", "部署工具/portal_dashboard.py", contains="ROLLBACK_AUTOMATION_JSON")],
        sync=[
            bone("Aliyun refresh rollback chain", "部署工具/aliyun_analysis_refresh.sh", contains="rollback_automation_guard.py"),
            bone("reverse sync rollback chain", "部署工具/sync_aliyun_reports_to_tencent.py", contains="rollback_automation_guard_latest"),
            bone("live-context rollback chain", "部署工具/pull_live_context.py", contains="rollback_automation_guard_latest"),
        ],
        tests=[
            bone("decision packet tests", "tests/test_strategy_decision_packet.py"),
            bone("rollback execution tests", "tests/test_rollback_execution_plan.py"),
            bone("rollback automation tests", "tests/test_rollback_automation_guard.py"),
        ],
        validation_blockers=["Automatic apply remains disabled until explicit policy, mature evidence, and reviewed releases exist."],
    ),
    module_spec(
        item_id="P1-E",
        priority="P1",
        name="Long-history research warehouse",
        objective="Research store export/query, Kline/depth planners, retention, compaction, and portal visibility.",
        inputs=[bone("event-store export", "部署工具/research_store_export.py")],
        main=[
            bone("research query", "部署工具/research_store_query.py"),
            bone("Kline backfill planner", "部署工具/research_kline_backfill.py"),
            bone("depth backfill planner", "部署工具/research_depth_backfill.py"),
            bone("retention planner", "部署工具/research_store_retention.py"),
            bone("compaction planner", "部署工具/research_store_compaction.py"),
        ],
        outputs=[
            bone("Kline planner output", "部署工具/research_kline_backfill.py", contains="research_kline_backfill_latest"),
            bone("depth planner output", "部署工具/research_depth_backfill.py", contains="research_depth_backfill_latest"),
            bone("research summary output", "部署工具/research_store_query.py", contains="research_store_summary_latest"),
        ],
        portal=[bone("research portal sections", "部署工具/portal_dashboard.py", contains="RESEARCH_STORE_JSON")],
        sync=[
            bone("Aliyun refresh research chain", "部署工具/aliyun_analysis_refresh.sh", contains="research_kline_backfill.py"),
            bone("reverse sync research chain", "部署工具/sync_aliyun_reports_to_tencent.py", contains="research_kline_backfill_latest"),
            bone("live-context research chain", "部署工具/pull_live_context.py", contains="research_kline_backfill_latest"),
        ],
        tests=[
            bone("Kline planner tests", "tests/test_research_kline_backfill.py"),
            bone("depth planner tests", "tests/test_research_depth_backfill.py"),
            bone("query tests", "tests/test_research_store_query.py"),
            bone("retention tests", "tests/test_research_store_retention.py"),
            bone("compaction tests", "tests/test_research_store_compaction.py"),
        ],
        validation_blockers=["Staged submit/ingest must produce 30+ day Kline acceptance and depth coverage."],
    ),
    module_spec(
        item_id="P2-A",
        priority="P2",
        name="Sentinel quality review",
        objective="Sentinel watchlist history, coverage, forward returns, and portal visibility.",
        inputs=[bone("watchlist history producer", "策略文件/market_mover_sentinel.py", contains="market_mover_watchlist_history.jsonl")],
        main=[bone("sentinel quality review", "部署工具/sentinel_quality_review.py", contains="watchlist_history")],
        outputs=[bone("sentinel report output", "部署工具/sentinel_quality_review.py", contains="sentinel_quality_latest")],
        portal=[bone("sentinel portal section", "部署工具/portal_dashboard.py", contains="SENTINEL_QUALITY_JSON")],
        sync=[
            bone("bounded watchlist mirror", "部署工具/shadow_sync_from_tencent.py", contains="market_mover_watchlist_history.jsonl"),
            bone("Aliyun sentinel run", "部署工具/aliyun_analysis_refresh.sh", contains="sentinel_quality_review.py"),
            bone("reverse sync sentinel report", "部署工具/sync_aliyun_reports_to_tencent.py", contains="sentinel_quality_latest"),
        ],
        tests=[bone("portal sentinel smoke path", "部署工具/portal_dashboard.py", contains="sentinel_quality_summary")],
        validation_blockers=["Needs watchlist-history accumulation after staged run for deeper attribution."],
        post_launch_backlog=["Split missed big moves into watchlist miss vs scanner universe vs mirror truncation."],
    ),
    module_spec(
        item_id="P2-B",
        priority="P2",
        name="Recovery-position strategy review",
        objective="Recovery positions are separated from active alpha and reviewed with signal, exit, and replay evidence.",
        inputs=[bone("account/event truth input", "部署工具/strategy_truth_ledger.py", contains="recovery")],
        main=[bone("truth ledger recovery review", "部署工具/strategy_truth_ledger.py", contains="recovery_replay_evidence")],
        outputs=[bone("truth ledger output", "部署工具/strategy_truth_ledger.py", contains="strategy_truth_latest")],
        portal=[bone("recovery portal table", "部署工具/portal_dashboard.py", contains="recovery_replay_evidence")],
        sync=[
            bone("Aliyun truth ledger run", "部署工具/aliyun_analysis_refresh.sh", contains="strategy_truth_ledger.py"),
            bone("reverse sync truth report", "部署工具/sync_aliyun_reports_to_tencent.py", contains="strategy_truth_latest"),
        ],
        tests=[bone("recovery tests", "tests/test_strategy_truth_ledger_recovery_path.py")],
        validation_blockers=["Automatic recovery exit remains disabled until governance and long-window replay are ready."],
        post_launch_backlog=["Approved automatic recovery-exit policy after staged evidence."],
    ),
    module_spec(
        item_id="P2-C",
        priority="P2",
        name="Existing candidates observation",
        objective="Existing strategy candidates stay visible through evolution gate, approvals, and attention ledger.",
        inputs=[
            bone("approvals ledger", "research_memory/approvals/manual_actions.jsonl"),
            bone("durable attention cache", "research_memory/attention/open_items.json"),
        ],
        main=[
            bone("strategy evolution gate", "部署工具/strategy_evolution_gate.py"),
            bone("decision attention", "部署工具/decision_attention.py"),
        ],
        outputs=[
            bone("evolution output", "部署工具/strategy_evolution_gate.py", contains="strategy_evolution_latest.json"),
            bone("attention output", "部署工具/decision_attention.py", contains="open_items.json"),
        ],
        portal=[bone("evolution and attention portal", "部署工具/portal_dashboard.py", contains="ATTENTION_JSON")],
        sync=[
            bone("Aliyun evolution run", "部署工具/aliyun_analysis_refresh.sh", contains="strategy_evolution_gate.py"),
            bone("reverse sync evolution report", "部署工具/sync_aliyun_reports_to_tencent.py", contains="strategy_evolution_latest"),
        ],
        tests=[bone("strategy decision packet tests", "tests/test_strategy_decision_packet.py")],
        validation_blockers=["Needs fresh observation samples after staged restart."],
    ),
    module_spec(
        item_id="P2-D",
        priority="P2",
        name="Engineering governance",
        objective="Git ledger guard, attention ack script/API, portal ack path, and deploy wiring are present.",
        inputs=[bone("attention item source", "research_memory/attention/open_items.json")],
        main=[
            bone("git change guard", "部署工具/git_change_guard.py"),
            bone("attention ack script", "部署工具/acknowledge_attention_items.py"),
            bone("attention API server", "部署工具/attention_api_server.py"),
        ],
        outputs=[
            bone("acknowledgement JSONL", "部署工具/acknowledge_attention_items.py", contains="acknowledgements.jsonl"),
            bone("git ledger requirement", "部署工具/git_change_guard.py", contains="CHANGELOG.md"),
        ],
        portal=[bone("browser ack button", "部署工具/portal_dashboard.py", contains="/api/attention/ack")],
        sync=[
            bone("attention API upload", "部署工具/deploy_shadow_aliyun.py", contains="attention_api_server.py"),
            bone("attention script release", "部署工具/release_manager.py", contains="acknowledge_attention_items.py"),
        ],
        tests=[bone("attention API tests", "tests/test_attention_api_server.py")],
        validation_blockers=["Browser-side ack still needs server-side service verification after staged portal restart."],
        post_launch_backlog=["Remote CI enforcement for git_change_guard.py."],
    ),
    module_spec(
        item_id="FINAL-ZERO-RUN",
        priority="FINAL",
        name="Clean old dirty data and start from zero",
        objective="Only after all skeleton and staged validations pass, archive/reset old testnet signal data and run fresh.",
        inputs=[bone("reset tool", "部署工具/runtime_data_reset.py", contains="Archive and reset")],
        main=[bone("reset apply switch", "部署工具/runtime_data_reset.py", contains="--apply")],
        outputs=[bone("reset receipt", "部署工具/runtime_data_reset.py", contains="testnet_data_reset_latest.json")],
        portal=[bone("long-term matrix records final gate", "部署工具/long_term_skeleton_review.py", contains="FINAL-ZERO-RUN")],
        sync=[bone("reset tool release", "部署工具/release_manager.py", contains="runtime_data_reset.py")],
        tests=[bone("reset preview mode", "部署工具/runtime_data_reset.py", contains="only previews")],
        validation_blockers=["Must wait until all P0/P1/P2 skeleton and staged validation items pass."],
    ),
]


def is_deployed_flat_root(root: Path) -> bool:
    """Tencent/Aliyun releases flatten tool and strategy files into root."""
    return any((root / name).exists() for name in ("long_term_skeleton_review.py", "portal_dashboard.py"))


def deployed_path_aliases(rel_path: str) -> list[str]:
    rel = rel_path.replace("\\", "/")
    aliases = [rel]
    prefix_aliases = (
        ("部署工具/systemd/", "systemd/"),
        ("部署工具/", ""),
        ("策略文件/", ""),
        ("交易客户端/", ""),
    )
    for prefix, replacement in prefix_aliases:
        if rel.startswith(prefix):
            aliases.append(replacement + rel[len(prefix) :])
    out: list[str] = []
    seen = set()
    for item in aliases:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def resolve_existing_path(root: Path, rel_path: str) -> tuple[Path | None, str | None]:
    candidates = deployed_path_aliases(rel_path) if is_deployed_flat_root(root) else [rel_path]
    for candidate in candidates:
        path = root / candidate
        if path.exists() and path.is_file():
            return path, candidate
    return None, None


def read_resolved_text(path: Path | None) -> str | None:
    if not path:
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def evaluate_bone(root: Path, item: dict[str, Any]) -> dict[str, Any]:
    rel_path = str(item.get("path") or "")
    markers = list(item.get("contains") or [])
    path, resolved_rel_path = resolve_existing_path(root, rel_path)
    exists = path is not None
    deployed_repo_only_bone = False
    if not exists and is_deployed_flat_root(root):
        exists = True
        deployed_repo_only_bone = True
        resolved_rel_path = rel_path
    text = read_resolved_text(path) if markers and path else None
    missing_markers = [marker for marker in markers if not text or marker not in text]
    ready = exists and (deployed_repo_only_bone or not missing_markers)
    if ready and deployed_repo_only_bone:
        detail = "repo-only source/test bone; verified by local skeleton review before deploy"
    elif ready:
        detail = "ok"
    elif not exists:
        candidates = deployed_path_aliases(rel_path) if is_deployed_flat_root(root) else [rel_path]
        detail = "missing file: " + ", ".join(candidates)
    else:
        detail = "missing marker: " + ", ".join(missing_markers)
    return {
        "label": item.get("label") or rel_path,
        "path": rel_path,
        "resolved_path": resolved_rel_path or rel_path,
        "ready": ready,
        "detail": detail,
        "markers": markers,
    }


def evaluate_module(root: Path, spec: dict[str, Any]) -> dict[str, Any]:
    categories: dict[str, Any] = {}
    ready_bones = 0
    total_bones = 0
    missing: list[str] = []
    for category in CATEGORIES:
        rows = [evaluate_bone(root, item) for item in (spec.get("bones", {}).get(category) or [])]
        ready = sum(1 for row in rows if row.get("ready"))
        total = len(rows)
        categories[category] = {
            "ready": ready,
            "total": total,
            "complete": ready == total,
            "items": rows,
        }
        ready_bones += ready
        total_bones += total
        missing.extend(f"{category}:{row.get('label')}" for row in rows if not row.get("ready"))
    blockers = list(spec.get("validation_blockers") or [])
    if missing:
        status = "missing_skeleton"
        next_action = "fill_missing_bones"
    elif blockers:
        status = "blocked_by_staged_validation"
        next_action = "run_staged_validation_or_collect_fresh_data"
    else:
        status = "skeleton_ready"
        next_action = "keep_monitoring"
    return {
        "id": spec.get("id"),
        "priority": spec.get("priority"),
        "name": spec.get("name"),
        "objective": spec.get("objective"),
        "status": status,
        "next_action": next_action,
        "ready_bones": ready_bones,
        "total_bones": total_bones,
        "ready_pct": round((ready_bones / total_bones * 100) if total_bones else 100.0, 1),
        "categories": categories,
        "missing_bones": missing,
        "validation_blockers": blockers,
        "post_launch_backlog": list(spec.get("post_launch_backlog") or []),
    }


def build_payload(root: Path = ROOT, specs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    modules = [evaluate_module(root, spec) for spec in (specs or DEFAULT_SPECS)]
    total = len(modules)
    status_counts: dict[str, int] = {}
    priority_counts: dict[str, dict[str, int]] = {}
    ready_bones = sum(int(item.get("ready_bones") or 0) for item in modules)
    total_bones = sum(int(item.get("total_bones") or 0) for item in modules)
    for item in modules:
        status = str(item.get("status") or "unknown")
        priority = str(item.get("priority") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        priority_counts.setdefault(priority, {})
        priority_counts[priority][status] = priority_counts[priority].get(status, 0) + 1
    if status_counts.get("missing_skeleton"):
        overall_status = "missing_skeleton"
        next_action = "fill_missing_skeleton_bones_only"
    elif status_counts.get("blocked_by_staged_validation"):
        overall_status = "blocked_by_staged_validation"
        next_action = "run_staged_validation_then_zero_reset"
    else:
        overall_status = "skeleton_ready"
        next_action = "ready_for_zero_reset"
    blockers = [
        {"id": item.get("id"), "name": item.get("name"), "blocker": blocker}
        for item in modules
        for blocker in (item.get("validation_blockers") or [])
    ]
    backlog = [
        {"id": item.get("id"), "name": item.get("name"), "item": backlog_item}
        for item in modules
        for backlog_item in (item.get("post_launch_backlog") or [])
    ]
    return {
        "generated_at": datetime.now(CST).isoformat(timespec="seconds"),
        "mode": "bone_only_no_service_no_binance",
        "root": str(root),
        "status": overall_status,
        "next_action": next_action,
        "summary": {
            "modules": total,
            "skeleton_ready": status_counts.get("skeleton_ready", 0),
            "missing_skeleton": status_counts.get("missing_skeleton", 0),
            "blocked_by_staged_validation": status_counts.get("blocked_by_staged_validation", 0),
            "ready_bones": ready_bones,
            "total_bones": total_bones,
            "ready_pct": round((ready_bones / total_bones * 100) if total_bones else 100.0, 1),
            "validation_blockers": len(blockers),
            "post_launch_backlog": len(backlog),
        },
        "status_counts": status_counts,
        "priority_counts": priority_counts,
        "modules": modules,
        "validation_blockers": blockers,
        "post_launch_backlog": backlog,
        "rule": "Do not expand detail work before the skeleton and staged validation gates pass.",
    }


def render_md(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    lines = [
        "# Long-term Skeleton Review",
        "",
        f"- Generated: `{payload.get('generated_at')}`",
        f"- Mode: `{payload.get('mode')}`",
        f"- Status: `{payload.get('status')}`",
        f"- Next action: `{payload.get('next_action')}`",
        f"- Modules: `{summary.get('modules')}`",
        f"- Bones ready: `{summary.get('ready_bones')}/{summary.get('total_bones')}` ({summary.get('ready_pct')}%)",
        f"- Missing skeleton modules: `{summary.get('missing_skeleton')}`",
        f"- Staged validation blockers: `{summary.get('validation_blockers')}`",
        f"- Post-launch backlog items: `{summary.get('post_launch_backlog')}`",
        "",
        "## Module Matrix",
        "",
        "| Priority | ID | Module | Status | Bones | Next | Blockers | Backlog |",
        "| --- | --- | --- | --- | ---: | --- | ---: | ---: |",
    ]
    for item in payload.get("modules") or []:
        lines.append(
            "| {priority} | {item_id} | {name} | {status} | {ready}/{total} | {next_action} | {blockers} | {backlog} |".format(
                priority=item.get("priority") or "",
                item_id=item.get("id") or "",
                name=item.get("name") or "",
                status=item.get("status") or "",
                ready=item.get("ready_bones") or 0,
                total=item.get("total_bones") or 0,
                next_action=item.get("next_action") or "",
                blockers=len(item.get("validation_blockers") or []),
                backlog=len(item.get("post_launch_backlog") or []),
            )
        )
    lines.extend(["", "## Missing Bones", ""])
    missing_any = False
    for item in payload.get("modules") or []:
        missing = item.get("missing_bones") or []
        if not missing:
            continue
        missing_any = True
        lines.append(f"- `{item.get('id')}` {item.get('name')}: " + "; ".join(str(row) for row in missing))
    if not missing_any:
        lines.append("- none")
    lines.extend(["", "## Staged Validation Blockers", ""])
    if payload.get("validation_blockers"):
        for row in payload.get("validation_blockers") or []:
            lines.append(f"- `{row.get('id')}` {row.get('name')}: {row.get('blocker')}")
    else:
        lines.append("- none")
    lines.extend(["", "## Post-launch Backlog", ""])
    if payload.get("post_launch_backlog"):
        for row in payload.get("post_launch_backlog") or []:
            lines.append(f"- `{row.get('id')}` {row.get('name')}: {row.get('item')}")
    else:
        lines.append("- none")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build bone-only long-term skeleton review")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--runtime-dir", default=str(ROOT / "runtime"))
    parser.add_argument("--reports-dir", default=str(ROOT / "reports"))
    args = parser.parse_args(argv)

    payload = build_payload(Path(args.root))
    runtime_dir = Path(args.runtime_dir)
    reports_dir = Path(args.reports_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "long_term_skeleton_latest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (reports_dir / "long_term_skeleton_latest.md").write_text(render_md(payload), encoding="utf-8")
    print(json.dumps(payload.get("summary") or {}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
