"""Plan and optionally rewrite research_store partitions for compaction.

Default mode is plan-only. Apply mode first backs up the full partition into an
ignored backup directory, then atomically rewrites the selected data file. It
does not delete partitions, call Binance, or touch live services.
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


def parse_tables(value: str | None) -> list[str]:
    tables = [item.strip() for item in str(value or "").split(",") if item.strip()]
    return tables or list(DEFAULT_TABLES)


def partition_date(path: Path) -> str:
    name = path.name
    return name[5:] if name.startswith("date=") else ""


def current_format(data_file: Path | None, preferred: str) -> str:
    if data_file and data_file.suffix in {".parquet", ".jsonl"}:
        return data_file.suffix[1:]
    return preferred


def data_file_for_partition(partition: Path, fmt: str) -> Path | None:
    preferred = partition / f"data.{fmt}"
    if preferred.exists():
        return preferred
    alternatives = sorted(partition.glob("data.parquet")) + sorted(partition.glob("data.jsonl"))
    return alternatives[0] if alternatives else None


def count_jsonl_rows(path: Path) -> int:
    try:
        with path.open("rb") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


def parquet_stats(path: Path) -> dict[str, int]:
    try:
        import pyarrow.parquet as pq
    except ImportError:
        return {"row_count": 0, "row_groups": 0}
    try:
        metadata = pq.ParquetFile(path).metadata
    except Exception:
        return {"row_count": 0, "row_groups": 0}
    return {"row_count": int(metadata.num_rows or 0), "row_groups": int(metadata.num_row_groups or 0)}


def partition_stats(data_file: Path | None, fmt: str) -> dict[str, Any]:
    if not data_file or not data_file.exists():
        return {"size_bytes": 0, "row_count": 0, "row_groups": 0}
    size = data_file.stat().st_size
    if fmt == "jsonl":
        return {"size_bytes": size, "row_count": count_jsonl_rows(data_file), "row_groups": 0}
    stats = parquet_stats(data_file)
    return {"size_bytes": size, **stats}


def choose_status(
    *,
    data_file: Path | None,
    fmt: str,
    target_format: str,
    size_bytes: int,
    row_count: int,
    row_groups: int,
    min_bytes: int,
    min_rows: int,
    max_row_groups: int,
) -> tuple[str, list[str]]:
    if data_file is None:
        return "missing_data_file", ["missing_data_file"]
    reasons: list[str] = []
    if target_format != fmt:
        reasons.append("format_conversion")
    if size_bytes >= max(1, min_bytes):
        reasons.append("large_file")
    if row_count >= max(1, min_rows):
        reasons.append("many_rows")
    if row_groups > max(0, max_row_groups):
        reasons.append("many_row_groups")
    if not reasons:
        return "ok", []
    return "compact_candidate", reasons


def scan_store(
    store: Path,
    *,
    fmt: str,
    target_format: str,
    tables: list[str],
    min_bytes: int,
    min_rows: int,
    max_row_groups: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for table in tables:
        table_dir = store / table
        for partition in sorted(table_dir.glob("date=*")):
            if not partition.is_dir():
                continue
            data_file = data_file_for_partition(partition, fmt)
            source_format = current_format(data_file, fmt)
            final_format = source_format if target_format == "same" else target_format
            stats = partition_stats(data_file, source_format)
            status, reasons = choose_status(
                data_file=data_file,
                fmt=source_format,
                target_format=final_format,
                size_bytes=int(stats.get("size_bytes") or 0),
                row_count=int(stats.get("row_count") or 0),
                row_groups=int(stats.get("row_groups") or 0),
                min_bytes=min_bytes,
                min_rows=min_rows,
                max_row_groups=max_row_groups,
            )
            rows.append(
                {
                    "table": table,
                    "date": partition_date(partition),
                    "path": str(partition),
                    "data_file": str(data_file) if data_file else "",
                    "source_format": source_format,
                    "target_format": final_format,
                    "status": status,
                    "reasons": reasons,
                    "size_bytes": int(stats.get("size_bytes") or 0),
                    "row_count": int(stats.get("row_count") or 0),
                    "row_groups": int(stats.get("row_groups") or 0),
                }
            )
    return rows


def ensure_within(path: Path, parent: Path) -> None:
    resolved = path.resolve()
    root = parent.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"path outside allowed root: {resolved}")


def read_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except Exception:
                continue
            if isinstance(item, dict):
                rows.append(item)
    return rows


def write_rows(rows: list[dict[str, Any]], target: Path, fmt: str, compression: str) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    if fmt == "jsonl":
        with tmp.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")
    elif fmt == "parquet":
        try:
            import pandas as pd
        except ImportError as exc:
            raise SystemExit("pandas is required for parquet compaction") from exc
        frame = pd.DataFrame(rows)
        try:
            frame.to_parquet(tmp, index=False, compression=compression)
        except ImportError as exc:
            raise SystemExit("pyarrow is required for parquet compaction") from exc
    else:
        raise ValueError(f"Unsupported format: {fmt}")
    tmp.replace(target)
    return len(rows)


def can_rewrite_format(fmt: str) -> tuple[bool, str]:
    if fmt == "jsonl":
        return True, ""
    if fmt != "parquet":
        return False, f"unsupported_format:{fmt}"
    try:
        import pandas  # noqa: F401
        import pyarrow  # noqa: F401
    except ImportError as exc:
        return False, f"missing_dependency:{exc.name or 'parquet'}"
    return True, ""


def load_rows(data_file: Path, fmt: str) -> list[dict[str, Any]]:
    if fmt == "jsonl":
        return read_jsonl_rows(data_file)
    try:
        import pandas as pd
    except ImportError as exc:
        raise SystemExit("pandas is required for parquet compaction") from exc
    frame = pd.read_parquet(data_file)
    frame = frame.where(pd.notnull(frame), None)
    return [dict(row) for row in frame.to_dict(orient="records")]


def compact_candidates(
    rows: list[dict[str, Any]],
    *,
    store: Path,
    backup_dir: Path,
    compression: str,
    generated_at: datetime,
) -> dict[str, Any]:
    store_root = store.resolve()
    backup_root = backup_dir.resolve()
    batch_dir = backup_dir / generated_at.strftime("%Y%m%d-%H%M%S")
    compacted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for row in rows:
        if row.get("status") != "compact_candidate":
            continue
        partition = Path(str(row.get("path") or ""))
        data_file = Path(str(row.get("data_file") or ""))
        if not partition.exists() or not data_file.exists():
            skipped.append({**row, "apply_status": "missing_source"})
            continue
        ensure_within(partition, store_root)
        table = str(row.get("table") or "")
        day = str(row.get("date") or "")
        source_format = str(row.get("source_format") or "")
        target_format = str(row.get("target_format") or source_format)
        dependency_error = ""
        for fmt in {source_format, target_format}:
            ok, reason = can_rewrite_format(fmt)
            if not ok:
                dependency_error = reason
                break
        if dependency_error:
            skipped.append({**row, "apply_status": "unsupported_format", "error": dependency_error})
            continue
        backup_partition = batch_dir / table / f"date={day}"
        ensure_within(backup_partition, backup_root)
        if backup_partition.exists():
            skipped.append({**row, "apply_status": "backup_target_exists", "backup_path": str(backup_partition)})
            continue
        backup_partition.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(partition, backup_partition)
        try:
            loaded_rows = load_rows(data_file, source_format)
            target_file = partition / f"data.{target_format}"
            rewritten = write_rows(loaded_rows, target_file, target_format, compression)
        except Exception as exc:
            skipped.append({**row, "apply_status": "rewrite_failed", "backup_path": str(backup_partition), "error": str(exc)})
            continue
        compacted.append(
            {
                **row,
                "apply_status": "rewritten",
                "backup_path": str(backup_partition),
                "target_file": str(partition / f"data.{target_format}"),
                "rewritten_rows": rewritten,
            }
        )
    return {
        "compacted": len(compacted),
        "skipped": len(skipped),
        "backup_dir": str(batch_dir),
        "compacted_partitions": compacted,
        "skipped_partitions": skipped,
    }


def summarize(rows: list[dict[str, Any]], apply_result: dict[str, Any] | None = None) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    by_table: dict[str, dict[str, Any]] = {}
    for row in rows:
        status = str(row.get("status") or "unknown")
        table = str(row.get("table") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
        table_summary = by_table.setdefault(
            table,
            {"table": table, "partitions": 0, "compact_candidate": 0, "ok": 0, "missing_data_file": 0, "bytes": 0, "rows": 0},
        )
        table_summary["partitions"] += 1
        table_summary[status] = int(table_summary.get(status) or 0) + 1
        table_summary["bytes"] += int(row.get("size_bytes") or 0)
        table_summary["rows"] += int(row.get("row_count") or 0)
    return {
        "partitions": len(rows),
        "compact_candidates": by_status.get("compact_candidate", 0),
        "by_status": by_status,
        "by_table": sorted(by_table.values(), key=lambda item: str(item.get("table"))),
        "apply_compacted": int((apply_result or {}).get("compacted") or 0),
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
        [row.get("table"), row.get("partitions"), row.get("compact_candidate"), row.get("ok"), row.get("missing_data_file"), row.get("rows"), row.get("bytes")]
        for row in summary.get("by_table", [])
    ]
    candidate_rows = [
        [
            row.get("table"),
            row.get("date"),
            row.get("source_format"),
            row.get("target_format"),
            row.get("row_count"),
            row.get("size_bytes"),
            ",".join(row.get("reasons") or []),
        ]
        for row in (payload.get("partitions") or [])
        if isinstance(row, dict) and row.get("status") == "compact_candidate"
    ][:20]
    compacted_rows = [
        [row.get("table"), row.get("date"), row.get("rewritten_rows"), row.get("backup_path")]
        for row in apply_result.get("compacted_partitions", [])
        if isinstance(row, dict)
    ][:20]
    return "\n\n".join(
        [
            "# Research Store Compaction",
            f"- Generated: `{payload.get('generated_at')}`",
            f"- Mode: `{'apply' if payload.get('apply_enabled') else 'plan_only'}`",
            f"- Store: `{payload.get('store_dir')}`",
            f"- Backup dir: `{payload.get('backup_dir')}`",
            f"- Thresholds: min bytes `{payload.get('min_bytes')}`, min rows `{payload.get('min_rows')}`, max row groups `{payload.get('max_row_groups')}`",
            f"- Partitions: `{summary.get('partitions', 0)}`; candidates `{summary.get('compact_candidates', 0)}`; compacted `{summary.get('apply_compacted', 0)}`; skipped `{summary.get('apply_skipped', 0)}`",
            "- Note: default mode only plans. Apply mode backs up partition data before rewriting; it does not delete data or call Binance.",
            "## Tables",
            md_table(["table", "partitions", "candidates", "ok", "missing", "rows", "bytes"], table_rows),
            "## Compaction Candidates",
            md_table(["table", "date", "source", "target", "rows", "bytes", "reasons"], candidate_rows),
            "## Applied Rewrites",
            md_table(["table", "date", "rewritten_rows", "backup_path"], compacted_rows),
        ]
    )


def write_outputs(runtime_dir: Path, reports_dir: Path, payload: dict[str, Any]) -> None:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "research_store_compaction_latest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    (reports_dir / "research_store_compaction_latest.md").write_text(render_md(payload), encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan/apply research_store partition compaction")
    parser.add_argument("--store", default=str(ROOT / "research_store"))
    parser.add_argument("--backup-dir", default=str(ROOT / "research_store_compaction_backup"))
    parser.add_argument("--runtime-dir", default=str(ROOT / "runtime"))
    parser.add_argument("--reports-dir", default=str(ROOT / "reports"))
    parser.add_argument("--tables", default=",".join(DEFAULT_TABLES))
    parser.add_argument("--format", choices=["parquet", "jsonl"], default="parquet")
    parser.add_argument("--target-format", choices=["same", "parquet", "jsonl"], default="same")
    parser.add_argument("--compression", default="snappy")
    parser.add_argument("--min-bytes", type=int, default=64 * 1024 * 1024)
    parser.add_argument("--min-rows", type=int, default=500_000)
    parser.add_argument("--max-row-groups", type=int, default=32)
    parser.add_argument("--apply", action="store_true", help="Back up and rewrite compaction candidates")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    generated_at = now_cst()
    store = Path(args.store)
    backup_dir = Path(args.backup_dir)
    rows = scan_store(
        store,
        fmt=args.format,
        target_format=args.target_format,
        tables=parse_tables(args.tables),
        min_bytes=args.min_bytes,
        min_rows=args.min_rows,
        max_row_groups=args.max_row_groups,
    )
    apply_result: dict[str, Any] = {}
    if args.apply:
        apply_result = compact_candidates(rows, store=store, backup_dir=backup_dir, compression=args.compression, generated_at=generated_at)
    payload = {
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "store_dir": str(store),
        "backup_dir": str(backup_dir),
        "format": args.format,
        "target_format": args.target_format,
        "tables": parse_tables(args.tables),
        "compression": args.compression,
        "min_bytes": int(args.min_bytes),
        "min_rows": int(args.min_rows),
        "max_row_groups": int(args.max_row_groups),
        "apply_enabled": bool(args.apply),
        "summary": summarize(rows, apply_result),
        "partitions": rows,
        "apply": apply_result,
        "live_impact": "none; plan-only by default; apply backs up and rewrites local research_store partitions only",
    }
    write_outputs(Path(args.runtime_dir), Path(args.reports_dir), payload)
    print(
        json.dumps(
            {
                "partitions": payload["summary"]["partitions"],
                "compact_candidates": payload["summary"]["compact_candidates"],
                "compacted": payload["summary"]["apply_compacted"],
                "apply": bool(args.apply),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
