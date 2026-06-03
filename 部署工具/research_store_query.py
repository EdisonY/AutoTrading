"""Query the offline research store with DuckDB.

This script is the first read side of the Parquet/DuckDB warehouse. It does not
touch live services; it summarizes exported research_store partitions into JSON
and Markdown for replay/evolution work.
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

TABLES = ("events", "sentinel_scans", "account_snapshots", "klines", "features")
DEFAULT_KLINE_TARGET_DAYS = 30
DEFAULT_KLINE_KEY_INTERVALS = ("15m", "30m", "1h")


def now_cst() -> datetime:
    return datetime.now(CST)


def json_dump(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def md_table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "_No rows._"
    out = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        out.append("| " + " | ".join(str(x) for x in row) + " |")
    return "\n".join(out)


def table_glob(store: Path, table: str, fmt: str) -> str | None:
    suffix = "parquet" if fmt == "parquet" else "jsonl"
    files = sorted((store / table).glob(f"date=*/data.{suffix}"))
    if not files:
        return None
    return (store / table / "date=*" / f"data.{suffix}").as_posix()


def register_view(con: Any, store: Path, table: str, fmt: str) -> bool:
    glob = table_glob(store, table, fmt)
    if not glob:
        return False
    escaped = glob.replace("'", "''")
    if fmt == "parquet":
        con.execute(f"create or replace view {table} as select * from read_parquet('{escaped}', union_by_name=true)")
    else:
        con.execute(f"create or replace view {table} as select * from read_json_auto('{escaped}', union_by_name=true)")
    return True


def query_dicts(con: Any, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    cur = con.execute(sql, params or [])
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def build_kline_acceptance(
    rows: list[dict[str, Any]],
    target_days: int,
    key_intervals: list[str],
) -> dict[str, Any]:
    by_interval = {str(row.get("interval") or ""): row for row in rows if row.get("interval")}
    missing = [interval for interval in key_intervals if interval not in by_interval]
    gaps = [
        interval
        for interval in key_intervals
        if interval in by_interval and int(by_interval[interval].get("coverage_days") or 0) < target_days
    ]
    required_met = [interval for interval in key_intervals if interval in by_interval and interval not in gaps]
    met_intervals = [str(row.get("interval")) for row in rows if bool(row.get("target_met"))]
    coverage_values = [int(row.get("coverage_days") or 0) for row in rows]
    if not rows:
        status = "no_klines"
    elif missing or gaps:
        status = "coverage_gap"
    else:
        status = "ok"
    return {
        "status": status,
        "target_met": status == "ok",
        "target_days": target_days,
        "key_intervals": key_intervals,
        "required_interval_count": len(key_intervals),
        "met_required_interval_count": len(required_met),
        "met_intervals": met_intervals,
        "required_met_intervals": required_met,
        "missing_intervals": missing,
        "gap_intervals": gaps,
        "min_coverage_days": min(coverage_values) if coverage_values else 0,
        "max_coverage_days": max(coverage_values) if coverage_values else 0,
    }


def build_summary(
    con: Any,
    available: dict[str, bool],
    days: int,
    kline_target_days: int = DEFAULT_KLINE_TARGET_DAYS,
    kline_key_intervals: list[str] | None = None,
) -> dict[str, Any]:
    cutoff = (now_cst() - timedelta(days=days)).strftime("%Y-%m-%d")
    key_intervals = kline_key_intervals or list(DEFAULT_KLINE_KEY_INTERVALS)
    summary: dict[str, Any] = {
        "days": days,
        "cutoff": cutoff,
        "available_tables": [name for name, ok in available.items() if ok],
        "strategy_funnel": [],
        "skip_layers": [],
        "sentinel": [],
        "latest_accounts": [],
        "kline_coverage": [],
        "kline_acceptance": build_kline_acceptance([], kline_target_days, key_intervals),
        "feature_coverage": [],
    }
    if available.get("events"):
        summary["strategy_funnel"] = query_dicts(
            con,
            """
            select
              coalesce(nullif(strategy, ''), 'unknown') as strategy,
              count(*) as events,
              sum(case when event_type='SIGNAL' or category='entry_candidate' then 1 else 0 end) as signals,
              sum(case when event_type='OPEN' or category='opened' then 1 else 0 end) as opens,
              sum(case when event_type in ('CLOSE','FORCED_CLOSE') then 1 else 0 end) as closes,
              sum(case when event_type='OPEN_SKIPPED' then 1 else 0 end) as open_skipped,
              sum(case when event_type='OPEN_FAILED' then 1 else 0 end) as open_failed,
              round(avg(score), 2) as avg_score,
              max(ts) as latest_ts
            from events
            where ts >= ?
            group by 1
            order by strategy
            """,
            [cutoff],
        )
        summary["skip_layers"] = query_dicts(
            con,
            """
            select
              coalesce(nullif(strategy, ''), 'unknown') as strategy,
              coalesce(nullif(stage, ''), nullif(layer, ''), 'unknown') as gate,
              count(*) as n
            from events
            where ts >= ? and event_type='OPEN_SKIPPED'
            group by 1, 2
            order by n desc
            limit 20
            """,
            [cutoff],
        )
    if available.get("sentinel_scans"):
        summary["sentinel"] = query_dicts(
            con,
            """
            select
              coalesce(nullif(strategy, ''), 'unknown') as strategy,
              coalesce(nullif(scan_result, ''), nullif(category, ''), 'unknown') as result,
              count(*) as scans,
              round(avg(change_pct), 3) as avg_change_pct,
              round(avg(quote_volume), 2) as avg_quote_volume
            from sentinel_scans
            where ts >= ?
            group by 1, 2
            order by scans desc
            limit 30
            """,
            [cutoff],
        )
    if available.get("account_snapshots"):
        summary["latest_accounts"] = query_dicts(
            con,
            """
            with ranked as (
              select *,
                     row_number() over(partition by account order by ts desc) as rn
              from account_snapshots
            )
            select account, ts, wallet_usdt, available_usdt, margin_usdt,
                   unrealized_pnl_usdt, open_positions
            from ranked
            where rn=1
            order by account
            """,
        )
    if available.get("klines"):
        summary["kline_coverage"] = query_dicts(
            con,
            """
            with normalized as (
              select
                interval,
                symbol,
                cast(open_time as varchar) as open_time,
                cast("date" as varchar) as bar_date
              from klines
              where coalesce(nullif(cast(open_time as varchar), ''), nullif(cast("date" as varchar), '')) >= ?
            )
            select
              interval,
              count(*) as bars,
              count(distinct symbol) as symbols,
              count(distinct bar_date) as coverage_days,
              date_diff('day', try_cast(min(bar_date) as date), try_cast(max(bar_date) as date)) + 1 as span_days,
              min(open_time) as first_bar,
              max(open_time) as latest_bar,
              ? as target_days,
              count(distinct bar_date) >= ? as target_met
            from normalized
            where coalesce(interval, '') <> '' and coalesce(bar_date, '') <> ''
            group by 1
            order by coverage_days desc, bars desc
            """,
            [cutoff, kline_target_days, kline_target_days],
        )
        summary["kline_acceptance"] = build_kline_acceptance(
            summary["kline_coverage"],
            kline_target_days,
            key_intervals,
        )
    if available.get("features"):
        summary["feature_coverage"] = query_dicts(
            con,
            """
            select
              interval,
              count(*) as rows,
              count(distinct symbol) as symbols,
              round(avg(abs(return_1_pct)), 4) as avg_abs_return_1_pct,
              round(avg(range_pct), 4) as avg_range_pct,
              max(open_time) as latest_bar
            from features
            where open_time >= ?
            group by 1
            order by rows desc
            """,
            [cutoff],
        )
    return summary


def render_md(payload: dict[str, Any]) -> str:
    funnel_rows = [
        [
            r.get("strategy"),
            r.get("events"),
            r.get("signals"),
            r.get("opens"),
            r.get("closes"),
            r.get("open_skipped"),
            r.get("open_failed"),
            r.get("latest_ts"),
        ]
        for r in payload.get("strategy_funnel", [])
    ]
    skip_rows = [[r.get("strategy"), r.get("gate"), r.get("n")] for r in payload.get("skip_layers", [])[:12]]
    sentinel_rows = [
        [r.get("strategy"), r.get("result"), r.get("scans"), r.get("avg_change_pct"), r.get("avg_quote_volume")]
        for r in payload.get("sentinel", [])[:12]
    ]
    account_rows = [
        [
            r.get("account"),
            r.get("unrealized_pnl_usdt"),
            r.get("open_positions"),
            r.get("available_usdt"),
            r.get("ts"),
        ]
        for r in payload.get("latest_accounts", [])
    ]
    kline_rows = [
        [
            r.get("interval"),
            r.get("bars"),
            r.get("symbols"),
            r.get("coverage_days"),
            r.get("target_days"),
            "yes" if r.get("target_met") else "no",
            r.get("first_bar"),
            r.get("latest_bar"),
        ]
        for r in payload.get("kline_coverage", [])
    ]
    kline_acceptance = payload.get("kline_acceptance") or {}
    feature_rows = [
        [r.get("interval"), r.get("rows"), r.get("symbols"), r.get("avg_abs_return_1_pct"), r.get("avg_range_pct"), r.get("latest_bar")]
        for r in payload.get("feature_coverage", [])
    ]
    return "\n\n".join(
        [
            "# Research Store Summary",
            f"- Generated: `{payload.get('generated_at')}`",
            f"- Store: `{payload.get('store_dir')}`",
            f"- Window: last `{payload.get('days')}` days from `{payload.get('cutoff')}`",
            f"- Tables: `{', '.join(payload.get('available_tables') or []) or 'none'}`",
            (
                f"- Kline acceptance: `{kline_acceptance.get('status', 'unknown')}`; "
                f"target `{kline_acceptance.get('target_days', '-')}` days; "
                f"required `{kline_acceptance.get('met_required_interval_count', 0)}/"
                f"{kline_acceptance.get('required_interval_count', 0)}`; "
                f"missing `{', '.join(kline_acceptance.get('missing_intervals') or []) or '-'}`; "
                f"gap `{', '.join(kline_acceptance.get('gap_intervals') or []) or '-'}`"
            ),
            "## Strategy Funnel",
            md_table(["strategy", "events", "signals", "opens", "closes", "skipped", "failed", "latest"], funnel_rows),
            "## OPEN_SKIPPED Gates",
            md_table(["strategy", "gate", "n"], skip_rows),
            "## Sentinel Contribution",
            md_table(["strategy", "result", "scans", "avg_change", "avg_quote_volume"], sentinel_rows),
            "## Latest Accounts",
            md_table(["account", "upnl", "positions", "available", "ts"], account_rows),
            "## Kline Coverage",
            md_table(["interval", "bars", "symbols", "coverage_days", "target_days", "met", "first", "latest"], kline_rows),
            "## Feature Coverage",
            md_table(["interval", "rows", "symbols", "avg_abs_ret_1", "avg_range", "latest"], feature_rows),
        ]
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Query research_store partitions with DuckDB")
    parser.add_argument("--store", default=str(ROOT / "research_store"))
    parser.add_argument("--runtime-dir", default=str(ROOT / "runtime"))
    parser.add_argument("--reports-dir", default=str(ROOT / "reports"))
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--format", choices=["parquet", "jsonl"], default="parquet")
    parser.add_argument("--kline-target-days", type=int, default=DEFAULT_KLINE_TARGET_DAYS)
    parser.add_argument(
        "--kline-key-intervals",
        default=",".join(DEFAULT_KLINE_KEY_INTERVALS),
        help="Comma-separated intervals required for long-window kline acceptance",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        import duckdb
    except ImportError as exc:
        raise SystemExit("duckdb is required. Install requirements.txt before running research_store_query.py") from exc
    store = Path(args.store)
    runtime_dir = Path(args.runtime_dir)
    reports_dir = Path(args.reports_dir)
    with duckdb.connect(database=":memory:") as con:
        available = {table: register_view(con, store, table, args.format) for table in TABLES}
        key_intervals = [item.strip() for item in str(args.kline_key_intervals or "").split(",") if item.strip()]
        payload = build_summary(con, available, args.days, args.kline_target_days, key_intervals)
    payload.update(
        {
            "generated_at": now_cst().isoformat(timespec="seconds"),
            "store_dir": str(store),
            "format": args.format,
        }
    )
    json_path = runtime_dir / "research_store_summary_latest.json"
    md_path = reports_dir / "research_store_summary_latest.md"
    json_dump(json_path, payload)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_md(payload), encoding="utf-8")
    print(json.dumps({"json": str(json_path), "md": str(md_path), "tables": payload["available_tables"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
