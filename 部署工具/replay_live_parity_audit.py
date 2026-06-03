"""Replay/live same-input parity audit for strategy gates.

Read-only. This script evaluates serialized `strategy_gate_case` payloads from
the event store through `core.strategy_gate_cases` and reports exact pass/mismatch
coverage. Rows without serialized cases are counted as observed-only so the gap
is visible instead of guessed from incomplete historical payloads.
"""

from __future__ import annotations

import argparse
import collections
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent if (SCRIPT_DIR.parent / "PROJECT_STATE.md").exists() else SCRIPT_DIR
sys.path.insert(0, str(ROOT))

from core.replay import ReplayEvent, evaluate_observed_gate, parse_payload
from core.strategy_gate_cases import evaluate_strategy_gate_case


CST = timezone(timedelta(hours=8))
OPEN_FLOW_TYPES = {"SIGNAL", "OPEN", "OPEN_SKIPPED", "OPEN_FAILED"}
SCAN_GATE_STAGES = {
    "confirmation",
    "cooldown",
    "market_data_guard",
    "market_microstructure",
    "position_gate",
    "position_replacement",
    "pre_filter",
    "resonance",
    "risk_gate",
    "score_gate",
    "sector_guard",
    "small_live_stage_guard",
    "tail_guard",
    "threshold",
    "tradability",
}
CASE_LIST_KEYS = ("strategy_gate_cases", "gate_cases", "parity_cases")
CASE_SINGLE_KEYS = ("strategy_gate_case", "gate_case", "parity_case")


def now_cst() -> datetime:
    return datetime.now(CST)


def pct(num: int | float, den: int | float) -> float:
    return round((float(num) / float(den) * 100.0), 2) if den else 0.0


def json_dump(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def query_events(db: Path, days: int, limit: int) -> list[dict[str, Any]]:
    if not db.exists():
        return []
    cutoff = (now_cst() - timedelta(days=days)).strftime("%Y-%m-%d")
    con = sqlite3.connect(db)
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """
            select id, ts, strategy, symbol, event_type, category, side, score,
                   stage, layer, reason, source, payload_json
            from events
            where substr(ts, 1, 10) >= ?
              and event_type in ('SIGNAL', 'OPEN', 'OPEN_SKIPPED', 'OPEN_FAILED')
              and strategy in ('A/v11', 'B/v16', 'C/v14')
            order by id desc
            limit ?
            """,
            (cutoff, int(limit)),
        ).fetchall()
    finally:
        con.close()
    return [dict(row) for row in rows]


def query_sentinel_scans(db: Path, days: int, limit: int) -> list[dict[str, Any]]:
    if not db.exists():
        return []
    cutoff = (now_cst() - timedelta(days=days)).strftime("%Y-%m-%d")
    con = sqlite3.connect(db)
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """
            select id, ts, strategy, symbol, event_type, category,
                   '' as side, null as score, decision_stage as stage,
                   filter_layer as layer, reason, 'sentinel_scans' as source,
                   scan_result, payload_json
            from sentinel_scans
            where date >= ?
              and strategy in ('A/v11', 'B/v16', 'C/v14')
              and decision_stage in (
                'confirmation', 'cooldown', 'market_data_guard', 'market_microstructure',
                'position_gate', 'position_replacement', 'pre_filter', 'resonance',
                'risk_gate', 'score_gate', 'sector_guard', 'small_live_stage_guard',
                'tail_guard', 'threshold', 'tradability'
              )
            order by id desc
            limit ?
            """,
            (cutoff, int(limit)),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        con.close()
    return [dict(row) for row in rows]


def _case_sources(payload: Mapping[str, Any]) -> Iterable[Any]:
    for key in CASE_LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            yield from value
    for key in CASE_SINGLE_KEYS:
        value = payload.get(key)
        if isinstance(value, Mapping):
            yield value
    for nested_key in ("raw", "raw_event", "raw_signal"):
        nested = payload.get(nested_key)
        if isinstance(nested, Mapping):
            yield from _case_sources(nested)


