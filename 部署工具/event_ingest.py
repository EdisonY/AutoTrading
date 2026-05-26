from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] if Path(__file__).resolve().parent.name == "部署工具" else Path.cwd()
sys.path.insert(0, str(ROOT))

from core.event_store import init_db, insert_events


SOURCES = [
    ("A/v11/events", "scanner_data/events.jsonl"),
    ("A/v11/decisions", "logs/decisions.jsonl"),
    ("A/v11/signals", "logs/signals.jsonl"),
    ("B/v16/events", "scanner_data_v16/events.jsonl"),
    ("B/v16/decisions", "logs_v16/decisions.jsonl"),
    ("B/v16/signals", "logs_v16/signals.jsonl"),
    ("C/v14/events", "scanner_data_v14/events.jsonl"),
    ("C/v14/decisions", "logs_v14/decisions.jsonl"),
    ("C/v14/signals", "logs_v14/signals.jsonl"),
]


def iter_tail_lines(path: Path, max_lines: int) -> list[str]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    return lines[-max_lines:] if max_lines > 0 else lines


def ingest_file(db: Path, root: Path, label: str, rel: str, max_lines: int) -> int:
    path = root / rel
    rows = []
    for line in iter_tail_lines(path, max_lines):
        try:
            item = json.loads(line)
        except Exception:
            continue
        if "strategy" not in item:
            item["strategy"] = label.split("/")[0] + "/" + label.split("/")[1]
        rows.append(item)
    return insert_events(db, rows, source=label)


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest recent JSONL audit events into SQLite event store.")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--db", type=Path, default=ROOT / "runtime" / "event_store.sqlite3")
    parser.add_argument("--max-lines-per-file", type=int, default=5000)
    args = parser.parse_args()
    init_db(args.db)
    total = 0
    details = {}
    for label, rel in SOURCES:
        count = ingest_file(args.db, args.root, label, rel, args.max_lines_per_file)
        details[label] = count
        total += count
    print(json.dumps({"db": str(args.db), "total": total, "details": details}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
