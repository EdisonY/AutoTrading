"""Maintain event retention, partition legacy JSONL files, and prune report noise."""

from __future__ import annotations

import argparse
import gzip
import json
import shutil
import sqlite3
import tempfile
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name == "部署工具" else SCRIPT_DIR
DB_PATH = ROOT / "runtime" / "event_store.sqlite3"
CST = timezone(timedelta(hours=8))

LEGACY_JSONL = [
    ROOT / "logs" / "decisions.jsonl",
    ROOT / "logs" / "signals.jsonl",
    ROOT / "logs" / "operations.jsonl",
    ROOT / "logs" / "system.jsonl",
    ROOT / "logs" / "account_snapshots.jsonl",
    ROOT / "logs_v14" / "decisions.jsonl",
    ROOT / "logs_v14" / "signals.jsonl",
    ROOT / "logs_v14" / "operations.jsonl",
    ROOT / "logs_v14" / "system.jsonl",
    ROOT / "logs_v16" / "decisions.jsonl",
    ROOT / "logs_v16" / "signals.jsonl",
    ROOT / "logs_v16" / "operations.jsonl",
    ROOT / "logs_v16" / "system.jsonl",
    ROOT / "scanner_data" / "events.jsonl",
    ROOT / "scanner_data_v14" / "events.jsonl",
    ROOT / "scanner_data_v16" / "events.jsonl",
    ROOT / "runtime" / "sentinel_events.jsonl",
]

TRADE_SOURCES = [
    ("A/v11/trades", ROOT / "scanner_data" / "trades.jsonl"),
    ("B/v16/trades", ROOT / "scanner_data_v16" / "trades.jsonl"),
    ("C/v14/trades", ROOT / "scanner_data_v14" / "trades.jsonl"),
]

NOISY_SHARD_DIRS = [
    ROOT / "runtime" / "sentinel_events",
    ROOT / "logs" / "account_snapshots",
]

TEXT_LOGS = [
    ROOT / "logs" / "scanner_stdout.log",
    ROOT / "logs" / "scanner_stderr.log",
    ROOT / "logs_v14" / "stdout.log",
    ROOT / "logs_v14" / "stderr.log",
    ROOT / "logs_v14" / "scanner_stdout.log",
    ROOT / "logs_v16" / "stdout.log",
    ROOT / "logs_v16" / "stderr.log",
]

SERVER_LOG_MIRROR_DIRS = [
    ROOT / "server_logs_tencent",
]

ARCHIVABLE_SHARD_DIRS = [
    ROOT / "logs" / "decisions",
    ROOT / "logs" / "signals",
    ROOT / "logs" / "operations",
    ROOT / "logs" / "system",
    ROOT / "logs_v14" / "decisions",
    ROOT / "logs_v14" / "signals",
    ROOT / "logs_v14" / "operations",
    ROOT / "logs_v14" / "system",
    ROOT / "logs_v16" / "decisions",
    ROOT / "logs_v16" / "signals",
    ROOT / "logs_v16" / "operations",
    ROOT / "logs_v16" / "system",
    ROOT / "scanner_data" / "events",
    ROOT / "scanner_data" / "trades",
    ROOT / "scanner_data_v14" / "events",
    ROOT / "scanner_data_v14" / "trades",
    ROOT / "scanner_data_v16" / "events",
    ROOT / "scanner_data_v16" / "trades",
]


def date_key(row: dict[str, Any]) -> str | None:
    raw = str(row.get("time") or row.get("ts") or row.get("timestamp") or row.get("entry_time") or row.get("exit_time") or "")
    if len(raw) >= 10 and raw[4:5] == "-" and raw[7:8] == "-":
        return raw[:10]
    return None


