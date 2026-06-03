"""Build a compact read-only review for active rollback-watch decisions."""

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


def render_risk(value: Any) -> str:
    if isinstance(value, list):
        return "; ".join(str(v) for v in value) or "-"
    if isinstance(value, dict):
        items = value.get("items")
        if isinstance(items, list):
            return "; ".join(str(v) for v in items) or "-"
        return json.dumps(value, ensure_ascii=False)
    return str(value or "-")


def render_path(value: Any) -> str:
    if isinstance(value, list):
        return "; ".join(str(v) for v in value) or "-"
    return str(value or "-")


def action_for(item: dict[str, Any]) -> str:
    priority = str(item.get("priority") or "")
    status = str(item.get("status") or "")
    q24 = item.get("quality_24h") or {}
    q72 = item.get("quality_72h") or {}
    pnl24 = as_float(q24.get("realized_pnl_after_cost"))
    pnl72 = as_float(q72.get("realized_pnl_after_cost"))
    closed72 = int(q72.get("closed_samples") or 0)
    forced = as_float(q24.get("forced_close_rate"))
    if priority == "P0" or status == "rollback_required":
        return "prepare_manual_rollback"
    if closed72 >= 50 and pnl72 <= -120:
        return "prepare_rollback_review"
    if pnl24 <= -80 or forced >= 0.10:
        return "pause_expansion_review_quality"
    return "continue_observation"


def extract_item(decision: dict[str, Any]) -> dict[str, Any] | None:
    if decision.get("priority") not in {"P0", "P1"}:
        return None
    if decision.get("status") not in {"rollback_watch", "rollback_required"}:
        return None
    windows = ((decision.get("post_approval_live") or {}).get("windows") or {})
    w24 = windows.get("24h") or {}
    w72 = windows.get("72h") or {}
    item = {
        "candidate_id": decision.get("candidate_id"),
        "strategy": decision.get("strategy"),
        "priority": decision.get("priority"),
        "status": decision.get("status"),
        "recommended_action": decision.get("recommended_action"),
        "blockers": decision.get("blockers") or [],
        "approved_at": ((decision.get("post_approval_live") or {}).get("approved_at") or ""),
        "window_24h": {
            "opens": int(w24.get("opens") or 0),
            "closed_samples": int((w24.get("quality") or {}).get("closed_samples") or 0),
            "forced_closes": int(w24.get("forced_closes") or 0),
            "open_failed": int(w24.get("open_failed") or 0),
            "close_failed": int(w24.get("close_failed") or 0),
            "regime": (w24.get("regime") or {}).get("label") or "",
        },
        "quality_24h": w24.get("quality") or {},
        "quality_72h": w72.get("quality") or {},
        "account_risk": decision.get("account_risk") or {},
        "decision_packet": decision.get("decision_packet") or {},
    }
    item["action"] = action_for(item)
    return item


def build_payload(evolution_path: Path) -> dict[str, Any]:
    payload = read_json(evolution_path)
    decisions = payload.get("decisions") if isinstance(payload, dict) else []
    items = [item for d in decisions if isinstance(d, dict) for item in [extract_item(d)] if item]
    counts: dict[str, int] = {}
    for item in items:
        action = str(item.get("action") or "unknown")
        counts[action] = counts.get(action, 0) + 1
    worst = min(
        items,
        key=lambda x: as_float((x.get("quality_24h") or {}).get("realized_pnl_after_cost")),
        default={},
    )
    return {
        "generated_at": datetime.now(CST).isoformat(timespec="seconds"),
        "source": str(evolution_path),
        "summary": {
            "items": len(items),
            "p0": sum(1 for item in items if item.get("priority") == "P0"),
            "p1": sum(1 for item in items if item.get("priority") == "P1"),
            "actions": counts,
            "worst_candidate": worst.get("candidate_id") or "",
            "worst_pnl_after_cost_24h": as_float((worst.get("quality_24h") or {}).get("realized_pnl_after_cost")) if worst else 0.0,
            "decision_packets": sum(1 for item in items if item.get("decision_packet")),
        },
        "items": items,
    }


def render_md(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    lines = [
        "# Rollback Watch Review",
        "",
        f"- Generated: `{payload.get('generated_at')}`",
        f"- Active P0/P1 rollback-watch items: `{int(summary.get('items') or 0)}`",
        f"- Items with decision packet: `{int(summary.get('decision_packets') or 0)}`",
        f"- Worst 24h after-cost PnL: `{summary.get('worst_candidate') or '-'} {as_float(summary.get('worst_pnl_after_cost_24h')):+.2f} USDT`",
        "",
        "## Items",
        "",
        "| Priority | Strategy | Candidate | Maturity | 24h closed | 24h PnL after cost | Forced rate | Open fail rate | Regime | Action |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for item in payload.get("items") or []:
        q24 = item.get("quality_24h") or {}
        packet = item.get("decision_packet") or {}
        maturity = (packet.get("evidence_maturity") or {}).get("label") or "-"
        lines.append(
            "| {priority} | {strategy} | {candidate} | {maturity} | {closed} | {pnl:+.2f} | {forced:.1%} | {open_failed:.1%} | {regime} | {action} |".format(
                priority=item.get("priority") or "",
                strategy=item.get("strategy") or "",
                candidate=item.get("candidate_id") or "",
                maturity=maturity,
                closed=int(q24.get("closed_samples") or 0),
                pnl=as_float(q24.get("realized_pnl_after_cost")),
                forced=as_float(q24.get("forced_close_rate")),
                open_failed=as_float(q24.get("open_failed_rate")),
                regime=(item.get("window_24h") or {}).get("regime") or "",
                action=item.get("action") or "",
            )
        )
    lines.extend(["", "## Decision Packets", ""])
    for item in payload.get("items") or []:
        packet = item.get("decision_packet") or {}
        if not packet:
            continue
        lines.append(f"### {item.get('candidate_id')}")
        lines.append(f"- Change: {packet.get('change') or '-'}")
        lines.append(f"- Expected advantage: {packet.get('expected_advantage') or '-'}")
        lines.append(f"- Risk: {render_risk(packet.get('risk'))}")
        lines.append(f"- Rollback path: {render_path(packet.get('rollback_path'))}")
        lines.append(f"- Automation: {packet.get('automation') or 'disabled_report_only'}")
        lines.append("")
    lines.extend(["", "## Rule", ""])
    lines.append("- Report-only. No automatic rollback, no parameter change, no order action.")
    lines.append("- `prepare_rollback_review` means evidence is strong enough to prepare an operator rollback decision.")
    lines.append("- `pause_expansion_review_quality` means do not expand further; keep monitoring or manually narrow after review.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build rollback-watch review from strategy evolution output")
    parser.add_argument("--evolution-json", default=str(ROOT / "runtime" / "strategy_evolution_latest.json"))
    parser.add_argument("--runtime-dir", default=str(ROOT / "runtime"))
    parser.add_argument("--reports-dir", default=str(ROOT / "reports"))
    args = parser.parse_args(argv)
    payload = build_payload(Path(args.evolution_json))
    runtime_dir = Path(args.runtime_dir)
    reports_dir = Path(args.reports_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "rollback_watch_review_latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (reports_dir / "rollback_watch_review_latest.md").write_text(render_md(payload), encoding="utf-8")
    print(json.dumps(payload.get("summary") or {}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
