"""Plan and optionally archive old research_store partitions.

Default mode is plan-only: inspect partition directories and write JSON/Markdown
reports. ``--apply`` moves archive candidates into an ignored archive directory;
it never deletes data or calls live services.
"""

from __future__ import annotations

import argparse
import json
import shutil
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
DEFAULT_TABLES = (
    "events",
    "sentinel_scans",
    "account_snapshots",
    "klines",
    "features",
    "depth_snapshots",
    "replay_features",
)


def now_cst() -> datetime:
    return datetime.now(CST)


def parse_now(value: str | None) -> datetime:
    if not value:
        return now_cst()
    text = str(value).strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt.astimezone(CST)


def parse_tables(value: str | None) -> list[str]:
    tables = [item.strip() for item in str(value or "").split(",") if item.strip()]
    return tables or list(DEFAULT_TABLES)


def partition_date(path: Path) -> str:
    name = path.name
    return name[5:] if name.startswith("date=") else ""


def parse_partition_date(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=CST)
    except Exception:
        return None


def count_jsonl_rows(path: Path) -> int | None:
    try:
        with path.open("rb") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return None


def data_file_for_partition(partition: Path, fmt: str) -> Path | None:
    suffix = "parquet" if fmt == "parquet" else "jsonl"
    preferred = partition / f"data.{suffix}"
    if preferred.exists():
        return preferred
    alternatives = sorted(partition.glob("data.*"))
    return alternatives[0] if alternatives else None


def classify_partition(day: str, *, now_dt: datetime, hot_days: int, retain_days: int) -> str:
    parsed = parse_partition_date(day)
    if parsed is None:
        return "invalid_date"
    age_days = max(0, (now_dt.date() - parsed.date()).days)
    if age_days < max(0, int(hot_days)):
        return "hot"
    if age_days < max(0, int(retain_days)):
        return "warm"
    return "archive_candidate"


def scan_store(
    store: Path,
    *,
    fmt: str,
    tables: list[str],
    hot_days: int,
    retain_days: int,
    now_dt: datetime,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for table in tables:
        table_dir = store / table
        for partition in sorted(table_dir.glob("date=*")):
            if not partition.is_dir():
                continue
            day = partition_date(partition)
            data_file = data_file_for_partition(partition, fmt)
            size_bytes = data_file.stat().st_size if data_file and data_file.exists() else 0
            row_count = count_jsonl_rows(data_file) if data_file and data_file.suffix == ".jsonl" else None
            parsed = parse_partition_date(day)
            age_days = max(0, (now_dt.date() - parsed.date()).days) if parsed else None
            rows.append(
                {
                    "table": table,
                    "date": day,
                    "path": str(partition),
                    "data_file": str(data_file) if data_file else "",
                    "format": (data_file.suffix[1:] if data_file else fmt),
                    "status": classify_partition(day, now_dt=now_dt, hot_days=hot_days, retain_days=retain_days),
                    "age_days": age_days,
                    "size_bytes": size_bytes,
                    "row_count": row_count,
                }
            )
    return rows


def ensure_within(path: Path, parent: Path) -> None:
    resolved = path.resolve()
    root = parent.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"path outside allowed root: {resolved}")


def archive_candidates(
    rows: list[dict[str, Any]],
    *,
    store: Path,
    archive_dir: Path,
) -> dict[str, Any]:
    store_root = store.resolve()
    archive_root = archive_dir.resolve()
    archive_dir.mkdir(parents=True, exist_ok=True)
    moved: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for row in rows:
        if row.get("status") != "archive_candidate":
            continue
        src = Path(str(row.get("path") or ""))
        if not src.exists():
            skipped.append({**row, "apply_status": "missing_source"})
            continue
        ensure_within(src, store_root)
        table = str(row.get("table") or "")
        day = str(row.get("date") or "")
        dst = archive_dir / table / f"date={day}"
        ensure_within(dst, archive_root)
        if dst.exists():
            skipped.append({**row, "apply_status": "archive_target_exists", "archive_path": str(dst)})
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        moved.append({**row, "apply_status": "moved", "archive_path": str(dst)})
    return {"moved": len(moved), "skipped": len(skipped), "moved_partitions": moved, "skipped_partitions": skipped}


def summarize(rows: list[dict[str, Any]], apply_result: dict[str, Any] | None = None) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    by_table: dict[str, dict[str, Any]] = {}
    total_bytes = 0
    archive_bytes = 0
    for row in rows:
        status = str(row.get("status") or "unknown")
        table = str(row.get("table") or "unknown")
        size = int(row.get("size_bytes") or 0)
        by_status[status] = by_status.get(status, 0) + 1
        total_bytes += size
        if status == "archive_candidate":
            archive_bytes += size
        table_summary = by_table.setdefault(
            table,
            {"table": table, "partitions": 0, "hot": 0, "warm": 0, "archive_candidate": 0, "invalid_date": 0, "bytes": 0},
        )
        table_summary["partitions"] += 1
        table_summary[status] = int(table_summary.get(status) or 0) + 1
        table_summary["bytes"] += size
    return {
        "partitions": len(rows),
        "by_status": by_status,
        "by_table": sorted(by_table.values(), key=lambda item: str(item.get("table"))),
        "total_bytes": total_bytes,
        "archive_candidate_bytes": archive_bytes,
        "archive_candidate_partitions": by_status.get("archive_candidate", 0),
        "apply_moved": int((apply_result or {}).get("moved") or 0),
        "apply_skipped": int((apply_result or {}).get("skipped") or 0),
    }


