"""Summarize replay/fill readiness from existing offline reports.

Report-only. This script does not query Binance, start services, or recalculate
trades. It reads the latest research-store, rollout, and truth-ledger JSON
artifacts and tells the operator whether post-ingest replay evidence is ready
for a continue/narrow/rollback review.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
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


@dataclass(frozen=True)
class ReadinessThresholds:
    min_rollout_paired_trades: int = 10
    min_rollout_completion_rate: float = 0.80
    min_recovery_completion_rate: float = 0.80
    min_depth_symbols: int = 10
    min_depth_snapshots: int = 50


ROLLOUT_CONTEXT_GAP_STATUSES = {
    "paper_missing_open_context",
    "paper_context_timeframe_gap",
    "missing_open",
    "missing_atr",
    "missing_time",
    "missing_entry_price",
    "missing_quantity",
    "unsupported_timeframe",
}
ROLLOUT_DATA_GAP_STATUSES = {
    "missing_kline_data",
    "missing_bars",
    "missing_depth_data",
    "depth_snapshot_unavailable",
}


def now_cst() -> datetime:
    return datetime.now(CST)


def read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def to_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except Exception:
        return default


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def completion_rate(completed: int, paired: int) -> float:
    return float(completed) / float(paired) if paired > 0 else 0.0


def component(
    name: str,
    status: str,
    ready: bool,
    category: str,
    detail: str,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "ready": ready,
        "category": category,
        "detail": detail,
        "metrics": metrics or {},
    }


def research_store_readiness(payload: Any, thresholds: ReadinessThresholds) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return component(
            "research_store",
            "report_gap",
            False,
            "report_gap",
            "research_store_summary_latest.json missing or unreadable",
        )
    kline_acceptance = payload.get("kline_acceptance") if isinstance(payload.get("kline_acceptance"), dict) else {}
    depth_rows = payload.get("depth_coverage") if isinstance(payload.get("depth_coverage"), list) else []
    depth_symbols = sum(1 for row in depth_rows if to_int((row or {}).get("snapshots")) > 0)
    depth_snapshots = sum(to_int((row or {}).get("snapshots")) for row in depth_rows if isinstance(row, dict))
    kline_ok = bool(kline_acceptance.get("target_met"))
    depth_ok = depth_symbols >= thresholds.min_depth_symbols and depth_snapshots >= thresholds.min_depth_snapshots
    metrics = {
        "kline_status": kline_acceptance.get("status") or "missing",
        "kline_target_met": kline_ok,
        "kline_required": kline_acceptance.get("key_intervals") or [],
        "kline_missing_intervals": kline_acceptance.get("missing_intervals") or [],
        "kline_gap_intervals": kline_acceptance.get("gap_intervals") or [],
        "depth_symbols": depth_symbols,
        "depth_snapshots": depth_snapshots,
        "min_depth_symbols": thresholds.min_depth_symbols,
        "min_depth_snapshots": thresholds.min_depth_snapshots,
    }
    gaps: list[str] = []
    if not kline_ok:
        gaps.append(f"kline:{metrics['kline_status']}")
    if not depth_ok:
        gaps.append(f"depth:{depth_symbols}/{thresholds.min_depth_symbols} symbols, {depth_snapshots}/{thresholds.min_depth_snapshots} snapshots")
    return component(
        "research_store",
        "ok" if not gaps else "data_gap",
        not gaps,
        "ready" if not gaps else "data_gap",
        "research store coverage is sufficient" if not gaps else "; ".join(gaps),
        metrics,
    )


def rollout_readiness(payload: Any, name: str, thresholds: ReadinessThresholds, window: str = "72h") -> dict[str, Any]:
    if not isinstance(payload, dict):
        return component(name, "report_gap", False, "report_gap", f"{name} rollout report missing or unreadable")
    replay = payload.get("replay_fill_comparison") if isinstance(payload.get("replay_fill_comparison"), dict) else {}
    row = replay.get(window) if isinstance(replay.get(window), dict) else {}
    paired = to_int(row.get("paired_trades"))
    completed = to_int(row.get("completed"))
    rate = to_float(row.get("completion_rate"), completion_rate(completed, paired))
    status_counts = row.get("status_counts") if isinstance(row.get("status_counts"), dict) else {}
    metrics = {
        "window": window,
        "paired_trades": paired,
        "completed": completed,
        "completion_rate": round(rate, 4),
        "min_paired_trades": thresholds.min_rollout_paired_trades,
        "min_completion_rate": thresholds.min_rollout_completion_rate,
        "status_counts": status_counts,
        "pnl_delta_usdt": to_float(row.get("pnl_delta_usdt")),
        "order_book_fill_count": to_int(row.get("order_book_fill_count")),
    }
    if paired < thresholds.min_rollout_paired_trades:
        return component(
            name,
            "waiting_for_samples",
            False,
            "sample_gap",
            f"{window} paired trades {paired}/{thresholds.min_rollout_paired_trades}",
            metrics,
        )
    if rate < thresholds.min_rollout_completion_rate:
        incomplete_statuses = {
            str(key): to_int(value)
            for key, value in status_counts.items()
            if str(key) != "complete" and to_int(value) > 0
        }
        incomplete_total = sum(incomplete_statuses.values())
        context_gap_total = sum(
            value for key, value in incomplete_statuses.items() if key in ROLLOUT_CONTEXT_GAP_STATUSES
        )
        data_gap_total = sum(
            value for key, value in incomplete_statuses.items() if key in ROLLOUT_DATA_GAP_STATUSES
        )
        if incomplete_total > 0 and data_gap_total == 0 and context_gap_total == incomplete_total:
            return component(
                name,
                "context_gap",
                False,
                "context_gap",
                f"{window} replay context completion {rate:.1%} below {thresholds.min_rollout_completion_rate:.0%}; collect fresh contextual paired samples",
                metrics,
            )
        return component(
            name,
            "data_gap",
            False,
            "data_gap",
            f"{window} replay completion {rate:.1%} below {thresholds.min_rollout_completion_rate:.0%}",
            metrics,
        )
    return component(name, "ok", True, "ready", f"{window} replay evidence ready", metrics)


def recovery_readiness(payload: Any, thresholds: ReadinessThresholds) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return component("recovery_replay", "report_gap", False, "report_gap", "strategy truth report missing or unreadable")
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    replay = payload.get("recovery_bar_replay_evidence") if isinstance(payload.get("recovery_bar_replay_evidence"), dict) else {}
    positions = replay.get("positions") if isinstance(replay.get("positions"), list) else []
    total = len(positions) or to_int(summary.get("total_recovery_positions"))
    if total <= 0:
        return component(
            "recovery_replay",
            "not_applicable",
            True,
            "ready",
            "no recovery positions",
            {"total_positions": 0, "completion_rate": 1.0},
        )
    if not positions and not replay:
        return component(
            "recovery_replay",
            "report_gap",
            False,
            "report_gap",
            "recovery replay summary missing while recovery positions exist",
            {"total_positions": total, "completion_rate": 0.0},
        )
    data_gap = to_int(replay.get("data_gap_positions"))
    if not data_gap:
        action_counts = replay.get("action_counts") if isinstance(replay.get("action_counts"), dict) else {}
        data_gap = to_int(action_counts.get("replay_data_gap"))
    complete = max(0, total - data_gap)
    rate = completion_rate(complete, total)
    metrics = {
        "total_positions": total,
        "completed_positions": complete,
        "data_gap_positions": data_gap,
        "completion_rate": round(rate, 4),
        "min_completion_rate": thresholds.min_recovery_completion_rate,
        "order_book_fill_count": to_int(replay.get("order_book_fill_count")),
        "action_counts": replay.get("action_counts") if isinstance(replay.get("action_counts"), dict) else {},
    }
    if rate < thresholds.min_recovery_completion_rate:
        return component(
            "recovery_replay",
            "data_gap",
            False,
            "data_gap",
            f"recovery replay completion {rate:.1%} below {thresholds.min_recovery_completion_rate:.0%}",
            metrics,
        )
    return component("recovery_replay", "ok", True, "ready", "recovery replay evidence ready", metrics)


def overall_status(components: list[dict[str, Any]]) -> tuple[str, str, list[dict[str, Any]]]:
    blockers = [item for item in components if not bool(item.get("ready"))]
    if not blockers:
        return "ready_for_operator_review", "review_continue_narrow_rollback", []
    categories = {str(item.get("category") or "") for item in blockers}
    if "report_gap" in categories:
        return "report_gap", "regenerate_missing_reports", blockers
    if "data_gap" in categories:
        return "data_gap", "run_staged_kline_depth_ingest_then_replay_review", blockers
    if "context_gap" in categories:
        return "context_gap", "collect_fresh_contextual_paired_samples", blockers
    if "sample_gap" in categories:
        return "waiting_for_samples", "collect_post_refresh_samples", blockers
    return "blocked", "inspect_replay_readiness_blockers", blockers


def build_payload(
    *,
    research_store: Any,
    a_v11_rollout: Any,
    b_v16_rollout: Any,
    truth: Any,
    thresholds: ReadinessThresholds,
) -> dict[str, Any]:
    components = [
        research_store_readiness(research_store, thresholds),
        rollout_readiness(a_v11_rollout, "a_v11_rollout", thresholds),
        rollout_readiness(b_v16_rollout, "b_v16_rollout", thresholds),
        recovery_readiness(truth, thresholds),
    ]
    status, next_action, blockers = overall_status(components)
    return {
        "generated_at": now_cst().isoformat(timespec="seconds"),
        "status": status,
        "priority": "P1" if status != "ready_for_operator_review" else "P2",
        "next_action": next_action,
        "automation": "disabled_report_only",
        "thresholds": asdict(thresholds),
        "components": components,
        "blockers": blockers,
        "summary": {
            "ready_components": sum(1 for item in components if item.get("ready")),
            "total_components": len(components),
            "blockers": len(blockers),
            "data_gap_blockers": sum(1 for item in blockers if item.get("category") == "data_gap"),
            "context_gap_blockers": sum(1 for item in blockers if item.get("category") == "context_gap"),
            "sample_gap_blockers": sum(1 for item in blockers if item.get("category") == "sample_gap"),
            "report_gap_blockers": sum(1 for item in blockers if item.get("category") == "report_gap"),
        },
        "note": "Reads existing local/mirrored JSON reports only; no Binance API call, service start, replay recomputation, rollout, or rollback.",
    }


def md_table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "_No rows._"
    out = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        out.append("| " + " | ".join(str(value) for value in row) + " |")
    return "\n".join(out)


def render_md(payload: dict[str, Any]) -> str:
    rows = [
        [
            item.get("name"),
            item.get("status"),
            "yes" if item.get("ready") else "no",
            item.get("category"),
            item.get("detail"),
        ]
        for item in payload.get("components", [])
    ]
    blocker_rows = [
        [item.get("name"), item.get("status"), item.get("detail")]
        for item in payload.get("blockers", [])
    ]
    return "\n\n".join(
        [
            "# Replay Readiness Review",
            f"- Generated: `{payload.get('generated_at')}`",
            f"- Status: `{payload.get('status')}`",
            f"- Priority: `{payload.get('priority')}`",
            f"- Next action: `{payload.get('next_action')}`",
            f"- Automation: `{payload.get('automation')}`",
            "",
            "## Components",
            md_table(["Component", "Status", "Ready", "Category", "Detail"], rows),
            "",
            "## Blockers",
            md_table(["Component", "Status", "Detail"], blocker_rows),
            "",
            "## Note",
            str(payload.get("note") or ""),
        ]
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build replay/fill readiness review from existing report JSON.")
    parser.add_argument("--runtime-dir", default=str(ROOT / "runtime"))
    parser.add_argument("--reports-dir", default=str(ROOT / "reports"))
    parser.add_argument("--research-json", default="")
    parser.add_argument("--a-rollout-json", default="")
    parser.add_argument("--b-rollout-json", default="")
    parser.add_argument("--truth-json", default="")
    parser.add_argument("--min-rollout-paired-trades", type=int, default=ReadinessThresholds.min_rollout_paired_trades)
    parser.add_argument("--min-rollout-completion-rate", type=float, default=ReadinessThresholds.min_rollout_completion_rate)
    parser.add_argument("--min-recovery-completion-rate", type=float, default=ReadinessThresholds.min_recovery_completion_rate)
    parser.add_argument("--min-depth-symbols", type=int, default=ReadinessThresholds.min_depth_symbols)
    parser.add_argument("--min-depth-snapshots", type=int, default=ReadinessThresholds.min_depth_snapshots)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    runtime_dir = Path(args.runtime_dir)
    reports_dir = Path(args.reports_dir)
    thresholds = ReadinessThresholds(
        min_rollout_paired_trades=max(0, int(args.min_rollout_paired_trades)),
        min_rollout_completion_rate=max(0.0, min(1.0, float(args.min_rollout_completion_rate))),
        min_recovery_completion_rate=max(0.0, min(1.0, float(args.min_recovery_completion_rate))),
        min_depth_symbols=max(0, int(args.min_depth_symbols)),
        min_depth_snapshots=max(0, int(args.min_depth_snapshots)),
    )
    research_json = Path(args.research_json) if args.research_json else runtime_dir / "research_store_summary_latest.json"
    a_rollout_json = Path(args.a_rollout_json) if args.a_rollout_json else runtime_dir / "a_v11_rollout_review_latest.json"
    b_rollout_json = Path(args.b_rollout_json) if args.b_rollout_json else runtime_dir / "b_v16_rollout_review_latest.json"
    truth_json = Path(args.truth_json) if args.truth_json else runtime_dir / "strategy_truth_latest.json"
    payload = build_payload(
        research_store=read_json(research_json),
        a_v11_rollout=read_json(a_rollout_json),
        b_v16_rollout=read_json(b_rollout_json),
        truth=read_json(truth_json),
        thresholds=thresholds,
    )
    write_json(runtime_dir / "replay_readiness_latest.json", payload)
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "replay_readiness_latest.md").write_text(render_md(payload), encoding="utf-8")
    print(json.dumps({"status": payload["status"], "next_action": payload["next_action"], "blockers": len(payload["blockers"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
