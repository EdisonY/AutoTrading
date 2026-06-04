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
CLOSE_FLOW_TYPES = {
    "CLOSE",
    "FORCED_CLOSE",
    "CLOSE_FAILED",
    "FORCED_CLOSE_FAILED",
    "EVICT_CLOSE",
    "EVICT_FAILED",
    "OPEN_SIZING_MISMATCH_CLOSED",
    "OPEN_SIZING_MISMATCH_FAILED",
}
EVENT_FLOW_TYPES = OPEN_FLOW_TYPES | CLOSE_FLOW_TYPES
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
ACCEPTANCE_MIN_EXACT_COVERAGE_PCT = 80.0
ACCEPTANCE_MIN_PASS_RATE_PCT = 99.9


def now_cst() -> datetime:
    return datetime.now(CST)


def pct(num: int | float, den: int | float) -> float:
    return round((float(num) / float(den) * 100.0), 2) if den else 0.0


def json_dump(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def parse_window_time(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip()
        if not raw:
            return None
        normalized = raw.replace("Z", "+00:00")
        candidates = [normalized]
        if " " in normalized and "T" not in normalized:
            candidates.append(normalized.replace(" ", "T", 1))
        dt = None
        for candidate in candidates:
            try:
                dt = datetime.fromisoformat(candidate)
                break
            except ValueError:
                pass
        if dt is None:
            for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(raw, fmt)
                    break
                except ValueError:
                    pass
        if dt is None:
            raise ValueError(f"Invalid time window value: {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt.astimezone(CST)


def _row_time(row: Mapping[str, Any]) -> datetime | None:
    try:
        return parse_window_time(row.get("ts"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _filter_rows_by_window(
    rows: Iterable[Mapping[str, Any]],
    *,
    since: datetime | None,
    until: datetime | None,
) -> list[dict[str, Any]]:
    if since is None and until is None:
        return [dict(row) for row in rows]
    filtered: list[dict[str, Any]] = []
    for row in rows:
        data = dict(row)
        ts = _row_time(data)
        if ts is None:
            continue
        if since is not None and ts < since:
            continue
        if until is not None and ts > until:
            continue
        filtered.append(data)
    return filtered


def query_events(
    db: Path,
    days: int,
    limit: int,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict[str, Any]]:
    if not db.exists():
        return []
    cutoff_dt = since or (now_cst() - timedelta(days=days))
    cutoff = cutoff_dt.strftime("%Y-%m-%d")
    con = sqlite3.connect(db)
    try:
        con.row_factory = sqlite3.Row
        placeholders = ", ".join("?" for _ in EVENT_FLOW_TYPES)
        until_sql = ""
        params: list[Any] = [cutoff]
        if until is not None:
            until_sql = "and substr(ts, 1, 10) <= ?"
            params.append(until.strftime("%Y-%m-%d"))
        params.extend(sorted(EVENT_FLOW_TYPES))
        params.append(int(limit))
        rows = con.execute(
            f"""
            select id, ts, strategy, symbol, event_type, category, side, score,
                   stage, layer, reason, source, payload_json
            from events
            where substr(ts, 1, 10) >= ?
              {until_sql}
              and event_type in ({placeholders})
              and strategy in ('A/v11', 'B/v16', 'C/v14')
            order by id desc
            limit ?
            """,
            tuple(params),
        ).fetchall()
    finally:
        con.close()
    return _filter_rows_by_window(rows, since=since, until=until)


def query_sentinel_scans(
    db: Path,
    days: int,
    limit: int,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict[str, Any]]:
    if not db.exists():
        return []
    cutoff_dt = since or (now_cst() - timedelta(days=days))
    cutoff = cutoff_dt.strftime("%Y-%m-%d")
    con = sqlite3.connect(db)
    try:
        con.row_factory = sqlite3.Row
        until_sql = ""
        params: list[Any] = [cutoff]
        if until is not None:
            until_sql = "and date <= ?"
            params.append(until.strftime("%Y-%m-%d"))
        params.append(int(limit))
        rows = con.execute(
            f"""
            select id, ts, strategy, symbol, event_type, category,
                   '' as side, null as score, decision_stage as stage,
                   filter_layer as layer, reason, 'sentinel_scans' as source,
                   scan_result, payload_json
            from sentinel_scans
            where date >= ?
              {until_sql}
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
            tuple(params),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        con.close()
    return _filter_rows_by_window(rows, since=since, until=until)


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
    if count == 1 and event_type in OPEN_FLOW_TYPES | CLOSE_FLOW_TYPES:
        normalized["expected_allowed"] = event_type in {
            "SIGNAL",
            "OPEN",
            "CLOSE",
            "FORCED_CLOSE",
            "EVICT_CLOSE",
            "OPEN_SIZING_MISMATCH_CLOSED",
        }
        meta = dict(normalized.get("meta") or {})
        meta["expected_allowed_inferred_from_event_type"] = event_type
        normalized["meta"] = meta
        return normalized, True
    return normalized, False


def extract_gate_cases(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    payload = parse_payload(row.get("payload_json"))
    raw_cases: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for item in _case_sources(payload):
        if not isinstance(item, Mapping):
            continue
        key = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        raw_cases.append(item)
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
        "close_flow_rows": 0,
        "close_rows_with_exact_cases": 0,
        "close_missing_case_rows": 0,
        "close_gate_cases": 0,
        "close_passed": 0,
        "close_mismatched": 0,
        "close_errors": 0,
        "_close_gates": collections.Counter(),
    }


def _flow_acceptance(
    summary: Mapping[str, Any],
    *,
    label: str,
    row_key: str,
    exact_rows_key: str,
    missing_rows_key: str,
    cases_key: str,
    passed_key: str,
    mismatched_key: str,
    errors_key: str,
    coverage_key: str,
    pass_rate_key: str,
) -> dict[str, Any]:
    rows = int(summary.get(row_key) or 0)
    exact_rows = int(summary.get(exact_rows_key) or 0)
    missing_rows = int(summary.get(missing_rows_key) or 0)
    cases = int(summary.get(cases_key) or 0)
    passed = int(summary.get(passed_key) or 0)
    mismatched = int(summary.get(mismatched_key) or 0)
    errors = int(summary.get(errors_key) or 0)
    coverage = float(summary.get(coverage_key) or 0.0)
    pass_rate = float(summary.get(pass_rate_key) or 0.0)
    readiness = round((coverage / 100.0) * (pass_rate / 100.0) * 100.0, 2) if rows and cases else 0.0

    if rows <= 0:
        status = "not_applicable"
        accepted = True
        next_action = "wait_for_flow_rows_after_fresh_run"
    elif cases <= 0:
        status = "missing_exact_cases"
        accepted = False
        next_action = "persist_strategy_gate_cases_for_this_flow"
    elif errors or mismatched:
        status = "blocked_by_mismatch"
        accepted = False
        next_action = "fix_exact_gate_mismatches_before_claiming_parity"
    elif coverage < ACCEPTANCE_MIN_EXACT_COVERAGE_PCT:
        status = "coverage_gap"
        accepted = False
        next_action = "increase_exact_case_coverage_or_collect_fresh_run_rows"
    elif pass_rate < ACCEPTANCE_MIN_PASS_RATE_PCT:
        status = "pass_rate_gap"
        accepted = False
        next_action = "investigate_non_passing_exact_cases"
    else:
        status = "accepted"
        accepted = True
        next_action = "keep_monitoring"

    return {
        "label": label,
        "status": status,
        "accepted": accepted,
        "rows": rows,
        "rows_with_exact_cases": exact_rows,
        "missing_case_rows": missing_rows,
        "gate_cases": cases,
        "passed": passed,
        "mismatched": mismatched,
        "errors": errors,
        "coverage_pct": coverage,
        "pass_rate_pct": pass_rate,
        "readiness_score_pct": readiness,
        "next_action": next_action,
    }


def build_acceptance(summary: Mapping[str, Any]) -> dict[str, Any]:
    flows = {
        "open_flow": _flow_acceptance(
            summary,
            label="open_flow",
            row_key="open_flow_rows",
            exact_rows_key="rows_with_exact_cases",
            missing_rows_key="missing_case_rows",
            cases_key="gate_cases",
            passed_key="passed",
            mismatched_key="mismatched",
            errors_key="errors",
            coverage_key="exact_case_coverage_pct",
            pass_rate_key="pass_rate_pct",
        ),
        "scan_flow": _flow_acceptance(
            summary,
            label="scan_flow",
            row_key="scan_gate_rows",
            exact_rows_key="scan_rows_with_exact_cases",
            missing_rows_key="scan_missing_case_rows",
            cases_key="scan_gate_cases",
            passed_key="scan_passed",
            mismatched_key="scan_mismatched",
            errors_key="scan_errors",
            coverage_key="scan_exact_case_coverage_pct",
            pass_rate_key="scan_pass_rate_pct",
        ),
        "close_flow": _flow_acceptance(
            summary,
            label="close_flow",
            row_key="close_flow_rows",
            exact_rows_key="close_rows_with_exact_cases",
            missing_rows_key="close_missing_case_rows",
            cases_key="close_gate_cases",
            passed_key="close_passed",
            mismatched_key="close_mismatched",
            errors_key="close_errors",
            coverage_key="close_exact_case_coverage_pct",
            pass_rate_key="close_pass_rate_pct",
        ),
    }
    active_flows = [flow for flow in flows.values() if flow["rows"] > 0]
    blocking_flows = [flow for flow in active_flows if not flow["accepted"]]
    has_cases = any(flow["gate_cases"] > 0 for flow in active_flows)
    total_rows = sum(flow["rows"] for flow in active_flows)
    readiness = (
        round(sum(flow["rows"] * flow["readiness_score_pct"] for flow in active_flows) / total_rows, 2)
        if total_rows
        else 0.0
    )

    if not active_flows:
        overall_status = "no_historical_rows"
        conclusion = "historical_same_input_parity_not_measurable"
        next_action = "run_final_staged_fresh_run_after_offline_work"
    elif not has_cases:
        overall_status = "missing_exact_cases"
        conclusion = "historical_same_input_parity_not_measurable"
        next_action = "persist_strategy_gate_cases_or_collect_fresh_run_rows"
    elif blocking_flows:
        overall_status = "partial" if all(flow["status"] == "coverage_gap" for flow in blocking_flows) else "blocked"
        conclusion = (
            "historical_same_input_parity_partial_coverage"
            if overall_status == "partial"
            else "historical_same_input_parity_blocked"
        )
        next_action = blocking_flows[0]["next_action"]
    else:
        overall_status = "accepted"
        conclusion = "historical_same_input_parity_accepted_for_available_rows"
        next_action = "continue_fresh_run_monitoring"

    return {
        "accepted": overall_status == "accepted",
        "overall_status": overall_status,
        "conclusion": conclusion,
        "readiness_score_pct": readiness,
        "min_exact_case_coverage_pct": ACCEPTANCE_MIN_EXACT_COVERAGE_PCT,
        "min_pass_rate_pct": ACCEPTANCE_MIN_PASS_RATE_PCT,
        "fresh_run_required": overall_status != "accepted",
        "next_action": next_action,
        "blocking_flows": [
            {
                "label": flow["label"],
                "status": flow["status"],
                "rows": flow["rows"],
                "coverage_pct": flow["coverage_pct"],
                "pass_rate_pct": flow["pass_rate_pct"],
                "next_action": flow["next_action"],
            }
            for flow in blocking_flows
        ],
        "flows": flows,
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
    if row_kind == "open_flow":
        prefix = ""
        gate_counter_key = "_gates"
        source_table = "events"
    elif row_kind == "close_flow":
        prefix = "close_"
        gate_counter_key = "_close_gates"
        source_table = "events"
    else:
        prefix = "scan_"
        gate_counter_key = "_scan_gates"
        source_table = "sentinel_scans"
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
                            "source_table": source_table,
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
                        "source_table": source_table,
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


def build_payload(
    db: Path,
    days: int,
    limit: int,
    *,
    since: str | datetime | None = None,
    until: str | datetime | None = None,
) -> dict[str, Any]:
    since_dt = parse_window_time(since)
    until_dt = parse_window_time(until)
    if since_dt is not None and until_dt is not None and since_dt > until_dt:
        raise ValueError("--since must be earlier than or equal to --until")

    rows = query_events(db, days, limit, since=since_dt, until=until_dt)
    scan_rows = query_sentinel_scans(db, days, limit, since=since_dt, until=until_dt)
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

    for row in reversed(rows):
        event = ReplayEvent.from_event_store_row(row)
        if event.event_type.value not in CLOSE_FLOW_TYPES:
            continue
        strategy = event.strategy or "unknown"
        latest_ts = max(latest_ts, event.ts or "")
        bucket = by_strategy.setdefault(strategy, _empty_strategy(strategy))
        bucket["close_flow_rows"] += 1
        totals["close_flow_rows"] += 1

        cases = extract_gate_cases(row)
        if not cases:
            bucket["close_missing_case_rows"] += 1
            totals["close_missing_case_rows"] += 1
            continue
        bucket["close_rows_with_exact_cases"] += 1
        totals["close_rows_with_exact_cases"] += 1
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
            row_kind="close_flow",
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
        row["close_exact_case_coverage_pct"] = pct(row["close_rows_with_exact_cases"], row["close_flow_rows"])
        row["close_pass_rate_pct"] = pct(row["close_passed"], row["close_gate_cases"])
        row["close_top_gates"] = [{"name": k, "count": v} for k, v in row.pop("_close_gates").most_common(8)]
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
        "close_flow_rows": int(totals["close_flow_rows"]),
        "close_rows_with_exact_cases": int(totals["close_rows_with_exact_cases"]),
        "close_missing_case_rows": int(totals["close_missing_case_rows"]),
        "close_gate_cases": int(totals["close_gate_cases"]),
        "close_passed": int(totals["close_passed"]),
        "close_mismatched": int(totals["close_mismatched"]),
        "close_errors": int(totals["close_errors"]),
        "close_exact_case_coverage_pct": pct(totals["close_rows_with_exact_cases"], totals["close_flow_rows"]),
        "close_pass_rate_pct": pct(totals["close_passed"], totals["close_gate_cases"]),
        "latest_ts": latest_ts,
    }
    if (
        summary["errors"]
        or summary["mismatched"]
        or summary["scan_errors"]
        or summary["scan_mismatched"]
        or summary["close_errors"]
        or summary["close_mismatched"]
    ):
        status = "bad"
        next_action = "fix_exact_gate_mismatches_before_claiming_replay_live_parity"
    elif summary["gate_cases"] == 0 and summary["scan_gate_cases"] == 0 and summary["close_gate_cases"] == 0:
        status = "missing_exact_cases"
        next_action = "instrument_live_scanners_to_persist_strategy_gate_cases"
    elif (
        summary["open_flow_rows"] and summary["exact_case_coverage_pct"] < 80
    ) or (
        summary["scan_gate_rows"] and summary["scan_exact_case_coverage_pct"] < 80
    ) or (
        summary["close_flow_rows"] and summary["close_exact_case_coverage_pct"] < 80
    ):
        status = "partial"
        next_action = "increase_strategy_gate_case_logging_coverage"
    else:
        status = "ok"
        next_action = "continue_historical_same_input_expansion"
    summary["status"] = status
    summary["next_action"] = next_action
    acceptance = build_acceptance(summary)
    summary["acceptance_status"] = acceptance["overall_status"]
    summary["acceptance_conclusion"] = acceptance["conclusion"]
    summary["readiness_score_pct"] = acceptance["readiness_score_pct"]

    return {
        "generated_at": now_cst().isoformat(),
        "db": str(db),
        "days": int(days),
        "since": since_dt.isoformat() if since_dt else None,
        "until": until_dt.isoformat() if until_dt else None,
        "limit": int(limit),
        "summary": summary,
        "acceptance": acceptance,
        "strategies": strategies,
        "mismatch_examples": mismatch_examples,
        "error_examples": error_examples,
        "case_keys": {"single": list(CASE_SINGLE_KEYS), "list": list(CASE_LIST_KEYS)},
    }


def build_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    acceptance = payload.get("acceptance") or {}
    flows = acceptance.get("flows") or {}
    lines = [
        "# Replay/Live Parity Audit",
        "",
        f"- Generated: `{payload.get('generated_at')}`",
        f"- DB: `{payload.get('db')}`",
        f"- Window: `{payload.get('days')}` day(s), limit `{payload.get('limit')}` rows",
        f"- Since: `{payload.get('since') or '-'}`",
        f"- Until: `{payload.get('until') or '-'}`",
        f"- Status: `{summary.get('status')}`",
        f"- Acceptance: `{acceptance.get('overall_status')}`",
        f"- Conclusion: `{acceptance.get('conclusion')}`",
        f"- Readiness score: `{acceptance.get('readiness_score_pct')}%`",
        f"- Acceptance thresholds: exact coverage >= `{acceptance.get('min_exact_case_coverage_pct')}%`, pass rate >= `{acceptance.get('min_pass_rate_pct')}%`",
        f"- Fresh-run required: `{acceptance.get('fresh_run_required')}`",
        f"- Open-flow rows: `{summary.get('open_flow_rows')}`",
        f"- Rows with exact cases: `{summary.get('rows_with_exact_cases')}` (`{summary.get('exact_case_coverage_pct')}%`)",
        f"- Gate cases: `{summary.get('gate_cases')}`; passed `{summary.get('passed')}`; mismatched `{summary.get('mismatched')}`; errors `{summary.get('errors')}`",
        f"- Pass rate: `{summary.get('pass_rate_pct')}%`",
        f"- Observed gate coverage: `{summary.get('observed_gate_coverage_pct')}%`",
        f"- Scan-gate rows: `{summary.get('scan_gate_rows')}`",
        f"- Scan rows with exact cases: `{summary.get('scan_rows_with_exact_cases')}` (`{summary.get('scan_exact_case_coverage_pct')}%`)",
        f"- Scan gate cases: `{summary.get('scan_gate_cases')}`; passed `{summary.get('scan_passed')}`; mismatched `{summary.get('scan_mismatched')}`; errors `{summary.get('scan_errors')}`",
        f"- Scan pass rate: `{summary.get('scan_pass_rate_pct')}%`",
        f"- Close-flow rows: `{summary.get('close_flow_rows')}`",
        f"- Close rows with exact cases: `{summary.get('close_rows_with_exact_cases')}` (`{summary.get('close_exact_case_coverage_pct')}%`)",
        f"- Close gate cases: `{summary.get('close_gate_cases')}`; passed `{summary.get('close_passed')}`; mismatched `{summary.get('close_mismatched')}`; errors `{summary.get('close_errors')}`",
        f"- Close pass rate: `{summary.get('close_pass_rate_pct')}%`",
        f"- Next action: `{summary.get('next_action')}`",
        "",
        "Exact parity only uses serialized `strategy_gate_case(s)` payloads. Rows without exact cases are counted as gaps, not guessed.",
        "`Open-flow` and `close-flow` come from the events table; `scan-level` comes from sentinel_scans pre-open filters and is reported separately.",
        "",
        "## Acceptance By Flow",
        "",
        "| Flow | Status | Accepted | Rows | Exact rows | Missing | Cases | Pass rate | Coverage | Readiness | Next action |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for flow_name in ("open_flow", "scan_flow", "close_flow"):
        flow = flows.get(flow_name) or {}
        lines.append(
            "| {label} | {status} | {accepted} | {rows} | {rows_with_exact_cases} | {missing_case_rows} | "
            "{gate_cases} | {pass_rate_pct}% | {coverage_pct}% | {readiness_score_pct}% | {next_action} |".format(
                label=flow.get("label", flow_name),
                status=flow.get("status", ""),
                accepted=str(flow.get("accepted")),
                rows=flow.get("rows", 0),
                rows_with_exact_cases=flow.get("rows_with_exact_cases", 0),
                missing_case_rows=flow.get("missing_case_rows", 0),
                gate_cases=flow.get("gate_cases", 0),
                pass_rate_pct=flow.get("pass_rate_pct", 0),
                coverage_pct=flow.get("coverage_pct", 0),
                readiness_score_pct=flow.get("readiness_score_pct", 0),
                next_action=flow.get("next_action", ""),
            )
        )
    lines.extend(
        [
            "",
            "## Strategy Detail",
            "",
        "| Strategy | Open flow | Exact rows | Missing cases | Cases | Passed | Mismatch | Errors | Pass rate | Scan rows | Scan exact | Scan cases | Scan pass | Close rows | Close exact | Close cases | Close pass | Top gates | Scan gates | Close gates |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |",
        ]
    )
    for row in payload.get("strategies") or []:
        gates = ", ".join(f"{g['name']}:{g['count']}" for g in row.get("top_gates") or [])
        scan_gates = ", ".join(f"{g['name']}:{g['count']}" for g in row.get("scan_top_gates") or [])
        close_gates = ", ".join(f"{g['name']}:{g['count']}" for g in row.get("close_top_gates") or [])
        lines.append(
            "| {strategy} | {open_flow_rows} | {rows_with_exact_cases} | {missing_case_rows} | {gate_cases} | "
            "{passed} | {mismatched} | {errors} | {pass_rate_pct}% | {scan_gate_rows} | "
            "{scan_rows_with_exact_cases} | {scan_gate_cases} | {scan_pass_rate_pct}% | {close_flow_rows} | "
            "{close_rows_with_exact_cases} | {close_gate_cases} | {close_pass_rate_pct}% | {gates} | {scan_gates} | {close_gates} |".format(
                gates=gates or "-",
                scan_gates=scan_gates or "-",
                close_gates=close_gates or "-",
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
    parser.add_argument("--since", default=None, help="Only audit rows at or after this CST/ISO timestamp")
    parser.add_argument("--until", default=None, help="Only audit rows at or before this CST/ISO timestamp")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = build_payload(Path(args.db), args.days, args.limit, since=args.since, until=args.until)
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
                "close_flow_rows": payload["summary"]["close_flow_rows"],
                "close_gate_cases": payload["summary"]["close_gate_cases"],
                "close_exact_case_coverage_pct": payload["summary"]["close_exact_case_coverage_pct"],
                "acceptance_status": payload["acceptance"]["overall_status"],
                "acceptance_conclusion": payload["acceptance"]["conclusion"],
                "readiness_score_pct": payload["acceptance"]["readiness_score_pct"],
                "since": payload.get("since"),
                "until": payload.get("until"),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
