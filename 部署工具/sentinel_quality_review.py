"""Sentinel Quality Review - measure sentinel signal contribution.

Reads from SQLite event_store.sqlite3 to evaluate:
- Which sentinel signals led to profitable moves
- Which were opened by strategies vs filtered vs rejected
- Forward returns at 15/30/60/120 minutes
- Coverage: how many big movers were in sentinel scan range

Outputs:
- runtime/sentinel_quality_latest.json
- reports/sentinel_quality_latest.md
"""

from __future__ import annotations

import argparse
import bisect
import statistics
import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

CST = timezone(timedelta(hours=8))
FEE_RATE = 0.0005  # 0.05% taker per side
FORWARD_HORIZONS_MIN = (15, 30, 60, 120)
FORWARD_TOLERANCE_MIN = 20
BIG_MOVE_ABS_PCT = 8.0
BUS_COVERAGE_WINDOW_MIN = 30
BUS_SCAN_LOOKBACK_MIN = 5
NEAR_MISS_WINDOW_MIN = 180
WATCHLIST_HISTORY_REL = Path("runtime") / "market_mover_watchlist_history.jsonl"


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST)
        return dt.astimezone(CST)
    except Exception:
        return None


def safe_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val) if val is not None else default
    except (ValueError, TypeError):
        return default


def parse_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def load_watchlist_history(root: Path, days: int = 7) -> list[dict[str, Any]]:
    now = datetime.now(CST)
    cutoff = now - timedelta(days=days)
    shard_dir = root / WATCHLIST_HISTORY_REL.parent / WATCHLIST_HISTORY_REL.stem
    candidates = [root / WATCHLIST_HISTORY_REL]
    for offset in range(max(1, days + 1)):
        candidates.append(shard_dir / f"{(now - timedelta(days=offset)).strftime('%Y-%m-%d')}.jsonl")
    rows: list[dict[str, Any]] = []
    seen_paths: set[Path] = set()
    for path in candidates:
        if path in seen_paths or not path.exists():
            continue
        seen_paths.add(path)
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        for line in lines:
            payload = parse_payload(line)
            ts = parse_dt(payload.get("ts"))
            if not ts or ts < cutoff:
                continue
            symbols = payload.get("symbols") if isinstance(payload.get("symbols"), list) else []
            symbol_set = {
                str(item.get("symbol") if isinstance(item, dict) else item).upper().strip()
                for item in symbols
            }
            rows.append(
                {
                    "ts": payload.get("ts"),
                    "symbols": [symbol for symbol in symbol_set if symbol],
                    "count": len(symbol_set),
                }
            )
    rows.sort(key=lambda item: parse_dt(item.get("ts")) or datetime.min.replace(tzinfo=CST))
    return rows


def watchlist_history_root(db_path: Path, default_root: Path) -> Path:
    if db_path.name == "event_store.sqlite3" and db_path.parent.name == "runtime":
        return db_path.parent.parent
    return default_root


def load_sentinel_decisions(con: sqlite3.Connection, days: int = 7) -> list[dict[str, Any]]:
    """Load OPEN/SIGNAL/SKIPPED events that have sentinel fields."""
    cutoff = (datetime.now(CST) - timedelta(days=days)).isoformat()
    rows = con.execute(
        """SELECT id, ts, strategy, symbol, event_type, side, score, stage, layer, reason, payload_json
           FROM events
           WHERE ts >= ? AND payload_json LIKE '%sentinel%'
           ORDER BY ts""",
        (cutoff,),
    ).fetchall()
    events = []
    for row in rows:
        payload = json.loads(row[10]) if row[10] else {}
        if not payload.get("sentinel"):
            continue
        events.append({
            "id": row[0],
            "ts": row[1],
            "strategy": row[2],
            "symbol": row[3],
            "event_type": row[4],
            "side": row[5],
            "score": safe_float(row[6]),
            "stage": row[7],
            "layer": row[8],
            "reason": row[9],
            "sentinel_reason": payload.get("sentinel_reason", ""),
            "sentinel_change_pct": safe_float(payload.get("sentinel_change_pct")),
            "sentinel_velocity_pct": safe_float(payload.get("sentinel_velocity_pct")),
            "sentinel_quote_volume": safe_float(payload.get("sentinel_quote_volume")),
            "sentinel_scan_result": payload.get("sentinel_scan_result", ""),
            "decision_stage": payload.get("decision_stage", ""),
            "filter_layer": payload.get("filter_layer", ""),
        })
    return events