def _expected_from_case_or_row(case: Mapping[str, Any], row: Mapping[str, Any], count: int) -> tuple[dict[str, Any], bool]:
    """Return normalized case and whether expected value was inferred."""
    normalized = dict(case)
    if "expected_allowed" in normalized:
        return normalized, False
    for key in ("observed_allowed", "live_allowed"):
        if key in normalized:
            normalized["expected_allowed"] = bool(normalized.get(key))
            return normalized, False
    event_type = str(row.get("event_type") or "").upper()
    if count == 1 and event_type in OPEN_FLOW_TYPES:
        normalized["expected_allowed"] = event_type in {"SIGNAL", "OPEN"}
        meta = dict(normalized.get("meta") or {})
        meta["expected_allowed_inferred_from_event_type"] = event_type
        normalized["meta"] = meta
        return normalized, True
    return normalized, False


def extract_gate_cases(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    payload = parse_payload(row.get("payload_json"))
    raw_cases = [item for item in _case_sources(payload) if isinstance(item, Mapping)]
    cases: list[dict[str, Any]] = []
    for item in raw_cases:
        case, inferred = _expected_from_case_or_row(item, row, len(raw_cases))
        meta = dict(case.get("meta") or {})
        meta.update(
            {
                "event_id": row.get("id"),
                "ts": row.get("ts"),
                "strategy": row.get("strategy"),
                "symbol": row.get("symbol"),
                "event_type": row.get("event_type"),
                "stage": row.get("stage"),
                "layer": row.get("layer"),
                "expected_inferred": inferred,
            }
        )
        case["meta"] = meta
        cases.append(case)
    return cases


def _empty_strategy(strategy: str) -> dict[str, Any]:
    return {
        "strategy": strategy,
        "open_flow_rows": 0,
        "rows_with_exact_cases": 0,
        "observed_gate_rows": 0,
        "missing_case_rows": 0,
        "gate_cases": 0,
        "passed": 0,
        "mismatched": 0,
        "errors": 0,
        "_gates": collections.Counter(),
        "scan_gate_rows": 0,
        "scan_rows_with_exact_cases": 0,
        "scan_missing_case_rows": 0,
        "scan_gate_cases": 0,
        "scan_passed": 0,
        "scan_mismatched": 0,
        "scan_errors": 0,
        "_scan_gates": collections.Counter(),
    }


def _process_cases(
    *,
    row: Mapping[str, Any],
    cases: list[dict[str, Any]],
    bucket: dict[str, Any],
    totals: collections.Counter,
    mismatch_examples: list[dict[str, Any]],
    error_examples: list[dict[str, Any]],
    strategy: str,
    symbol: str,
    event_type: str,
    row_kind: str,
) -> None:
    prefix = "" if row_kind == "open_flow" else "scan_"
    gate_counter_key = "_gates" if row_kind == "open_flow" else "_scan_gates"
    for case in cases:
        gate = str(case.get("gate") or "")
        bucket[gate_counter_key][gate or "unknown"] += 1
        bucket[f"{prefix}gate_cases"] += 1
        totals[f"{prefix}gate_cases"] += 1
        try:
            decision = evaluate_strategy_gate_case(case)
            expected_allowed = case.get("expected_allowed")
            expected_reason = case.get("expected_reason")
            allowed_match = expected_allowed is None or bool(expected_allowed) == decision.allowed
            reason_match = expected_reason is None or str(expected_reason) == decision.reason
            if allowed_match and reason_match:
                bucket[f"{prefix}passed"] += 1
                totals[f"{prefix}passed"] += 1
            else:
                bucket[f"{prefix}mismatched"] += 1
                totals[f"{prefix}mismatched"] += 1
                if len(mismatch_examples) < 12:
                    mismatch_examples.append(
                        {
                            "source_table": "events" if row_kind == "open_flow" else "sentinel_scans",
                            "row_kind": row_kind,
                            "event_id": row.get("id"),
                            "ts": row.get("ts"),
                            "strategy": strategy,
                            "symbol": symbol,
                            "event_type": event_type,
                            "case": case.get("name") or gate,
                            "gate": gate,
                            "expected_allowed": expected_allowed,
                            "actual_allowed": decision.allowed,
                            "expected_reason": expected_reason,
                            "actual_reason": decision.reason,
                        }
                    )
        except Exception as exc:
            bucket[f"{prefix}errors"] += 1
            totals[f"{prefix}errors"] += 1
            if len(error_examples) < 12:
                error_examples.append(
                    {
                        "source_table": "events" if row_kind == "open_flow" else "sentinel_scans",
                        "row_kind": row_kind,
                        "event_id": row.get("id"),
                        "ts": row.get("ts"),
                        "strategy": strategy,
                        "symbol": symbol,
                        "event_type": event_type,
                        "case": case.get("name") or gate,
                        "gate": gate,
                        "error": str(exc),
                    }
                )


def build_payload(db: Path, days: int, limit: int) -> dict[str, Any]:
    rows = query_events(db, days, limit)
    scan_rows = query_sentinel_scans(db, days, limit)
    by_strategy: dict[str, dict[str, Any]] = {}
    mismatch_examples: list[dict[str, Any]] = []
    error_examples: list[dict[str, Any]] = []
    totals = collections.Counter()
    latest_ts = ""

    for row in reversed(rows):
        event = ReplayEvent.from_event_store_row(row)
        if event.event_type.value not in OPEN_FLOW_TYPES:
            continue
        strategy = event.strategy or "unknown"
        latest_ts = max(latest_ts, event.ts or "")
        bucket = by_strategy.setdefault(strategy, _empty_strategy(strategy))
        bucket["open_flow_rows"] += 1
        totals["open_flow_rows"] += 1

        observed = evaluate_observed_gate(event)
        if observed.gate and observed.gate not in {"unknown", "unknown_gate"}:
            bucket["observed_gate_rows"] += 1
            totals["observed_gate_rows"] += 1

        cases = extract_gate_cases(row)
        if not cases:
            bucket["missing_case_rows"] += 1
            totals["missing_case_rows"] += 1
            continue
        bucket["rows_with_exact_cases"] += 1
        totals["rows_with_exact_cases"] += 1
        _process_cases(
            row=row,
            cases=cases,
            bucket=bucket,
            totals=totals,
            mismatch_examples=mismatch_examples,
            error_examples=error_examples,
            strategy=strategy,
            symbol=event.symbol,
            event_type=event.event_type.value,
            row_kind="open_flow",
        )

    for row in reversed(scan_rows):
        strategy = str(row.get("strategy") or "unknown")
        stage = str(row.get("stage") or "")
        if stage not in SCAN_GATE_STAGES:
            continue
        latest_ts = max(latest_ts, str(row.get("ts") or ""))
        bucket = by_strategy.setdefault(strategy, _empty_strategy(strategy))
        bucket["scan_gate_rows"] += 1
        totals["scan_gate_rows"] += 1
        cases = extract_gate_cases(row)
        if not cases:
            bucket["scan_missing_case_rows"] += 1
            totals["scan_missing_case_rows"] += 1
            continue
        bucket["scan_rows_with_exact_cases"] += 1
        totals["scan_rows_with_exact_cases"] += 1
        _process_cases(
            row=row,
            cases=cases,
            bucket=bucket,
            totals=totals,
            mismatch_examples=mismatch_examples,
            error_examples=error_examples,
            strategy=strategy,
            symbol=str(row.get("symbol") or ""),
            event_type=str(row.get("scan_result") or row.get("event_type") or ""),
            row_kind="scan_flow",
        )

    strategies: list[dict[str, Any]] = []
    for name in sorted(by_strategy):
        row = by_strategy[name]
        row["exact_case_coverage_pct"] = pct(row["rows_with_exact_cases"], row["open_flow_rows"])
        row["pass_rate_pct"] = pct(row["passed"], row["gate_cases"])
        row["top_gates"] = [{"name": k, "count": v} for k, v in row.pop("_gates").most_common(8)]
        row["scan_exact_case_coverage_pct"] = pct(row["scan_rows_with_exact_cases"], row["scan_gate_rows"])
        row["scan_pass_rate_pct"] = pct(row["scan_passed"], row["scan_gate_cases"])
        row["scan_top_gates"] = [{"name": k, "count": v} for k, v in row.pop("_scan_gates").most_common(8)]
        strategies.append(row)

    summary = {
        "open_flow_rows": int(totals["open_flow_rows"]),
        "observed_gate_rows": int(totals["observed_gate_rows"]),
        "rows_with_exact_cases": int(totals["rows_with_exact_cases"]),
        "missing_case_rows": int(totals["missing_case_rows"]),
        "gate_cases": int(totals["gate_cases"]),
        "passed": int(totals["passed"]),
        "mismatched": int(totals["mismatched"]),
        "errors": int(totals["errors"]),
        "exact_case_coverage_pct": pct(totals["rows_with_exact_cases"], totals["open_flow_rows"]),
        "observed_gate_coverage_pct": pct(totals["observed_gate_rows"], totals["open_flow_rows"]),
        "pass_rate_pct": pct(totals["passed"], totals["gate_cases"]),
        "scan_gate_rows": int(totals["scan_gate_rows"]),
        "scan_rows_with_exact_cases": int(totals["scan_rows_with_exact_cases"]),
        "scan_missing_case_rows": int(totals["scan_missing_case_rows"]),
        "scan_gate_cases": int(totals["scan_gate_cases"]),
        "scan_passed": int(totals["scan_passed"]),
        "scan_mismatched": int(totals["scan_mismatched"]),
        "scan_errors": int(totals["scan_errors"]),
        "scan_exact_case_coverage_pct": pct(totals["scan_rows_with_exact_cases"], totals["scan_gate_rows"]),
        "scan_pass_rate_pct": pct(totals["scan_passed"], totals["scan_gate_cases"]),
        "latest_ts": latest_ts,
    }
    if summary["errors"] or summary["mismatched"] or summary["scan_errors"] or summary["scan_mismatched"]:
        status = "bad"
        next_action = "fix_exact_gate_mismatches_before_claiming_replay_live_parity"
    elif summary["gate_cases"] == 0 and summary["scan_gate_cases"] == 0:
        status = "missing_exact_cases"
        next_action = "instrument_live_scanners_to_persist_strategy_gate_cases"
    elif (
        summary["open_flow_rows"] and summary["exact_case_coverage_pct"] < 80
    ) or (
        summary["scan_gate_rows"] and summary["scan_exact_case_coverage_pct"] < 80
    ):
        status = "partial"
        next_action = "increase_strategy_gate_case_logging_coverage"
    else:
        status = "ok"
        next_action = "continue_historical_same_input_expansion"
    summary["status"] = status
    summary["next_action"] = next_action

    return {
        "generated_at": now_cst().isoformat(),
        "db": str(db),
        "days": int(days),
        "limit": int(limit),
        "summary": summary,
        "strategies": strategies,
        "mismatch_examples": mismatch_examples,
        "error_examples": error_examples,
        "case_keys": {"single": list(CASE_SINGLE_KEYS), "list": list(CASE_LIST_KEYS)},
    }


def build_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    lines = [
        "# Replay/Live Parity Audit",
        "",
        f"- Generated: `{payload.get('generated_at')}`",
        f"- DB: `{payload.get('db')}`",
        f"- Window: `{payload.get('days')}` day(s), limit `{payload.get('limit')}` rows",
        f"- Status: `{summary.get('status')}`",
        f"- Open-flow rows: `{summary.get('open_flow_rows')}`",
        f"- Rows with exact cases: `{summary.get('rows_with_exact_cases')}` (`{summary.get('exact_case_coverage_pct')}%`)",
        f"- Gate cases: `{summary.get('gate_cases')}`; passed `{summary.get('passed')}`; mismatched `{summary.get('mismatched')}`; errors `{summary.get('errors')}`",
        f"- Pass rate: `{summary.get('pass_rate_pct')}%`",
        f"- Observed gate coverage: `{summary.get('observed_gate_coverage_pct')}%`",
        f"- Scan-gate rows: `{summary.get('scan_gate_rows')}`",
        f"- Scan rows with exact cases: `{summary.get('scan_rows_with_exact_cases')}` (`{summary.get('scan_exact_case_coverage_pct')}%`)",
        f"- Scan gate cases: `{summary.get('scan_gate_cases')}`; passed `{summary.get('scan_passed')}`; mismatched `{summary.get('scan_mismatched')}`; errors `{summary.get('scan_errors')}`",
        f"- Scan pass rate: `{summary.get('scan_pass_rate_pct')}%`",
        f"- Next action: `{summary.get('next_action')}`",
        "",
        "Exact parity only uses serialized `strategy_gate_case(s)` payloads. Rows without exact cases are counted as gaps, not guessed.",
        "`Open-flow` comes from the events table; `scan-level` comes from sentinel_scans pre-open filters and is reported separately.",
        "",
        "| Strategy | Open flow | Exact rows | Missing cases | Cases | Passed | Mismatch | Errors | Pass rate | Scan rows | Scan exact | Scan cases | Scan pass | Top gates | Scan gates |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for row in payload.get("strategies") or []:
        gates = ", ".join(f"{g['name']}:{g['count']}" for g in row.get("top_gates") or [])
        scan_gates = ", ".join(f"{g['name']}:{g['count']}" for g in row.get("scan_top_gates") or [])
        lines.append(
            "| {strategy} | {open_flow_rows} | {rows_with_exact_cases} | {missing_case_rows} | {gate_cases} | "
            "{passed} | {mismatched} | {errors} | {pass_rate_pct}% | {scan_gate_rows} | "
            "{scan_rows_with_exact_cases} | {scan_gate_cases} | {scan_pass_rate_pct}% | {gates} | {scan_gates} |".format(
                gates=gates or "-",
                scan_gates=scan_gates or "-",
                **row,
            )
        )
    if payload.get("mismatch_examples"):
        lines.extend(["", "## Mismatch Examples", ""])
        for item in payload["mismatch_examples"]:
            lines.append(
                "- `{source_table}` `{strategy}` `{symbol}` `{event_type}` row `{event_id}` gate `{gate}`: "
                "expected `{expected_allowed}/{expected_reason}` got `{actual_allowed}/{actual_reason}`".format(**item)
            )
    if payload.get("error_examples"):
        lines.extend(["", "## Error Examples", ""])
        for item in payload["error_examples"]:
            lines.append(
                "- `{source_table}` `{strategy}` `{symbol}` `{event_type}` row `{event_id}` gate `{gate}`: `{error}`".format(**item)
            )
    return "\n".join(lines) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit serialized replay/live same-input strategy gate cases")
    default_db = ROOT / "server_logs_tencent" / "runtime" / "event_store.sqlite3"
    if not default_db.exists():
        default_db = ROOT / "runtime" / "event_store.sqlite3"
    parser.add_argument("--db", default=str(default_db))
    parser.add_argument("--runtime-dir", default=str(ROOT / "runtime"))
    parser.add_argument("--reports-dir", default=str(ROOT / "reports"))
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--limit", type=int, default=30000)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = build_payload(Path(args.db), args.days, args.limit)
    runtime_dir = Path(args.runtime_dir)
    reports_dir = Path(args.reports_dir)
    json_dump(runtime_dir / "replay_live_parity_latest.json", payload)
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "replay_live_parity_latest.md").write_text(build_markdown(payload), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": payload["summary"]["status"],
                "open_flow_rows": payload["summary"]["open_flow_rows"],
                "gate_cases": payload["summary"]["gate_cases"],
                "mismatched": payload["summary"]["mismatched"],
                "errors": payload["summary"]["errors"],
                "exact_case_coverage_pct": payload["summary"]["exact_case_coverage_pct"],
                "scan_gate_rows": payload["summary"]["scan_gate_rows"],
                "scan_gate_cases": payload["summary"]["scan_gate_cases"],
                "scan_exact_case_coverage_pct": payload["summary"]["scan_exact_case_coverage_pct"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
