from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any


def _date_from_item(item: dict[str, Any]) -> str:
    raw = str(item.get("time") or item.get("ts") or item.get("timestamp") or item.get("entry_time") or item.get("exit_time") or "")
    if len(raw) >= 10 and raw[4:5] == "-" and raw[7:8] == "-":
        return raw[:10]
    return datetime.now().strftime("%Y-%m-%d")


def write_jsonl_with_daily_shard(path: Path, item: dict[str, Any]) -> None:
    line = json.dumps(item, ensure_ascii=False, default=str) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    legacy_enabled = os.environ.get("LEGACY_JSONL_ENABLED", "1").strip().lower() not in {"0", "false", "no"}
    if legacy_enabled or path.name == "trades.jsonl":
        with path.open("a", encoding="utf-8") as f:
            f.write(line)

    shard_dir = path.parent / path.stem
    shard_dir.mkdir(parents=True, exist_ok=True)
    shard_path = shard_dir / f"{_date_from_item(item)}.jsonl"
    with shard_path.open("a", encoding="utf-8") as f:
        f.write(line)
