"""Export SQLite event-store tables into an offline research store.

This is the first step toward the Parquet/DuckDB research warehouse. It is
read-only against the live SQLite DB and writes partitioned files under the
ignored ``research_store/`` directory.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
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

TABLES = {
    "events": "substr(ts, 1, 10)",
    "sentinel_scans": "coalesce(date, substr(ts, 1, 10))",
    "account_snapshots": "substr(ts, 1, 10)",
}


def now_cst() -> datetime:
    return datetime.now(CST)


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "select 1 from sqlite_master where type='table' and name=? limit 1",
        (table,),
    ).fetchone()
    return bool(row)


def list_dates(conn: sqlite3.Connection, table: str, date_expr: str, cutoff: str) -> list[str]:
    rows = conn.execute(
        f"""
        select distinct {date_expr} as d
        from {table}
        where ts >= ? and {date_expr} is not null and length({date_expr}) >= 10
        order by d
        """,
        (cutoff,),
    ).fetchall()
    return [str(row[0])[:10] for row in rows if row and row[0]]


def read_table_day(conn: sqlite3.Connection, table: str, date_expr: str, day: str):
    try:
        import pandas as pd
    except ImportError as exc:
        raise SystemExit("pandas is required for research_store_export.py") from exc
    return pd.read_sql_query(
        f"select * from {table} where {date_expr} = ? order by id",
        conn,
        params=(day,),
    )


def write_frame(df: Any, path: Path, fmt: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    if fmt == "parquet":
        try:
            df.to_parquet(tmp, index=False)
        except ImportError as exc:
            raise SystemExit(
                "pyarrow is required for --format parquet. Install requirements or rerun with --format jsonl."
            ) from exc
    elif fmt == "jsonl":
        df.to_json(tmp, orient="records", lines=True, force_ascii=False)
    else:
        raise ValueError(f"Unsupported format: {fmt}")
    tmp.replace(path)


def read_manifest(out_dir: Path) -> dict[str, Any]:
    path = out_dir / "manifest_latest.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def day_stats(conn: sqlite3.Connection, table: str, date_expr: str, day: str) -> dict[str, Any]:
    row = conn.execute(
        f"select count(*) as rows, max(ts) as max_ts from {table} where {date_expr} = ?",
        (day,),
    ).fetchone()
    return {
        "rows": int(row[0] or 0) if row else 0,
        "max_ts": str(row[1] or "") if row else "",
    }


def export_table(
    conn: sqlite3.Connection,
    table: str,
    date_expr: str,
    out_dir: Path,
    cutoff: str,
    fmt: str,
    previous_partitions: dict[str, Any] | None = None,
    incremental: bool = True,
) -> dict[str, Any]:
    if not table_exists(conn, table):
        return {"table": table, "status": "missing", "files": 0, "rows": 0}
    dates = list_dates(conn, table, date_expr, cutoff)
    rows_total = 0
    scanned_rows = 0
    files = 0
    skipped_files = 0
    suffix = "parquet" if fmt == "parquet" else "jsonl"
    previous_partitions = previous_partitions or {}
    partitions: dict[str, dict[str, Any]] = {}
    for day in dates:
        stats = day_stats(conn, table, date_expr, day)
        scanned_rows += int(stats["rows"])
        if stats["rows"] <= 0:
            continue
        target = out_dir / table / f"date={day}" / f"data.{suffix}"
        previous = previous_partitions.get(day) if isinstance(previous_partitions, dict) else None
        if (
            incremental
            and target.exists()
            and isinstance(previous, dict)
            and int(previous.get("rows") or -1) == int(stats["rows"])
            and str(previous.get("max_ts") or "") == str(stats["max_ts"] or "")
        ):
            partitions[day] = {**stats, "path": str(target), "status": "skipped_unchanged"}
            skipped_files += 1
            continue
        df = read_table_day(conn, table, date_expr, day)
        if df.empty:
            continue
        write_frame(df, target, fmt)
        rows_total += int(len(df))
        files += 1
        partitions[day] = {**stats, "path": str(target), "status": "written"}
    return {
        "table": table,
        "status": "ok",
        "files": files,
        "skipped_files": skipped_files,
        "rows": rows_total,
        "scanned_rows": scanned_rows,
        "dates": dates,
        "partitions": partitions,
    }


def write_manifest(out_dir: Path, manifest: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "manifest_latest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export event_store.sqlite3 into research_store partitions")
    parser.add_argument("--db", default=str(ROOT / "runtime" / "event_store.sqlite3"))
    parser.add_argument("--out-dir", default=str(ROOT / "research_store"))
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--format", choices=["parquet", "jsonl"], default="parquet")
    parser.add_argument("--tables", nargs="*", default=list(TABLES), choices=list(TABLES))
    parser.add_argument("--force", action="store_true", help="Rewrite partitions even when manifest watermark is unchanged")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    db = Path(args.db)
    out_dir = Path(args.out_dir)
    if not db.exists():
        raise SystemExit(f"SQLite DB not found: {db}")
    cutoff = (now_cst() - timedelta(days=args.days)).strftime("%Y-%m-%d")
    started = now_cst().isoformat(timespec="seconds")
    previous_manifest = read_manifest(out_dir)
    previous_partitions = previous_manifest.get("partitions") if isinstance(previous_manifest.get("partitions"), dict) else {}
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        results = [
            export_table(
                conn,
                table,
                TABLES[table],
                out_dir,
                cutoff,
                args.format,
                previous_partitions.get(table) if isinstance(previous_partitions, dict) else {},
                incremental=not args.force,
            )
            for table in args.tables
        ]
    partitions = {
        result["table"]: result.get("partitions") or {}
        for result in results
        if result.get("status") == "ok"
    }
    manifest = {
        "generated_at": now_cst().isoformat(timespec="seconds"),
        "started_at": started,
        "db": str(db),
        "out_dir": str(out_dir),
        "days": args.days,
        "format": args.format,
        "incremental": not args.force,
        "results": results,
        "partitions": partitions,
    }
    write_manifest(out_dir, manifest)
    print(json.dumps(manifest, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
