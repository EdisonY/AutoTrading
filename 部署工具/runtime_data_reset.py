"""Archive and reset testnet runtime signal data.

This is for construction-mode resets only. It preserves durable memory,
attention acknowledgement tables, approvals, and source code. By default it
only previews. Use --apply after Binance-facing services are stopped.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name == "部署工具" else SCRIPT_DIR
CST = timezone(timedelta(hours=8))

RESET_TABLES = ("events", "sentinel_scans", "account_snapshots")

RUNTIME_FILES = [
    "runtime/account_snapshot_latest.json",
    "runtime/account_snapshot_error_latest.json",
    "runtime/alerts_latest.json",
    "runtime/a_v11_rollout_review_latest.json",
    "runtime/replay_gate_audit_latest.json",
    "runtime/replay_feature_dataset_latest.json",
    "runtime/rollback_watch_review_latest.json",
    "runtime/sentinel_quality_latest.json",
    "runtime/strategy_evolution_latest.json",
    "runtime/strategy_truth_latest.json",
    "runtime/research_store_summary_latest.json",
    "runtime/market_mover_watchlist.json",
    "runtime/market_data_cache.json",
    "runtime/paper_exchange_state.json",
    "runtime/paper_exchange_latest.json",
    "runtime/binance_api_queue_summary_latest.json",
    "runtime/waiting_period_optimization_latest.json",
    "runtime/long_term_skeleton_latest.json",
]

LOG_AND_SCANNER_FILES = [
    "logs/alerts.jsonl",
    "logs/account_snapshot_errors.jsonl",
    "logs/account_snapshots.jsonl",
    "logs/market_mover_sentinel.jsonl",
    "logs/scanner_stdout.log",
    "logs/scanner_stderr.log",
    "logs/decisions.jsonl",
    "logs/signals.jsonl",
    "logs/operations.jsonl",
    "logs/system.jsonl",
    "logs_v14/stdout.log",
    "logs_v14/stderr.log",
    "logs_v14/scanner_stdout.log",
    "logs_v14/decisions.jsonl",
    "logs_v14/signals.jsonl",
    "logs_v14/operations.jsonl",
    "logs_v14/system.jsonl",
    "logs_v16/stdout.log",
    "logs_v16/stderr.log",
    "logs_v16/decisions.jsonl",
    "logs_v16/signals.jsonl",
    "logs_v16/operations.jsonl",
    "logs_v16/system.jsonl",
    "scanner_data/events.jsonl",
    "scanner_data/trades.jsonl",
    "scanner_data/report.md",
    "scanner_data_v14/events.jsonl",
    "scanner_data_v14/trades.jsonl",
    "scanner_data_v14/report.md",
    "scanner_data_v16/events.jsonl",
    "scanner_data_v16/trades.jsonl",
    "scanner_data_v16/positions_v16.json",
    "scanner_data_v16/report.md",
    "scanner_data_v12/events.jsonl",
    "scanner_data_v12/trades.jsonl",
    "scanner_data_v12/report.md",
]


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT)).replace("\\", "/")
    except Exception:
        return str(path)


def table_counts(db_path: Path) -> dict[str, int | str]:
    counts: dict[str, int | str] = {}
    if not db_path.exists():
        return counts
    con = sqlite3.connect(str(db_path))
    try:
        for table in RESET_TABLES + ("attention_items", "attention_acknowledgements", "baseline_runs"):
            try:
                counts[table] = int(con.execute(f"select count(*) from {table}").fetchone()[0])
            except Exception as exc:
                counts[table] = str(exc)
    finally:
        con.close()
    return counts


def copy_if_exists(path: Path, archive_root: Path) -> bool:
    if not path.exists():
        return False
    dest = archive_root / rel(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if path.is_dir():
        shutil.copytree(path, dest, dirs_exist_ok=True)
    else:
        shutil.copy2(path, dest)
    return True


def reset_db(db_path: Path, archive_root: Path, apply: bool) -> dict[str, Any]:
    before = table_counts(db_path)
    result: dict[str, Any] = {
        "db": str(db_path),
        "exists": db_path.exists(),
        "bytes_before": db_path.stat().st_size if db_path.exists() else 0,
        "counts_before": before,
        "counts_after": {},
        "backup": "",
    }
    if not db_path.exists():
        return result
    backup_path = archive_root / rel(db_path)
    result["backup"] = str(backup_path)
    if not apply:
        return result
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(db_path, backup_path)
    con = sqlite3.connect(str(db_path), timeout=60)
    try:
        con.execute("pragma wal_checkpoint(truncate)")
        for table in RESET_TABLES:
            con.execute(f"delete from {table}")
            con.execute("delete from sqlite_sequence where name=?", (table,))
        con.commit()
        con.execute("vacuum")
        con.commit()
    finally:
        con.close()
    result["bytes_after"] = db_path.stat().st_size
    result["counts_after"] = table_counts(db_path)
    return result


def archive_and_clear_files(paths: list[str], archive_root: Path, apply: bool, *, remove: bool) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in paths:
        path = ROOT / raw
        row: dict[str, Any] = {"path": raw, "exists": path.exists(), "action": "none"}
        if not path.exists():
            rows.append(row)
            continue
        row["bytes_before"] = path.stat().st_size if path.is_file() else None
        row["backup"] = str(archive_root / raw)
        if apply:
            copy_if_exists(path, archive_root)
            if remove:
                path.unlink()
                row["action"] = "archived_removed"
            else:
                path.write_text("", encoding="utf-8")
                row["action"] = "archived_truncated"
        else:
            row["action"] = "would_archive_remove" if remove else "would_archive_truncate"
        rows.append(row)
    return rows


def write_receipt(payload: dict[str, Any], apply: bool) -> Path:
    path = ROOT / "runtime" / "testnet_data_reset_latest.json"
    if apply:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_construction_marker(payload: dict[str, Any], apply: bool) -> Path:
    path = ROOT / "runtime" / "construction_mode.json"
    if apply:
        path.parent.mkdir(parents=True, exist_ok=True)
        marker = {
            "enabled": True,
            "reason": "testnet_runtime_reset_and_long_term_staged_validation",
            "updated_at": payload.get("generated_at"),
            "reset_archive_root": payload.get("archive_root"),
            "resume_gate": "all_long_term_skeleton_and_staged_validation_items_pass",
        }
        path.write_text(json.dumps(marker, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Archive and reset testnet runtime data")
    parser.add_argument("--db", default=str(ROOT / "runtime" / "event_store.sqlite3"))
    parser.add_argument("--archive-root", default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--include-generated-runtime", action="store_true")
    args = parser.parse_args(argv)

    stamp = datetime.now(CST).strftime("%Y%m%d-%H%M%S")
    archive_root = Path(args.archive_root) if args.archive_root else ROOT / "archive" / "testnet_data_reset" / stamp
    db_path = Path(args.db)
    selected_runtime = RUNTIME_FILES if args.include_generated_runtime else []
    payload: dict[str, Any] = {
        "generated_at": datetime.now(CST).isoformat(),
        "apply": bool(args.apply),
        "archive_root": str(archive_root),
        "preserved": ["attention_items", "attention_acknowledgements", "meta", "baseline_runs"],
        "db_reset": reset_db(db_path, archive_root, args.apply),
        "log_files": archive_and_clear_files(LOG_AND_SCANNER_FILES, archive_root, args.apply, remove=False),
        "runtime_files": archive_and_clear_files(selected_runtime, archive_root, args.apply, remove=True),
    }
    receipt = write_receipt(payload, args.apply)
    construction_marker = write_construction_marker(payload, args.apply)
    payload["receipt"] = str(receipt)
    payload["construction_marker"] = str(construction_marker)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