def load_sentinel_scanned(con: sqlite3.Connection, days: int = 7) -> list[dict[str, Any]]:
    """Load SENTINEL_SCANNED events from dedicated sentinel_scans table (fallback to events)."""
    cutoff = (datetime.now(CST) - timedelta(days=days)).strftime("%Y-%m-%d")
    # Try dedicated table first
    try:
        rows = con.execute(
            """SELECT id, ts, strategy, symbol, event_type, reason, category,
                      decision_stage, filter_layer, change_pct, velocity_pct,
                      quote_volume, scan_result, payload_json
               FROM sentinel_scans
               WHERE date >= ?
               ORDER BY ts""",
            (cutoff,),
        ).fetchall()
        if rows:
            out = []
            for row in rows:
                payload = parse_payload(row[13])
                out.append(
                    {
                        "id": row[0],
                        "ts": row[1],
                        "strategy": row[2],
                        "symbol": row[3],
                        "sentinel_reason": row[5] or payload.get("sentinel_reason", ""),
                        "sentinel_change_pct": safe_float(row[9]),
                        "sentinel_velocity_pct": safe_float(row[10]),
                        "sentinel_quote_volume": safe_float(row[11] or payload.get("sentinel_quote_volume")),
                        "sentinel_scan_result": row[12] or payload.get("sentinel_scan_result", ""),
                        "decision_stage": row[7] or payload.get("decision_stage", ""),
                        "filter_layer": row[8] or payload.get("filter_layer", ""),
                        "reason": row[5] or "",
                        "last_price": safe_float(payload.get("sentinel_last_price") or payload.get("last_price")),
                        "rank": payload.get("sentinel_rank") or payload.get("rank"),
                        "side": payload.get("side", ""),
                        "score": safe_float(payload.get("score")),
                    }
                )
            return out
    except Exception:
        pass
    # Fallback to events table
    cutoff_iso = (datetime.now(CST) - timedelta(days=days)).isoformat()
    rows = con.execute(
        """SELECT id, ts, strategy, symbol, event_type, payload_json
           FROM events
           WHERE event_type = 'SENTINEL_SCANNED' AND ts >= ?
           ORDER BY ts""",
        (cutoff_iso,),
    ).fetchall()
    events = []
    for row in rows:
        payload = json.loads(row[5]) if row[5] else {}
        events.append({
            "id": row[0],
            "ts": row[1],
            "strategy": row[2],
            "symbol": row[3],
            "sentinel_reason": payload.get("sentinel_reason", ""),
            "sentinel_change_pct": safe_float(payload.get("sentinel_change_pct")),
            "sentinel_velocity_pct": safe_float(payload.get("sentinel_velocity_pct")),
            "sentinel_scan_result": payload.get("sentinel_scan_result", ""),
            "decision_stage": payload.get("decision_stage", ""),
            "filter_layer": payload.get("filter_layer", ""),
            "reason": payload.get("reason", ""),
            "last_price": safe_float(payload.get("sentinel_last_price") or payload.get("last_price")),
            "rank": payload.get("sentinel_rank") or payload.get("rank"),
            "side": payload.get("side", ""),
            "score": safe_float(payload.get("score")),
        })
    return events


def load_sentinel_bus_signals(con: sqlite3.Connection, days: int = 7) -> list[dict[str, Any]]:
    cutoff_iso = (datetime.now(CST) - timedelta(days=days)).isoformat()
    rows = con.execute(
        """SELECT id, ts, symbol, reason, payload_json
           FROM events
           WHERE event_type = 'SENTINEL_SIGNAL' AND ts >= ?
           ORDER BY ts""",
        (cutoff_iso,),
    ).fetchall()
    signals = []
    for row in rows:
        payload = parse_payload(row[4])
        signals.append(
            {
                "id": row[0],
                "ts": row[1],
                "symbol": row[2] or payload.get("symbol", ""),
                "reason": row[3] or payload.get("reason", ""),
                "change_pct": safe_float(payload.get("change_pct")),
                "abs_change_pct": safe_float(payload.get("abs_change_pct") or abs(safe_float(payload.get("change_pct")))),
                "velocity_pct": safe_float(payload.get("velocity_pct")),
                "quote_volume": safe_float(payload.get("quote_volume")),
                "last_price": safe_float(payload.get("last_price")),
                "rank": payload.get("rank"),
            }
        )
    return signals


def classify_response(events: list[dict]) -> dict[str, list[dict]]:
    """Classify sentinel events by strategy response."""
    by_response: dict[str, list[dict]] = {
        "opened": [],
        "signal_generated": [],
        "skipped": [],
        "filtered": [],
        "no_signal": [],
        "error": [],
    }
    for e in events:
        etype = e.get("event_type", "")
        if etype == "OPEN":
            by_response["opened"].append(e)
        elif etype == "SIGNAL":
            by_response["signal_generated"].append(e)
        elif etype == "OPEN_SKIPPED":
            by_response["skipped"].append(e)
        elif etype in ("SENTINEL_SCANNED", "SENTINEL_SIGNAL"):
            result = e.get("sentinel_scan_result", "")
            if "error" in str(e.get("reason", "")).lower() or "400" in str(e.get("reason", "")):
                by_response["error"].append(e)
            elif e.get("filter_layer"):
                by_response["filtered"].append(e)
            else:
                by_response["no_signal"].append(e)
        else:
            by_response["no_signal"].append(e)
    return by_response


