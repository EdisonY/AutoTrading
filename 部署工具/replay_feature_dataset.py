"""Build a replay dataset by aligning live events with research-store features."""

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
ROOT = SCRIPT_DIR.parent if (SCRIPT_DIR.parent / "PROJECT_STATE.md").exists() else SCRIPT_DIR
CST = timezone(timedelta(hours=8))
EVENT_TYPES = ("OPEN", "OPEN_SKIPPED", "OPEN_FAILED", "SIGNAL")


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


def build_dataset(con: Any, days: int) -> list[dict[str, Any]]:
    cutoff = (now_cst() - timedelta(days=days)).strftime("%Y-%m-%d")
    placeholders = ", ".join("?" for _ in EVENT_TYPES)
    sql = f"""
    with event_rows as (
      select
        id as event_id,
        ts as event_ts,
        try_cast(ts as timestamp) as event_dt,
        strategy,
        symbol,
        event_type,
        category,
        side,
        score,
        stage,
        layer,
        reason,
        payload_json,
        coalesce(nullif(stage, ''), nullif(layer, ''), 'unknown_gate') as replay_gate,
        case
          when event_type='OPEN' then 'accepted_open'
          when event_type='SIGNAL' then 'candidate'
          when event_type='OPEN_SKIPPED' then 'rejected'
          when event_type='OPEN_FAILED' then 'execution_failed'
          else 'observed'
        end as replay_decision,
        case when event_type='OPEN' then true else false end as accepted
      from events
      where ts >= ?
        and event_type in ({placeholders})
        and strategy in ('A/v11','B/v16','C/v14')
        and coalesce(symbol, '') <> ''
    ),
    candidates as (
      select
        e.*,
        f.interval as feature_interval,
        f.open_time as feature_time,
        f.open_time_ms as feature_time_ms,
        f.close as feature_close,
        f.return_1_pct,
        f.return_3_pct,
        f.return_10_pct,
        f.body_pct,
        f.range_pct,
        f.quote_volume,
        row_number() over (
          partition by e.event_id
          order by
            case
              when lower(f.interval)=lower(coalesce(json_extract_string(e.payload_json, '$.timeframe'), '')) then 0
              when f.interval='15m' then 1
              when f.interval='30m' then 2
              when f.interval='1h' then 3
              else 9
            end,
            try_cast(f.open_time as timestamp) desc
        ) as rn
      from event_rows e
      left join features f
        on f.symbol = e.symbol
        and try_cast(f.open_time as timestamp) <= e.event_dt
        and try_cast(f.open_time as timestamp) >= e.event_dt - interval 2 day
    )
    select *
    from candidates
    where rn = 1
    order by event_dt, event_id
    """
    return query_dicts(con, sql, [cutoff, *EVENT_TYPES])


def write_dataset(rows: list[dict[str, Any]], out_dir: Path, fmt: str) -> dict[str, Any]:
    if not rows:
        return {"status": "empty", "rows": 0, "files": 0, "partitions": {}}
    try:
        import pandas as pd
    except ImportError as exc:
        raise SystemExit("pandas is required for replay_feature_dataset.py") from exc
    suffix = "parquet" if fmt == "parquet" else "jsonl"
    df = pd.DataFrame(rows)
    if "event_ts" not in df:
        return {"status": "empty", "rows": 0, "files": 0, "partitions": {}}
    df["date"] = df["event_ts"].astype(str).str.slice(0, 10)
    partitions: dict[str, dict[str, Any]] = {}
    files = 0
    for day, day_df in df.groupby("date", dropna=False):
        target = out_dir / "replay_features" / f"date={day}" / f"data.{suffix}"
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        if fmt == "parquet":
            try:
                day_df.to_parquet(tmp, index=False)
            except ImportError as exc:
                raise SystemExit("pyarrow is required for --format parquet. Install requirements or rerun with --format jsonl.") from exc
        else:
            day_df.to_json(tmp, orient="records", lines=True, force_ascii=False)
        tmp.replace(target)
        files += 1
        partitions[str(day)] = {"rows": int(len(day_df)), "path": str(target), "status": "written"}
    return {"status": "ok", "rows": int(len(df)), "files": files, "partitions": partitions}


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    matched = sum(1 for row in rows if row.get("feature_time"))
    by_strategy: dict[str, dict[str, Any]] = {}
    by_gate: dict[str, dict[str, Any]] = {}
    by_type: dict[str, dict[str, Any]] = {}
    for row in rows:
        for bucket, key in ((by_strategy, row.get("strategy") or "unknown"), (by_gate, row.get("replay_gate") or "unknown"), (by_type, row.get("event_type") or "unknown")):
            item = bucket.setdefault(str(key), {"events": 0, "matched": 0})
            item["events"] += 1
            if row.get("feature_time"):
                item["matched"] += 1
    for bucket in (by_strategy, by_gate, by_type):
        for item in bucket.values():
            item["match_rate"] = round(item["matched"] / max(1, item["events"]) * 100, 2)
    return {
        "events": total,
        "matched_features": matched,
        "match_rate": round(matched / max(1, total) * 100, 2),
        "by_strategy": by_strategy,
        "by_gate": dict(sorted(by_gate.items(), key=lambda kv: kv[1]["events"], reverse=True)[:20]),
        "by_event_type": by_type,
    }


