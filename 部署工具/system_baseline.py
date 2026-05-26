from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1] if Path(__file__).resolve().parent.name == "部署工具" else Path.cwd()
sys.path.insert(0, str(ROOT))

from core.event_store import init_db, insert_baseline


EVENT_SPECS = [
    ("A/v11", "scanner_data/events.jsonl", "logs/decisions.jsonl", "logs/signals.jsonl", "scanner_data/trades.jsonl"),
    ("B/v16", "scanner_data_v16/events.jsonl", "logs_v16/decisions.jsonl", "logs_v16/signals.jsonl", "scanner_data_v16/trades.jsonl"),
    ("C/v14", "scanner_data_v14/events.jsonl", "logs_v14/decisions.jsonl", "logs_v14/signals.jsonl", "scanner_data_v14/trades.jsonl"),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def file_info(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "path": str(path)}
    stat = path.stat()
    return {
        "exists": True,
        "path": str(path),
        "bytes": stat.st_size,
        "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
    }


def tail_jsonl(path: Path, limit: int = 5000) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("rb") as f:
        try:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - limit))
            data = f.read().decode("utf-8", errors="replace")
        except OSError:
            return []
    lines = data.splitlines()
    if lines and not lines[0].startswith("{"):
        lines = lines[1:]
    rows = []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows


def summarize_jsonl(path: Path) -> dict[str, Any]:
    info = file_info(path)
    rows = tail_jsonl(path, limit=200_000)
    counts: dict[str, int] = {}
    last_ts = ""
    for row in rows:
        key = str(row.get("event") or row.get("status") or row.get("category") or "unknown")
        counts[key] = counts.get(key, 0) + 1
        ts = str(row.get("time") or row.get("ts") or row.get("timestamp") or "")
        if ts > last_ts:
            last_ts = ts
    info.update(
        {
            "tail_rows": len(rows),
            "tail_counts": sorted(counts.items(), key=lambda item: item[1], reverse=True)[:12],
            "last_ts": last_ts,
        }
    )
    return info


def scan_codebase(root: Path) -> dict[str, Any]:
    py_files = list(root.rglob("*.py"))
    pycache = list(root.rglob("__pycache__"))
    pyc_files = list(root.rglob("*.pyc"))
    large_files = sorted(
        [p for p in root.rglob("*") if p.is_file()],
        key=lambda p: p.stat().st_size,
        reverse=True,
    )[:25]
    return {
        "python_files": len(py_files),
        "pycache_dirs": len(pycache),
        "pyc_files": len(pyc_files),
        "large_files": [{"path": str(p), "bytes": p.stat().st_size} for p in large_files],
    }


def collect(root: Path) -> dict[str, Any]:
    started = time.perf_counter()
    strategies = {}
    for name, events, decisions, signals, trades in EVENT_SPECS:
        strategies[name] = {
            "events": summarize_jsonl(root / events),
            "decisions": summarize_jsonl(root / decisions),
            "signals": summarize_jsonl(root / signals),
            "trades": summarize_jsonl(root / trades),
        }
    return {
        "generated_at": now_iso(),
        "root": str(root),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "strategies": strategies,
        "codebase": scan_codebase(root),
    }


def write_report(payload: dict[str, Any], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    (out_dir / f"system_baseline_{stamp}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "system_baseline_latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# System Baseline", "", f"- generated_at: `{payload['generated_at']}`", f"- root: `{payload['root']}`", ""]
    for name, data in payload["strategies"].items():
        lines.append(f"## {name}")
        for label in ("events", "decisions", "signals", "trades"):
            item = data[label]
            lines.append(
                f"- {label}: exists={item['exists']} bytes={item.get('bytes', 0)} "
                f"tail_rows={item.get('tail_rows', 0)} last_ts=`{item.get('last_ts', '')}`"
            )
        lines.append("")
    lines += [
        "## Codebase",
        f"- python_files: {payload['codebase']['python_files']}",
        f"- pycache_dirs: {payload['codebase']['pycache_dirs']}",
        f"- pyc_files: {payload['codebase']['pyc_files']}",
    ]
    (out_dir / "system_baseline_latest.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect system baseline for migration.")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--out-dir", type=Path, default=ROOT / "reports")
    parser.add_argument("--db", type=Path, default=ROOT / "runtime" / "event_store.sqlite3")
    args = parser.parse_args()
    payload = collect(args.root)
    init_db(args.db)
    host = os.environ.get("COMPUTERNAME") or (os.uname().nodename if hasattr(os, "uname") else "")
    insert_baseline(args.db, payload, host=host)
    write_report(payload, args.out_dir)
    print(json.dumps({"generated_at": payload["generated_at"], "db": str(args.db), "out_dir": str(args.out_dir)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