def compute_sentinel_stats(events: list[dict], scanned: list[dict]) -> dict[str, Any]:
    """Compute sentinel quality statistics."""
    now = datetime.now(CST)
    by_response = classify_response(events)

    # Per sentinel reason stats
    reason_stats: dict[str, dict] = {}
    for e in events:
        reason = e.get("sentinel_reason", "unknown")
        if reason not in reason_stats:
            reason_stats[reason] = {"total": 0, "opened": 0, "skipped": 0, "filtered": 0, "no_signal": 0}
        reason_stats[reason]["total"] += 1
        etype = e.get("event_type", "")
        if etype == "OPEN":
            reason_stats[reason]["opened"] += 1
        elif etype == "OPEN_SKIPPED":
            reason_stats[reason]["skipped"] += 1
        elif e.get("filter_layer"):
            reason_stats[reason]["filtered"] += 1
        else:
            reason_stats[reason]["no_signal"] += 1

    # Per strategy stats
    strategy_stats: dict[str, dict] = {}
    for e in events:
        strategy = e.get("strategy", "unknown")
        if strategy not in strategy_stats:
            strategy_stats[strategy] = {"total": 0, "opened": 0, "skipped": 0, "filtered": 0}
        strategy_stats[strategy]["total"] += 1
        etype = e.get("event_type", "")
        if etype == "OPEN":
            strategy_stats[strategy]["opened"] += 1
        elif etype == "OPEN_SKIPPED":
            strategy_stats[strategy]["skipped"] += 1
        elif e.get("filter_layer"):
            strategy_stats[strategy]["filtered"] += 1

    # Top movers by change_pct
    all_sentinel = sorted(
        [e for e in scanned if abs(e.get("sentinel_change_pct", 0)) > 0],
        key=lambda e: abs(e.get("sentinel_change_pct", 0)),
        reverse=True,
    )
    top_movers = all_sentinel[:20]

    # Coverage: how many top movers were scanned
    unique_symbols_scanned = set(e.get("symbol", "") for e in scanned)
    unique_symbols_opened = set(e.get("symbol", "") for e in events if e.get("event_type") == "OPEN")

    return {
        "total_sentinel_decisions": len(events),
        "total_scanned": len(scanned),
        "unique_symbols_scanned": len(unique_symbols_scanned),
        "unique_symbols_opened": len(unique_symbols_opened),
        "response_breakdown": {k: len(v) for k, v in by_response.items()},
        "reason_stats": reason_stats,
        "strategy_stats": strategy_stats,
        "top_movers": [
            {
                "symbol": e.get("symbol"),
                "change_pct": e.get("sentinel_change_pct"),
                "velocity_pct": e.get("sentinel_velocity_pct"),
                "reason": e.get("sentinel_reason"),
                "strategy": e.get("strategy"),
                "ts": e.get("ts"),
            }
            for e in top_movers
        ],
    }