def render_md(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    strategy_rows = [[k, v.get("events"), v.get("matched"), f"{v.get('match_rate')}%"] for k, v in (summary.get("by_strategy") or {}).items()]
    gate_rows = [[k, v.get("events"), v.get("matched"), f"{v.get('match_rate')}%"] for k, v in (summary.get("by_gate") or {}).items()]
    type_rows = [[k, v.get("events"), v.get("matched"), f"{v.get('match_rate')}%"] for k, v in (summary.get("by_event_type") or {}).items()]
    return "\n\n".join(
        [
            "# Replay Feature Dataset",
            f"- Generated: `{payload.get('generated_at')}`",
            f"- Store: `{payload.get('store_dir')}`",
            f"- Window: last `{payload.get('days')}` days",
            f"- Events: `{summary.get('events', 0)}`; matched features: `{summary.get('matched_features', 0)}`; match rate: `{summary.get('match_rate', 0)}%`",
            "## By Strategy",
            md_table(["strategy", "events", "matched", "match_rate"], strategy_rows),
            "## By Event Type",
            md_table(["event_type", "events", "matched", "match_rate"], type_rows),
            "## Top Replay Gates",
            md_table(["gate", "events", "matched", "match_rate"], gate_rows[:12]),
        ]
    ) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Align replay events with research_store features")
    parser.add_argument("--store", default=str(ROOT / "research_store"))
    parser.add_argument("--runtime-dir", default=str(ROOT / "runtime"))
    parser.add_argument("--reports-dir", default=str(ROOT / "reports"))
    parser.add_argument("--out-dir", default=str(ROOT / "research_store"))
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--format", choices=["parquet", "jsonl"], default="parquet")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        import duckdb
    except ImportError as exc:
        raise SystemExit("duckdb is required. Install requirements.txt before running replay_feature_dataset.py") from exc
    store = Path(args.store)
    runtime_dir = Path(args.runtime_dir)
    reports_dir = Path(args.reports_dir)
    out_dir = Path(args.out_dir)
    with duckdb.connect(database=":memory:") as con:
        have_events = register_view(con, store, "events", args.format)
        have_features = register_view(con, store, "features", args.format)
        rows = build_dataset(con, args.days) if have_events and have_features else []
    export = write_dataset(rows, out_dir, args.format)
    payload = {
        "generated_at": now_cst().isoformat(timespec="seconds"),
        "store_dir": str(store),
        "out_dir": str(out_dir),
        "days": args.days,
        "format": args.format,
        "available": {"events": have_events, "features": have_features},
        "summary": summarize(rows),
        "export": export,
    }
    json_path = runtime_dir / "replay_feature_dataset_latest.json"
    md_path = reports_dir / "replay_feature_dataset_latest.md"
    json_dump(json_path, payload)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_md(payload), encoding="utf-8")
    print(json.dumps({"json": str(json_path), "md": str(md_path), "rows": len(rows), "matched": payload["summary"]["matched_features"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