def partition_file(path: Path, archive: bool) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "rows": 0, "dates": 0, "status": "missing"}
    shard_dir = path.parent / path.stem
    shard_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    handles: dict[str, Any] = {}
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"{path.stem}_", dir=str(path.parent)))
    try:
        with path.open("r", encoding="utf-8", errors="replace") as src:
            for line in src:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                day = date_key(row)
                if not day:
                    continue
                if day not in handles:
                    handles[day] = (tmp_dir / f"{day}.jsonl").open("w", encoding="utf-8")
                handles[day].write(line if line.endswith("\n") else line + "\n")
                count += 1
        for handle in handles.values():
            handle.close()
        for tmp in tmp_dir.glob("*.jsonl"):
            tmp.replace(shard_dir / tmp.name)
        if archive:
            archive_path = ROOT / "archive" / "legacy_jsonl" / path.relative_to(ROOT)
            archive_path = archive_path.with_suffix(path.suffix + ".gz")
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("rb") as src, gzip.open(archive_path, "wb", compresslevel=6) as dst:
                shutil.copyfileobj(src, dst)
            path.unlink()
        return {"path": str(path), "rows": count, "dates": len(handles), "status": "archived" if archive else "partitioned"}
    finally:
        for handle in handles.values():
            if not handle.closed:
                handle.close()
        shutil.rmtree(tmp_dir, ignore_errors=True)


def ingest_trades() -> dict[str, int]:
    inserted: dict[str, int] = {}
    if not DB_PATH.exists():
        return inserted
    con = sqlite3.connect(DB_PATH, timeout=30)
    try:
        for source, path in TRADE_SOURCES:
            if not path.exists():
                continue
            if con.execute("select count(*) from events where source=?", (source,)).fetchone()[0]:
                continue
            rows = []
            with path.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        payload = json.loads(line)
                    except Exception:
                        continue
                    rows.append(
                        (
                            str(payload.get("exit_time") or payload.get("time") or payload.get("entry_time") or datetime.now(CST).isoformat()),
                            source.split("/trades")[0],
                            str(payload.get("symbol") or ""),
                            str(payload.get("event") or "TRADE").upper(),
                            "trade",
                            str(payload.get("side") or ""),
                            source,
                            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
                        )
                    )
            con.executemany(
                "insert into events(ts,strategy,symbol,event_type,category,side,source,payload_json) values(?,?,?,?,?,?,?,?)",
                rows,
            )
            inserted[source] = len(rows)
        con.commit()
    finally:
        con.close()
    return inserted


def prune_db(
    purge_duplicate_sentinel: bool,
    purge_event_store_mirrors: bool,
    vacuum: bool,
    retention: bool,
    event_retention_days: int,
    sentinel_retention_days: int,
) -> dict[str, int]:
    if not DB_PATH.exists():
        return {}
    con = sqlite3.connect(DB_PATH, timeout=60)
    counts: dict[str, int] = {}
    try:
        table_names = {
            row[0]
            for row in con.execute("select name from sqlite_master where type='table'")
        }
        if purge_duplicate_sentinel:
            counts["sentinel_deleted"] = con.execute("delete from events where source='sentinel/events'").rowcount
        if purge_event_store_mirrors:
            counts["decision_mirror_deleted"] = con.execute(
                "delete from events where source in ('A/v11/decisions','B/v16/decisions','C/v14/decisions')"
            ).rowcount
            counts["sentinel_event_mirror_deleted"] = con.execute(
                "delete from events where event_type='SENTINEL_SCANNED'"
            ).rowcount
        if retention:
            cutoff_sentinel = (datetime.now(CST) - timedelta(days=7)).isoformat()
            counts["sentinel_retention_deleted"] = con.execute(
                "delete from events where source='sentinel/events' and ts < ?",
                (cutoff_sentinel,),
            ).rowcount
            cutoff_sentinel_scans = (datetime.now(CST) - timedelta(days=max(1, sentinel_retention_days))).strftime("%Y-%m-%d")
            if "sentinel_scans" in table_names:
                counts["sentinel_scans_retention_deleted"] = con.execute(
                    "delete from sentinel_scans where date < ?",
                    (cutoff_sentinel_scans,),
                ).rowcount
            else:
                counts["sentinel_scans_retention_deleted"] = 0
            cutoff_events = (datetime.now(CST) - timedelta(days=max(1, event_retention_days))).strftime("%Y-%m-%d")
            counts["raw_event_deleted"] = con.execute(
                "delete from events where source not like '%/trades' and category != 'trade' and ts < ?",
                (cutoff_events,),
            ).rowcount
        cutoff_account = (datetime.now(CST) - timedelta(days=30)).isoformat()
        counts["snapshot_deleted"] = con.execute("delete from account_snapshots where ts < ?", (cutoff_account,)).rowcount
        con.commit()
        if vacuum:
            con.execute("vacuum")
    finally:
        con.close()
    return counts


def prune_snapshot_html() -> int:
    report_dir = ROOT / "复盘报告"
    removed = 0
    for path in report_dir.glob("account_snapshot_*.html"):
        if path.name == "account_snapshot_latest.html":
            continue
        path.unlink()
        removed += 1
    return removed


def gzip_archive(path: Path, group: str) -> Path:
    archive_path = ROOT / "archive" / group / path.relative_to(ROOT)
    archive_path = archive_path.with_suffix(path.suffix + ".gz")
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    if archive_path.exists():
        archive_path = archive_path.with_name(f"{archive_path.stem}_{int(datetime.now(CST).timestamp())}{archive_path.suffix}")
    with path.open("rb") as src, gzip.open(archive_path, "wb", compresslevel=6) as dst:
        shutil.copyfileobj(src, dst)
    return archive_path


def archive_noisy_shards(include_today: bool = False) -> dict[str, int]:
    counts: dict[str, int] = {}
    today = datetime.now(CST).strftime("%Y-%m-%d")
    for shard_dir in NOISY_SHARD_DIRS:
        removed = 0
        if shard_dir.exists():
            for path in shard_dir.glob("*.jsonl"):
                if not include_today and path.stem == today:
                    continue
                if path.is_file() and path.stat().st_size > 0:
                    gzip_archive(path, "noisy_shards")
                    path.unlink()
                    removed += 1
        counts[str(shard_dir.relative_to(ROOT))] = removed
    return counts


def parse_shard_day(path: Path) -> datetime | None:
    try:
        return datetime.strptime(path.name[:10], "%Y-%m-%d").replace(tzinfo=CST)
    except ValueError:
        return None


def archive_old_shards(hot_days: int) -> dict[str, int]:
    cutoff = datetime.now(CST) - timedelta(days=max(1, hot_days))
    counts: dict[str, int] = {}
    for shard_dir in ARCHIVABLE_SHARD_DIRS:
        archived = 0
        if shard_dir.exists():
            for path in shard_dir.glob("*.jsonl"):
                day = parse_shard_day(path)
                if day and day < cutoff and path.stat().st_size > 0:
                    gzip_archive(path, "daily_shards")
                    path.unlink()
                    archived += 1
        counts[str(shard_dir.relative_to(ROOT))] = archived
    return counts


def prune_archives(archive_days: int) -> int:
    cutoff = datetime.now(CST).timestamp() - max(1, archive_days) * 86400
    removed = 0
    archive_root = ROOT / "archive"
    if not archive_root.exists():
        return 0
    for path in archive_root.rglob("*.gz"):
        if path.stat().st_mtime < cutoff:
            path.unlink()
            removed += 1
    return removed


def trim_text_logs(lines: int = 5000, min_bytes: int = 5_000_000) -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in TEXT_LOGS:
        if not path.exists() or path.stat().st_size < min_bytes:
            counts[str(path.relative_to(ROOT))] = 0
            continue
        gzip_archive(path, "trimmed_text_logs")
        with path.open("rb") as f:
            data = deque(f, maxlen=lines)
        path.write_bytes(b"".join(data))
        counts[str(path.relative_to(ROOT))] = len(data)
    return counts


def prune_release_dirs(keep_releases: int) -> dict[str, Any]:
    releases = ROOT / "releases"
    if not releases.exists():
        return {"removed": 0, "bytes_removed": 0, "kept": 0, "status": "missing"}
    dirs = [path for path in releases.iterdir() if path.is_dir()]
    dirs.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    keep = max(0, int(keep_releases))
    removed = 0
    bytes_removed = 0
    for path in dirs[keep:]:
        try:
            size = sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
            shutil.rmtree(path)
            removed += 1
            bytes_removed += size
        except Exception:
            continue
    return {"removed": removed, "bytes_removed": bytes_removed, "kept": min(len(dirs), keep), "status": "ok"}