def compute_forward_returns(scanned: list[dict[str, Any]]) -> dict[str, Any]:
    by_symbol: dict[str, list[tuple[datetime, float]]] = {}
    for row in scanned:
        symbol = str(row.get("symbol") or "")
        ts = parse_dt(row.get("ts"))
        price = safe_float(row.get("last_price"))
        if not symbol or not ts or price <= 0:
            continue
        by_symbol.setdefault(symbol, []).append((ts, price))
    for rows in by_symbol.values():
        rows.sort(key=lambda item: item[0])

    horizon_returns: dict[str, list[float]] = {f"{m}m": [] for m in FORWARD_HORIZONS_MIN}
    horizon_directional: dict[str, list[float]] = {f"{m}m": [] for m in FORWARD_HORIZONS_MIN}
    reason_returns: dict[str, dict[str, list[float]]] = {}
    examples: list[dict[str, Any]] = []

    for row in scanned:
        symbol = str(row.get("symbol") or "")
        ts = parse_dt(row.get("ts"))
        price = safe_float(row.get("last_price"))
        series = by_symbol.get(symbol) or []
        if not symbol or not ts or price <= 0 or len(series) < 2:
            continue
        times = [item[0] for item in series]
        direction = 1.0 if safe_float(row.get("sentinel_change_pct")) >= 0 else -1.0
        reason = str(row.get("sentinel_reason") or "unknown")
        for minutes in FORWARD_HORIZONS_MIN:
            label = f"{minutes}m"
            target = ts + timedelta(minutes=minutes)
            idx = bisect.bisect_left(times, target)
            if idx >= len(series):
                continue
            future_ts, future_price = series[idx]
            if future_ts > target + timedelta(minutes=FORWARD_TOLERANCE_MIN):
                continue
            raw_return = (future_price / price - 1.0) * 100.0
            directional = raw_return * direction
            after_fee = directional - (FEE_RATE * 2 * 100.0)
            horizon_returns[label].append(raw_return)
            horizon_directional[label].append(after_fee)
            reason_returns.setdefault(reason, {}).setdefault(label, []).append(after_fee)
            if len(examples) < 12 and minutes in (60, 120):
                examples.append(
                    {
                        "symbol": symbol,
                        "strategy": row.get("strategy"),
                        "reason": reason,
                        "ts": row.get("ts"),
                        "horizon": label,
                        "raw_return_pct": round(raw_return, 4),
                        "directional_after_fee_pct": round(after_fee, 4),
                    }
                )

    def summarize(values: list[float]) -> dict[str, Any]:
        if not values:
            return {"samples": 0, "avg_pct": 0.0, "median_pct": 0.0, "win_rate_pct": 0.0}
        return {
            "samples": len(values),
            "avg_pct": round(sum(values) / len(values), 4),
            "median_pct": round(statistics.median(values), 4),
            "win_rate_pct": round(sum(1 for value in values if value > 0) / len(values) * 100.0, 2),
        }

    by_horizon = {
        label: {
            "raw": summarize(horizon_returns[label]),
            "directional_after_fee": summarize(horizon_directional[label]),
        }
        for label in horizon_returns
    }
    by_reason = {
        reason: {label: summarize(values) for label, values in labels.items()}
        for reason, labels in sorted(reason_returns.items(), key=lambda item: sum(len(v) for v in item[1].values()), reverse=True)
    }
    return {"by_horizon": by_horizon, "by_reason": by_reason, "examples": examples}


def attribution_bucket(row: dict[str, Any] | None) -> str:
    if not row:
        return "not_scanned"
    result = str(row.get("sentinel_scan_result") or row.get("scan_result") or "").lower()
    stage = str(row.get("decision_stage") or "").lower()
    layer = str(row.get("filter_layer") or "").lower()
    reason = str(row.get("reason") or "").lower()
    if result in ("opened", "open") or stage == "open":
        return "opened"
    if "error" in result or "error" in reason or "400" in reason:
        return "analysis_or_data_error"
    if result in ("no_signal", "signal_not_found"):
        return "scanned_no_signal"
    if "pre_filter" in result or stage == "pre_filter":
        return "pre_filter_rejected"
    if layer == "market_data" or "market" in stage:
        return "market_data_rejected"
    if layer == "confirmation" or "confirmation" in stage:
        return "confirmation_rejected"
    if layer == "execution" or "execution" in stage or "tradability" in stage:
        return "execution_rejected"
    if layer == "risk" or "risk" in stage or "cooldown" in stage or "position" in stage or "capital" in stage:
        return "risk_rejected"
    if layer == "strategy" or "score" in stage or "threshold" in stage or "strategy" in stage:
        return "strategy_rejected"
    return "scanned_other"


def attribution_label(bucket: str) -> str:
    labels = {
        "not_scanned": "未进入策略扫描",
        "opened": "已开仓",
        "scanned_no_signal": "已扫描但无信号",
        "strategy_rejected": "策略拒绝",
        "risk_rejected": "风控/冷却/仓位拒绝",
        "confirmation_rejected": "确认层拒绝",
        "execution_rejected": "执行/交易所规则拒绝",
        "market_data_rejected": "行情数据拒绝",
        "pre_filter_rejected": "预过滤拒绝",
        "analysis_or_data_error": "分析/数据错误",
        "scanned_other": "已扫描但未归类",
    }
    return labels.get(bucket, bucket)


def not_scanned_bucket(row: dict[str, Any], scan_by_symbol: dict[str, list[tuple[datetime, dict[str, Any]]]]) -> str:
    symbol = str(row.get("symbol") or "")
    ts = parse_dt(row.get("ts"))
    scans = scan_by_symbol.get(symbol) or []
    if not scans:
        return "never_scanned_in_mirror"
    if not ts:
        return "scanned_outside_window"
    nearest_delta = min(abs((scan_ts - ts).total_seconds()) for scan_ts, _scan in scans)
    if nearest_delta <= NEAR_MISS_WINDOW_MIN * 60:
        return "near_window_gap"
    return "scanned_outside_window"


def not_scanned_label(bucket: str) -> str:
    labels = {
        "never_scanned_in_mirror": "镜像内从未被策略扫描",
        "near_window_gap": "扫描窗口错过",
        "scanned_outside_window": "只在较远时间被扫描",
    }
    return labels.get(bucket, bucket)


