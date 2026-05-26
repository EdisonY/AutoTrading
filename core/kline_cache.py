"""Small filesystem cache for repeated kline pulls across scanner passes."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def _safe_name(symbol: str, bar: str, limit: int) -> str:
    return f"{symbol.upper()}_{bar}_{int(limit)}.json".replace("/", "_")


def load_cached_klines(root: Path, symbol: str, bar: str, limit: int, *, max_age_sec: int = 90) -> list[list[Any]] | None:
    path = root / "runtime" / "kline_cache" / _safe_name(symbol, bar, limit)
    try:
        if time.time() - path.stat().st_mtime > max_age_sec:
            return None
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        rows = payload.get("rows")
        return rows if isinstance(rows, list) else None
    except Exception:
        return None


def save_cached_klines(root: Path, symbol: str, bar: str, limit: int, rows: list[list[Any]]) -> None:
    path = root / "runtime" / "kline_cache" / _safe_name(symbol, bar, limit)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps({"ts": time.time(), "rows": rows}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass
