"""Cleanup event_store.sqlite3 — remove events older than N days.

SAFE BY DEFAULT:
- Only deletes SENTINEL_SCANNED, EVENT, SIGNAL (high-frequency low-value)
- NEVER deletes OPEN, CLOSE, FORCED_CLOSE, OPEN_FAILED, OPEN_SKIPPED
- Runs VACUUM after cleanup to reclaim disk space
- Creates a backup before any deletion

Usage:
  python cleanup_event_store.py --days 30 --dry-run   # preview only
  python cleanup_event_store.py --days 30 --apply       # actually delete
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

CST = timezone(timedelta(hours=8))

# Event types that are SAFE to delete (high-frequency, low-value)
DELETABLE_TYPES = {"SENTINEL_SCANNED", "EVENT", "SIGNAL"}

# Event types that should NEVER be deleted (core trading data)
PROTECTED_TYPES = {"OPEN", "CLOSE", "FORCED_CLOSE", "OPEN_FAILED", "OPEN_SKIPPED", "SENTINEL_SIGNAL", "SMOKE_TEST"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cleanup event_store.sqlite3")
    parser.add_argument("--db", default=None, help="Path to event_store.sqlite3")
    parser.add_argument("--days", type=int, default=30, help="Delete events older than N days")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no deletion")
    parser.add_argument("--apply", action="store_true", help="Actually delete and vacuum")
    args = parser.parse_args(argv)

    script_dir = Path(__file__).resolve().parent
    root = script_dir.parent if script_dir.name == "部署工具" else script_dir
    db_path = Path(args.db) if args.db else root / "runtime" / "event_store.sqlite3"

    if not db_path.exists():
        print(f"ERROR: Database not found: {db_path}")
        return 1

    cutoff = (datetime.now(CST) - timedelta(days=args.days)).isoformat(timespec="seconds")
    print(f"Database: {db_path}")
    print(f"Size before: {os.path.getsize(db_path) / 1024 / 1024:.1f} MB")
    print(f"Cutoff: {cutoff} (events older than {args.days} days)")
    print()

    con = sqlite3.connect(str(db_path))
    try:
        # Count what would be deleted
        for etype in sorted(DELETABLE_TYPES):
            count = con.execute(
                "SELECT count(*) FROM events WHERE event_type = ? AND ts < ?",
                (etype, cutoff)
            ).fetchone()[0]
            if count > 0:
                print(f"  {etype}: {count:,} rows to delete")

        # Count protected (will NOT be deleted)
        print()
        print("Protected (will NOT be deleted):")
        for etype in sorted(PROTECTED_TYPES):
            count = con.execute(
                "SELECT count(*) FROM events WHERE event_type = ?",
                (etype,)
            ).fetchone()[0]
            if count > 0:
                print(f"  {etype}: {count:,} rows (all kept)")

        total_deletable = con.execute(
            "SELECT count(*) FROM events WHERE event_type IN ({}) AND ts < ?".format(
                ",".join("?" for _ in DELETABLE_TYPES)
            ),
            list(DELETABLE_TYPES) + [cutoff]
        ).fetchone()[0]

        print()
        print(f"Total rows to delete: {total_deletable:,}")

        if args.dry_run:
            print()
            print("[DRY RUN] No changes made. Use --apply to execute.")
            return 0

        if not args.apply:
            print()
            print("Use --dry-run to preview or --apply to execute.")
            return 0

        # Create backup
        backup_path = db_path.with_suffix(f".backup_{datetime.now(CST).strftime('%Y%m%d_%H%M%S')}.sqlite3")
        print(f"\nCreating backup: {backup_path}")
        shutil.copy2(db_path, backup_path)
        print(f"  Backup size: {os.path.getsize(backup_path) / 1024 / 1024:.1f} MB")

        # Delete
        print(f"\nDeleting {total_deletable:,} rows...")
        con.execute(
            "DELETE FROM events WHERE event_type IN ({}) AND ts < ?".format(
                ",".join("?" for _ in DELETABLE_TYPES)
            ),
            list(DELETABLE_TYPES) + [cutoff]
        )
        con.commit()

        # VACUUM
        print("Running VACUUM (this may take a while)...")
        con.execute("VACUUM")
        con.commit()

        size_after = os.path.getsize(db_path) / 1024 / 1024
        print(f"\nSize after: {size_after:.1f} MB")
        print(f"Freed: {os.path.getsize(backup_path) / 1024 / 1024 - size_after:.1f} MB")
        print(f"Backup kept at: {backup_path}")
        print("\nDone!")

    finally:
        con.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