def compute_watchlist_history_summary(history: list[dict[str, Any]], days: int) -> dict[str, Any]:
    symbols: set[str] = set()
    for row in history:
        symbols.update(str(symbol) for symbol in row.get("symbols") or [])
    return {
        "available": bool(history),
        "days": days,
        "snapshots": len(history),
        "unique_symbols": len(symbols),
        "first_ts": history[0].get("ts") if history else None,
        "last_ts": history[-1].get("ts") if history else None,
    }


def build_scan_index(scanned: list[dict[str, Any]]) -> dict[str, list[tuple[datetime, dict[str, Any]]]]:
    scan_by_symbol: dict[str, list[tuple[datetime, dict[str, Any]]]] = {}
    for row in scanned:
        symbol = str(row.get("symbol") or "")
        ts = parse_dt(row.get("ts"))
        if symbol and ts:
            scan_by_symbol.setdefault(symbol, []).append((ts, row))
    for rows in scan_by_symbol.values():
        rows.sort(key=lambda item: item[0])
    return scan_by_symbol


def find_matching_scans(
    row: dict[str, Any],
    scan_by_symbol: dict[str, list[tuple[datetime, dict[str, Any]]]],
) -> list[dict[str, Any]]:
    symbol = str(row.get("symbol") or "")
    ts = parse_dt(row.get("ts"))
    scans = scan_by_symbol.get(symbol) or []
    if not ts or not scans:
        return []
    times = [item[0] for item in scans]
    idx = bisect.bisect_left(times, ts - timedelta(minutes=BUS_SCAN_LOOKBACK_MIN))
    matches: list[dict[str, Any]] = []
    while idx < len(scans) and scans[idx][0] <= ts + timedelta(minutes=BUS_COVERAGE_WINDOW_MIN):
        matches.append(scans[idx][1])
        idx += 1
    return matches