def md_table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "_No rows._"
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        out.append("| " + " | ".join(str(value) for value in row) + " |")
    return "\n".join(out)


def render_md(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    apply_result = payload.get("apply") if isinstance(payload.get("apply"), dict) else {}
    table_rows = [
        [
            row.get("table"),
            row.get("partitions"),
            row.get("hot"),
            row.get("warm"),
            row.get("archive_candidate"),
            row.get("invalid_date"),
            row.get("bytes"),
        ]
        for row in summary.get("by_table", [])
    ]
    sample_rows = [
        [
            row.get("table"),
            row.get("date"),
            row.get("status"),
            row.get("age_days"),
            row.get("size_bytes"),
            row.get("row_count") if row.get("row_count") is not None else "-",
        ]
        for row in (payload.get("partitions") or [])
        if isinstance(row, dict) and row.get("status") == "archive_candidate"
    ][:20]
    moved_rows = [
        [row.get("table"), row.get("date"), row.get("archive_path")]
        for row in apply_result.get("moved_partitions", [])
        if isinstance(row, dict)
    ][:20]
    return "\n\n".join(
        [
            "# Research Store Retention",
            f"- Generated: `{payload.get('generated_at')}`",
            f"- Mode: `{'apply' if payload.get('apply_enabled') else 'plan_only'}`",
            f"- Store: `{payload.get('store_dir')}`",
            f"- Archive dir: `{payload.get('archive_dir')}`",
            f"- Hot days: `{payload.get('hot_days')}`; retain days: `{payload.get('retain_days')}`",
            f"- Partitions: `{summary.get('partitions', 0)}`; archive candidates `{summary.get('archive_candidate_partitions', 0)}`; moved `{summary.get('apply_moved', 0)}`; skipped `{summary.get('apply_skipped', 0)}`",
            "- Note: default mode only plans. Apply mode moves old partitions to archive; it does not delete data or call Binance.",
            "## Tables",
            md_table(["table", "partitions", "hot", "warm", "archive", "invalid", "bytes"], table_rows),
            "## Archive Candidates",
            md_table(["table", "date", "status", "age_days", "bytes", "rows"], sample_rows),
            "## Applied Moves",
            md_table(["table", "date", "archive_path"], moved_rows),
        ]
    )


def write_outputs(runtime_dir: Path, reports_dir: Path, payload: dict[str, Any]) -> None:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "research_store_retention_latest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    (reports_dir / "research_store_retention_latest.md").write_text(render_md(payload), encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan/apply research_store partition retention")
    parser.add_argument("--store", default=str(ROOT / "research_store"))
    parser.add_argument("--archive-dir", default=str(ROOT / "research_store_archive"))
    parser.add_argument("--runtime-dir", default=str(ROOT / "runtime"))
    parser.add_argument("--reports-dir", default=str(ROOT / "reports"))
    parser.add_argument("--tables", default=",".join(DEFAULT_TABLES))
    parser.add_argument("--hot-days", type=int, default=14)
    parser.add_argument("--retain-days", type=int, default=90)
    parser.add_argument("--format", choices=["parquet", "jsonl"], default="parquet")
    parser.add_argument("--now", default="")
    parser.add_argument("--apply", action="store_true", help="Move archive candidates to --archive-dir")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    store = Path(args.store)
    archive_dir = Path(args.archive_dir)
    now_dt = parse_now(args.now)
    rows = scan_store(
        store,
        fmt=args.format,
        tables=parse_tables(args.tables),
        hot_days=args.hot_days,
        retain_days=args.retain_days,
        now_dt=now_dt,
    )
    apply_result: dict[str, Any] = {}
    if args.apply:
        apply_result = archive_candidates(rows, store=store, archive_dir=archive_dir)
    payload = {
        "generated_at": now_cst().isoformat(timespec="seconds"),
        "store_dir": str(store),
        "archive_dir": str(archive_dir),
        "format": args.format,
        "tables": parse_tables(args.tables),
        "hot_days": int(args.hot_days),
        "retain_days": int(args.retain_days),
        "now": now_dt.isoformat(timespec="seconds"),
        "apply_enabled": bool(args.apply),
        "summary": summarize(rows, apply_result),
        "partitions": rows,
        "apply": apply_result,
        "live_impact": "none; plan-only by default; apply only moves local research_store partitions to archive",
    }
    write_outputs(Path(args.runtime_dir), Path(args.reports_dir), payload)
    print(
        json.dumps(
            {
                "partitions": payload["summary"]["partitions"],
                "archive_candidates": payload["summary"]["archive_candidate_partitions"],
                "moved": payload["summary"]["apply_moved"],
                "apply": bool(args.apply),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
