"""Audit live event-store rows through the shared replay gate taxonomy.

This is a low-risk N2 bridge: it does not change live strategy behavior. It
measures how much of live OPEN/SIGNAL/OPEN_SKIPPED/OPEN_FAILED flow can already
be explained by `core.replay`, so later pure strategy gates can be extracted
without guessing where current samples are rejected.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
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
ROOT = SCRIPT_DIR.parent if (SCRIPT_DIR.parent / "PROJECT_STATE.md").exists() else SCRIPT_DIR
sys.path.insert(0, str(ROOT))

from core.replay import ReplayEvent, evaluate_observed_gate


CST = timezone(timedelta(hours=8))
OPEN_FLOW_TYPES = {"SIGNAL", "OPEN", "OPEN_SKIPPED", "OPEN_FAILED"}
UNKNOWN_GATES = {"", "unknown", "unknown_gate", "none", "-"}


def now_cst() -> datetime:
    return datetime.now(CST)


def json_dump(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def query_events(db: Path, days: int, limit: int) -> list[dict[str, Any]]:
    cutoff = (now_cst() - timedelta(days=days)).strftime("%Y-%m-%d")
    if not db.exists():
        return []
    with sqlite3.connect(db) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """
            select id, ts, strategy, symbol, event_type, category, side, score,
                   stage, layer, reason, source, payload_json
            from events
            where substr(ts, 1, 10) >= ?
              and event_type in ('SIGNAL', 'OPEN', 'OPEN_SKIPPED', 'OPEN_FAILED', 'CLOSE', 'FORCED_CLOSE', 'CLOSE_FAILED', 'FORCED_CLOSE_FAILED')
              and strategy in ('A/v11', 'B/v16', 'C/v14')
            order by id desc
            limit ?
            """,
            (cutoff, int(limit)),
        ).fetchall()
    return [dict(row) for row in rows]


def pct(num: int | float, den: int | float) -> float:
    return round((float(num) / float(den) * 100.0), 2) if den else 0.0


def top_items(counter: Counter[str], limit: int = 8) -> list[dict[str, Any]]:
    return [{"name": k, "count": v} for k, v in counter.most_common(limit)]


def build_payload(db: Path, days: int, limit: int) -> dict[str, Any]:
    rows = query_events(db, days, limit)
    by_strategy: dict[str, dict[str, Any]] = {}
    totals = Counter()
    latest_ts = ""

    for row in reversed(rows):
        event = ReplayEvent.from_event_store_row(row)
        decision = evaluate_observed_gate(event)
        strategy = event.strategy or "unknown"
        latest_ts = max(latest_ts, event.ts or "")
        bucket = by_strategy.setdefault(
            strategy,
            {
                "strategy": strategy,
                "events": 0,
                "open_flow_events": 0,
                "accepted_opens": 0,
                "signals": 0,
                "rejected": 0,
                "execution_failed": 0,
                "close_events": 0,
                "unknown_gate": 0,
                "_event_types": Counter(),
                "_decisions": Counter(),
                "_gates": Counter(),
                "_unknown_examples": [],
            },
        )
        bucket["events"] += 1
        bucket["_event_types"][event.event_type.value] += 1
        bucket["_decisions"][decision.decision] += 1
        bucket["_gates"][decision.gate or "unknown"] += 1
        totals["events"] += 1
        totals[f"event_type:{event.event_type.value}"] += 1
        totals[f"decision:{decision.decision}"] += 1

        if event.event_type.value in OPEN_FLOW_TYPES:
            bucket["open_flow_events"] += 1
            totals["open_flow_events"] += 1
            if decision.gate.lower() in UNKNOWN_GATES:
                bucket["unknown_gate"] += 1
                totals["unknown_gate"] += 1
                examples = bucket["_unknown_examples"]
                if len(examples) < 5:
                    examples.append(
                        {
                            "ts": event.ts,
                            "symbol": event.symbol,
                            "event_type": event.event_type.value,
                            "reason": event.reason,
                        }
                    )
        if decision.decision == "accepted_open":
            bucket["accepted_opens"] += 1
            totals["accepted_opens"] += 1
        elif decision.decision == "candidate":
            bucket["signals"] += 1
            totals["signals"] += 1
        elif decision.decision == "rejected":
            bucket["rejected"] += 1
            totals["rejected"] += 1
        elif decision.decision == "execution_failed":
            bucket["execution_failed"] += 1
            totals["execution_failed"] += 1
        elif decision.decision == "close_observed":
            bucket["close_events"] += 1
            totals["close_events"] += 1

    strategy_rows = []
    for name in sorted(by_strategy):
        row = by_strategy[name]
        open_flow = int(row["open_flow_events"])
        unknown = int(row["unknown_gate"])
        row["gate_coverage_pct"] = pct(open_flow - unknown, open_flow)
        row["event_types"] = top_items(row.pop("_event_types"))
        row["decisions"] = top_items(row.pop("_decisions"))
        row["top_gates"] = top_items(row.pop("_gates"))
        row["unknown_examples"] = row.pop("_unknown_examples")
        strategy_rows.append(row)

    open_flow_total = int(totals["open_flow_events"])
    unknown_total = int(totals["unknown_gate"])
    summary = {
        "events": int(totals["events"]),
        "open_flow_events": open_flow_total,
        "signals": int(totals["signals"]),
        "accepted_opens": int(totals["accepted_opens"]),
        "rejected": int(totals["rejected"]),
        "execution_failed": int(totals["execution_failed"]),
        "close_events": int(totals["close_events"]),
        "unknown_gate": unknown_total,
        "gate_coverage_pct": pct(open_flow_total - unknown_total, open_flow_total),
        "latest_ts": latest_ts,
    }
    summary["status"] = (
        "ok"
        if summary["gate_coverage_pct"] >= 90
        else "warn"
        if summary["gate_coverage_pct"] >= 75
        else "bad"
    )
    summary["next_action"] = (
        "continue_extracting_pure_live_gates"
        if summary["status"] == "ok"
        else "fill_missing_stage_layer_before_gate_extraction"
    )
    return {
        "generated_at": now_cst().isoformat(),
        "db": str(db),
        "days": days,
        "limit": limit,
        "summary": summary,
        "strategies": strategy_rows,
    }


def build_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    lines = [
        "# Replay Gate Audit",
        "",
        f"- Generated: `{payload.get('generated_at')}`",
        f"- DB: `{payload.get('db')}`",
        f"- Window: `{payload.get('days')}` day(s), limit `{payload.get('limit')}` rows",
        f"- Status: `{summary.get('status')}`",
        f"- Open-flow events: `{summary.get('open_flow_events')}`",
        f"- Gate coverage: `{summary.get('gate_coverage_pct')}%`",
        f"- Unknown gate rows: `{summary.get('unknown_gate')}`",
        f"- Next action: `{summary.get('next_action')}`",
        "",
        "| Strategy | Open flow | Opens | Signals | Rejected | Failed | Unknown gate | Coverage | Top gates |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in payload.get("strategies") or []:
        gates = ", ".join(f"{g['name']}:{g['count']}" for g in row.get("top_gates") or [])
        lines.append(
            "| {strategy} | {open_flow_events} | {accepted_opens} | {signals} | {rejected} | "
            "{execution_failed} | {unknown_gate} | {gate_coverage_pct}% | {gates} |".format(
                gates=gates or "-",
                **row,
            )
        )
    return "\n".join(lines) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit event-store rows through core.replay gate taxonomy")
    default_db = ROOT / "server_logs_tencent" / "runtime" / "event_store.sqlite3"
    if not default_db.exists():
        default_db = ROOT / "runtime" / "event_store.sqlite3"
    parser.add_argument("--db", default=str(default_db))
    parser.add_argument("--runtime-dir", default=str(ROOT / "runtime"))
    parser.add_argument("--reports-dir", default=str(ROOT / "reports"))
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--limit", type=int, default=20000)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = build_payload(Path(args.db), args.days, args.limit)
    runtime_dir = Path(args.runtime_dir)
    reports_dir = Path(args.reports_dir)
    json_dump(runtime_dir / "replay_gate_audit_latest.json", payload)
    (reports_dir / "replay_gate_audit_latest.md").parent.mkdir(parents=True, exist_ok=True)
    (reports_dir / "replay_gate_audit_latest.md").write_text(build_markdown(payload), encoding="utf-8")
    print(
        json.dumps(
            {
                "events": payload["summary"]["events"],
                "open_flow_events": payload["summary"]["open_flow_events"],
                "gate_coverage_pct": payload["summary"]["gate_coverage_pct"],
                "status": payload["summary"]["status"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