def choose_representative_scan(matches: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not matches:
        return None
    priority = {
        "opened": 0,
        "execution_rejected": 1,
        "risk_rejected": 2,
        "confirmation_rejected": 3,
        "strategy_rejected": 4,
        "market_data_rejected": 5,
        "pre_filter_rejected": 6,
        "analysis_or_data_error": 7,
        "scanned_no_signal": 8,
        "scanned_other": 9,
    }
    return sorted(matches, key=lambda item: priority.get(attribution_bucket(item), 99))[0]


def compute_bus_coverage(signals: list[dict[str, Any]], scanned: list[dict[str, Any]]) -> dict[str, Any]:
    scan_by_symbol = build_scan_index(scanned)

    big_signals = [row for row in signals if safe_float(row.get("abs_change_pct")) >= BIG_MOVE_ABS_PCT]
    covered = 0
    missed: list[dict[str, Any]] = []
    bucket_counts: dict[str, int] = {}
    not_scanned_counts: dict[str, int] = {}
    examples_by_bucket: dict[str, list[dict[str, Any]]] = {}
    not_scanned_examples: dict[str, list[dict[str, Any]]] = {}
    scan_results: dict[str, int] = {}
    stages: dict[str, int] = {}
    for row in big_signals:
        matches = find_matching_scans(row, scan_by_symbol)
        representative = choose_representative_scan(matches)
        bucket = attribution_bucket(representative)
        missed_sub_bucket = not_scanned_bucket(row, scan_by_symbol) if bucket == "not_scanned" else ""
        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
        if missed_sub_bucket:
            not_scanned_counts[missed_sub_bucket] = not_scanned_counts.get(missed_sub_bucket, 0) + 1
        if representative:
            covered += 1
            scan_result = str(representative.get("sentinel_scan_result") or "unknown")
            stage = str(representative.get("decision_stage") or "unknown")
            scan_results[scan_result] = scan_results.get(scan_result, 0) + 1
            stages[stage] = stages.get(stage, 0) + 1
        example = {
            "symbol": str(row.get("symbol") or ""),
            "ts": row.get("ts"),
            "reason": row.get("reason"),
            "change_pct": round(safe_float(row.get("change_pct")), 4),
            "velocity_pct": round(safe_float(row.get("velocity_pct")), 4),
            "quote_volume": round(safe_float(row.get("quote_volume")), 2),
            "attribution": bucket,
            "attribution_label": attribution_label(bucket),
            "not_scanned_bucket": missed_sub_bucket,
            "not_scanned_label": not_scanned_label(missed_sub_bucket) if missed_sub_bucket else "",
        }
        if representative:
            example.update(
                {
                    "strategy": representative.get("strategy"),
                    "scan_ts": representative.get("ts"),
                    "scan_result": representative.get("sentinel_scan_result"),
                    "decision_stage": representative.get("decision_stage"),
                    "filter_layer": representative.get("filter_layer"),
                    "scan_reason": representative.get("reason"),
                    "score": representative.get("score"),
                    "side": representative.get("side"),
                }
            )
        examples_by_bucket.setdefault(bucket, [])
        if len(examples_by_bucket[bucket]) < 5:
            examples_by_bucket[bucket].append(example)
        if missed_sub_bucket:
            not_scanned_examples.setdefault(missed_sub_bucket, [])
            if len(not_scanned_examples[missed_sub_bucket]) < 5:
                not_scanned_examples[missed_sub_bucket].append(example)
        if bucket == "not_scanned" and len(missed) < 15:
            missed.append(example)
    total = len(big_signals)
    attribution = {
        "window_minutes": BUS_COVERAGE_WINDOW_MIN,
        "scan_lookback_minutes": BUS_SCAN_LOOKBACK_MIN,
        "buckets": [
            {
                "bucket": key,
                "label": attribution_label(key),
                "count": count,
                "pct": round(count / max(total, 1) * 100.0, 2),
                "examples": examples_by_bucket.get(key, []),
            }
            for key, count in sorted(bucket_counts.items(), key=lambda item: item[1], reverse=True)
        ],
        "scan_results": dict(sorted(scan_results.items(), key=lambda item: item[1], reverse=True)),
        "decision_stages": dict(sorted(stages.items(), key=lambda item: item[1], reverse=True)),
        "not_scanned_breakdown": [
            {
                "bucket": key,
                "label": not_scanned_label(key),
                "count": count,
                "pct_of_big_moves": round(count / max(total, 1) * 100.0, 2),
                "pct_of_not_scanned": round(count / max(bucket_counts.get("not_scanned", 0), 1) * 100.0, 2),
                "examples": not_scanned_examples.get(key, []),
            }
            for key, count in sorted(not_scanned_counts.items(), key=lambda item: item[1], reverse=True)
        ],
    }
    return {
        "bus_signals": len(signals),
        "big_move_threshold_abs_pct": BIG_MOVE_ABS_PCT,
        "big_move_signals": total,
        "covered_big_move_signals": covered,
        "coverage_pct": round(covered / max(total, 1) * 100.0, 2),
        "missed_examples": missed,
        "attribution": attribution,
    }


def build_output(stats: dict, events: list[dict], scanned: list[dict], bus_signals: list[dict], watchlist_history: list[dict[str, Any]], days: int) -> dict[str, Any]:
    """Build the complete sentinel quality output."""
    now = datetime.now(CST)
    opened = [e for e in events if e.get("event_type") == "OPEN"]
    skipped = [e for e in events if e.get("event_type") == "OPEN_SKIPPED"]

    return {
        "generated_at": now.isoformat(),
        "summary": {
            "total_sentinel_decisions": stats["total_sentinel_decisions"],
            "total_scanned": stats["total_scanned"],
            "unique_symbols_scanned": stats["unique_symbols_scanned"],
            "unique_symbols_opened": stats["unique_symbols_opened"],
            "open_rate": round(len(opened) / max(stats["total_sentinel_decisions"], 1) * 100, 1),
            "skip_rate": round(len(skipped) / max(stats["total_sentinel_decisions"], 1) * 100, 1),
        },
        "response_breakdown": stats["response_breakdown"],
        "reason_stats": stats["reason_stats"],
        "strategy_stats": stats["strategy_stats"],
        "top_movers": stats["top_movers"],
        "forward_returns": compute_forward_returns(scanned),
        "coverage": compute_bus_coverage(bus_signals, scanned),
        "watchlist_history": compute_watchlist_history_summary(watchlist_history, days),
    }


def write_json(output: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def write_markdown(output: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    s = output["summary"]
    lines = [
        "# 哨兵贡献评估",
        "",
        f"- 生成时间: {output['generated_at']}",
        "",
        "## 总览",
        "",
        f"- 哨兵相关决策总数: {s['total_sentinel_decisions']}",
        f"- 哨兵扫描事件总数: {s['total_scanned']}",
        f"- 扫描去重币种: {s['unique_symbols_scanned']}",
        f"- 开仓去重币种: {s['unique_symbols_opened']}",
        f"- 开仓率: {s['open_rate']}%",
        f"- 跳过率: {s['skip_rate']}%",
        "",
        "## 策略响应分布",
        "",
        "| 响应类型 | 数量 |",
        "|----------|-----:|",
    ]
    for resp, count in output.get("response_breakdown", {}).items():
        lines.append(f"| {resp} | {count} |")

    lines.extend(["", "## 按哨兵原因统计", ""])
    lines.append("| 原因 | 总数 | 开仓 | 跳过 | 过滤 | 无信号 |")
    lines.append("|------|-----:|-----:|-----:|-----:|------:|")
    for reason, stats in sorted(output.get("reason_stats", {}).items(), key=lambda x: x[1]["total"], reverse=True):
        lines.append(
            f"| {reason} | {stats['total']} | {stats['opened']} | {stats['skipped']} | {stats['filtered']} | {stats['no_signal']} |"
        )

    lines.extend(["", "## 按策略统计", ""])
    lines.append("| 策略 | 总数 | 开仓 | 跳过 | 过滤 |")
    lines.append("|------|-----:|-----:|-----:|-----:|")
    for strategy, stats in sorted(output.get("strategy_stats", {}).items()):
        lines.append(
            f"| {strategy} | {stats['total']} | {stats['opened']} | {stats['skipped']} | {stats['filtered']} |"
        )

    lines.extend(["", "## Top 20 异动币种", ""])
    lines.append("| 币种 | 涨跌幅 | 加速度 | 原因 | 策略 | 时间 |")
    lines.append("|------|-------:|-------:|------|------|------|")
    for m in output.get("top_movers", [])[:20]:
        lines.append(
            f"| {m['symbol']} | {m['change_pct']:+.2f}% | {m['velocity_pct']:.2f}% | {m['reason']} | {m['strategy']} | {m['ts'][:16]} |"
        )

    lines.extend(["", "## 前向收益审计", ""])
    lines.append("基于后续哨兵扫描中的同币种价格近似估算，不调用交易所 API；方向收益按哨兵涨跌方向扣除双边 taker 费。")
    lines.append("")
    lines.append("| 窗口 | 样本 | 原始均值 | 原始中位数 | 方向扣费后均值 | 方向胜率 |")
    lines.append("|------|-----:|---------:|-----------:|---------------:|---------:|")
    for label, row in (output.get("forward_returns", {}).get("by_horizon") or {}).items():
        raw = row.get("raw") or {}
        directional = row.get("directional_after_fee") or {}
        lines.append(
            f"| {label} | {int(directional.get('samples') or 0)} | "
            f"{float(raw.get('avg_pct') or 0):+.4f}% | {float(raw.get('median_pct') or 0):+.4f}% | "
            f"{float(directional.get('avg_pct') or 0):+.4f}% | {float(directional.get('win_rate_pct') or 0):.2f}% |"
        )

    lines.extend(["", "## 大行情覆盖审计", ""])
    coverage = output.get("coverage") or {}
    lines.append(
        f"- 哨兵总线信号: {int(coverage.get('bus_signals') or 0)}；"
        f"大行情阈值: abs(change) >= {float(coverage.get('big_move_threshold_abs_pct') or 0):.1f}%；"
        f"大行情信号: {int(coverage.get('big_move_signals') or 0)}；"
        f"进入策略扫描覆盖: {int(coverage.get('covered_big_move_signals') or 0)}；"
        f"覆盖率: {float(coverage.get('coverage_pct') or 0):.2f}%"
    )
    lines.append("")
    lines.append("| 未覆盖样例 | 涨跌幅 | 加速度 | 成交额 | 时间 |")
    lines.append("|------------|-------:|-------:|-------:|------|")
    for row in coverage.get("missed_examples") or []:
        lines.append(
            f"| {row.get('symbol')} | {float(row.get('change_pct') or 0):+.2f}% | "
            f"{float(row.get('velocity_pct') or 0):+.2f}% | {float(row.get('quote_volume') or 0):.0f} | {str(row.get('ts') or '')[:16]} |"
        )
    attribution = coverage.get("attribution") or {}
    lines.extend(["", "## 大行情归因首版", ""])
    lines.append("归因按大行情触发后 5 分钟前到 30 分钟后是否进入策略扫描，以及扫描结果的 stage/layer 粗分；仍需后续接完整 replay/fill。")
    lines.append("")
    lines.append("| 归因 | 数量 | 占比 | 样例 |")
    lines.append("|------|-----:|-----:|------|")
    for bucket in attribution.get("buckets") or []:
        examples = ", ".join(str(row.get("symbol") or "") for row in (bucket.get("examples") or [])[:4])
        lines.append(
            f"| {bucket.get('label') or bucket.get('bucket')} | {int(bucket.get('count') or 0)} | "
            f"{float(bucket.get('pct') or 0):.2f}% | {examples} |"
        )
    if attribution.get("scan_results"):
        top_results = "；".join(f"{k}: {v}" for k, v in list(attribution.get("scan_results", {}).items())[:8])
        lines.append("")
        lines.append(f"- 已扫描大行情 scan_result 分布: {top_results}")
    not_scanned_breakdown = attribution.get("not_scanned_breakdown") or []
    if not_scanned_breakdown:
        lines.extend(["", "### 未进入策略扫描细分", ""])
        lines.append("| 细分 | 数量 | 占全部大行情 | 占未扫描 | 样例 |")
        lines.append("|------|-----:|-------------:|---------:|------|")
        for bucket in not_scanned_breakdown:
            examples = ", ".join(str(row.get("symbol") or "") for row in (bucket.get("examples") or [])[:4])
            lines.append(
                f"| {bucket.get('label') or bucket.get('bucket')} | {int(bucket.get('count') or 0)} | "
                f"{float(bucket.get('pct_of_big_moves') or 0):.2f}% | "
                f"{float(bucket.get('pct_of_not_scanned') or 0):.2f}% | {examples} |"
            )
    watchlist_history = output.get("watchlist_history") or {}
    lines.extend(["", "## Watchlist 历史覆盖", ""])
    if watchlist_history.get("available"):
        lines.append(
            f"- watchlist snapshots: {int(watchlist_history.get('snapshots') or 0)}；"
            f"去重币种: {int(watchlist_history.get('unique_symbols') or 0)}；"
            f"范围: {watchlist_history.get('first_ts')} ~ {watchlist_history.get('last_ts')}"
        )
    else:
        lines.append("- 暂无 durable watchlist snapshot 历史；已上线后续采集，下一轮数据可用于区分未进 watchlist 与镜像/扫描缺口。")

    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sentinel Quality Review")
    parser.add_argument("--db", default=None, help="Path to event_store.sqlite3")
    parser.add_argument("--runtime-dir", default=None, help="Runtime output directory")
    parser.add_argument("--reports-dir", default=None, help="Reports output directory")
    parser.add_argument("--days", type=int, default=7, help="Lookback days")
    args = parser.parse_args(argv)

    script_dir = Path(__file__).resolve().parent
    root = script_dir.parent if script_dir.name == "部署工具" else script_dir

    db_path = Path(args.db) if args.db else root / "runtime" / "event_store.sqlite3"
    runtime_dir = Path(args.runtime_dir) if args.runtime_dir else root / "runtime"
    reports_dir = Path(args.reports_dir) if args.reports_dir else root / "reports"

    if not db_path.exists():
        print(f"ERROR: Database not found: {db_path}")
        return 1

    con = sqlite3.connect(str(db_path))
    try:
        print(f"Loading sentinel data from {db_path} (last {args.days} days)...")
        events = load_sentinel_decisions(con, days=args.days)
        scanned = load_sentinel_scanned(con, days=args.days)
        bus_signals = load_sentinel_bus_signals(con, days=args.days)
        watchlist_history = load_watchlist_history(watchlist_history_root(db_path, root), days=args.days)

        print(f"  Sentinel decisions: {len(events)}")
        print(f"  Sentinel scanned: {len(scanned)}")
        print(f"  Sentinel bus signals: {len(bus_signals)}")
        print(f"  Watchlist snapshots: {len(watchlist_history)}")

        stats = compute_sentinel_stats(events, scanned)
        output = build_output(stats, events, scanned, bus_signals, watchlist_history, args.days)

        json_path = runtime_dir / "sentinel_quality_latest.json"
        md_path = reports_dir / "sentinel_quality_latest.md"
        write_json(output, json_path)
        write_markdown(output, md_path)

        print(f"\nOutput:")
        print(f"  JSON: {json_path}")
        print(f"  MD:   {md_path}")

        s = output["summary"]
        print(f"\n=== Summary ===")
        print(f"Sentinel decisions: {s['total_sentinel_decisions']}")
        print(f"Scanned: {s['total_scanned']} ({s['unique_symbols_scanned']} unique symbols)")
        print(f"Opened: {s['unique_symbols_opened']} symbols")
        print(f"Open rate: {s['open_rate']}%")
        print(f"Skip rate: {s['skip_rate']}%")
        print()
        print("Response breakdown:")
        for resp, count in output.get("response_breakdown", {}).items():
            print(f"  {resp}: {count}")
        print()
        print("Top 5 reasons:")
        for reason, rs in sorted(output.get("reason_stats", {}).items(), key=lambda x: x[1]["total"], reverse=True)[:5]:
            print(f"  {reason}: total={rs['total']} opened={rs['opened']} skipped={rs['skipped']}")
        coverage = output.get("coverage") or {}
        print(f"Coverage: {coverage.get('coverage_pct')}% ({coverage.get('covered_big_move_signals')}/{coverage.get('big_move_signals')})")
        attribution = (coverage.get("attribution") or {}).get("buckets") or []
        if attribution:
            print("Attribution:")
            for row in attribution[:5]:
                print(f"  {row.get('bucket')}: {row.get('count')} ({row.get('pct')}%)")
        not_scanned = (coverage.get("attribution") or {}).get("not_scanned_breakdown") or []
        if not_scanned:
            print("Not scanned breakdown:")
            for row in not_scanned[:5]:
                print(f"  {row.get('bucket')}: {row.get('count')} ({row.get('pct_of_not_scanned')}%)")

        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