def prune_server_log_mirror(retention_days: int) -> dict[str, Any]:
    cutoff = datetime.now(CST).timestamp() - max(1, int(retention_days)) * 86400
    result: dict[str, Any] = {}
    for root in SERVER_LOG_MIRROR_DIRS:
        removed = 0
        bytes_removed = 0
        if not root.exists():
            result[str(root.relative_to(ROOT)) if root.is_relative_to(ROOT) else str(root)] = {"status": "missing", "removed": 0, "bytes_removed": 0}
            continue
        for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if path.is_file() and path.stat().st_mtime < cutoff:
                size = path.stat().st_size
                path.unlink()
                removed += 1
                bytes_removed += size
            elif path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    pass
        label = str(root.relative_to(ROOT)) if root.is_relative_to(ROOT) else str(root)
        result[label] = {"status": "ok", "removed": removed, "bytes_removed": bytes_removed}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="AutoTrading data maintenance")
    parser.add_argument("--partition-legacy", action="store_true")
    parser.add_argument("--archive-legacy", action="store_true")
    parser.add_argument("--ingest-trades", action="store_true")
    parser.add_argument("--purge-duplicate-sentinel", action="store_true")
    parser.add_argument("--purge-event-store-mirrors", action="store_true")
    parser.add_argument("--retention", action="store_true")
    parser.add_argument("--event-retention-days", type=int, default=14)
    parser.add_argument("--sentinel-retention-days", type=int, default=14)
    parser.add_argument("--vacuum", action="store_true")
    parser.add_argument("--prune-snapshot-html", action="store_true")
    parser.add_argument("--archive-noisy-shards", action="store_true")
    parser.add_argument("--include-today-noisy-shards", action="store_true")
    parser.add_argument("--archive-old-shards", action="store_true")
    parser.add_argument("--hot-shard-days", type=int, default=7)
    parser.add_argument("--prune-archives", action="store_true")
    parser.add_argument("--archive-days", type=int, default=90)
    parser.add_argument("--trim-text-logs", action="store_true")
    parser.add_argument("--text-log-lines", type=int, default=5000)
    parser.add_argument("--prune-releases", action="store_true")
    parser.add_argument("--keep-releases", type=int, default=60)
    parser.add_argument("--prune-server-log-mirror", action="store_true")
    parser.add_argument("--server-log-mirror-days", type=int, default=7)
    args = parser.parse_args()
    output: dict[str, Any] = {}
    if args.ingest_trades:
        output["ingested_trades"] = ingest_trades()
    if args.partition_legacy or args.archive_legacy:
        output["legacy"] = [partition_file(path, args.archive_legacy) for path in LEGACY_JSONL]
    if args.purge_duplicate_sentinel or args.purge_event_store_mirrors or args.retention or args.vacuum:
        output["db"] = prune_db(
            args.purge_duplicate_sentinel,
            args.purge_event_store_mirrors,
            args.vacuum,
            args.retention,
            args.event_retention_days,
            args.sentinel_retention_days,
        )
    if args.prune_snapshot_html:
        output["snapshot_html_removed"] = prune_snapshot_html()
    if args.archive_noisy_shards:
        output["noisy_shards"] = archive_noisy_shards(args.include_today_noisy_shards)
    if args.archive_old_shards:
        output["daily_shards"] = archive_old_shards(args.hot_shard_days)
    if args.prune_archives:
        output["archives_removed"] = prune_archives(args.archive_days)
    if args.trim_text_logs:
        output["trimmed_text_logs"] = trim_text_logs(args.text_log_lines)
    if args.prune_releases:
        output["releases"] = prune_release_dirs(args.keep_releases)
    if args.prune_server_log_mirror:
        output["server_log_mirror"] = prune_server_log_mirror(args.server_log_mirror_days)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
