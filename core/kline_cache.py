"""Small filesystem cache for repeated kline pulls across scanner passes."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def _safe_name(symbol: str, bar: str, limit: int) -> str:
    return f"{symbol.upper()}_{bar}_{int(limit)}.json".replace("/", "_")


def kline_cache_max_age_sec(default: int = 90) -> int:
    try:
        return max(0, int(float(os.environ.get("SCANNER_KLINE_CACHE_MAX_AGE_SEC", str(default)))))
    except Exception:
        return int(default)


def kline_network_enabled() -> bool:
    raw = os.environ.get("SCANNER_KLINE_NETWORK_ENABLED", "0").strip().lower()
    override = os.environ.get("SCANNER_DIRECT_KLINE_NETWORK_ALLOWED", "0").strip().lower()
    return raw in {"1", "true", "yes", "on"} and override in {"1", "true", "yes", "on"}


def kline_base_url() -> str:
    return os.environ.get("SCANNER_KLINE_BASE_URL", "https://fapi.binance.com").strip().rstrip("/")


def kline_request_url(symbol: str, bar: str, limit: int) -> str:
    return f"{kline_base_url()}/fapi/v1/klines?symbol={symbol}&interval={bar}&limit={int(limit)}"


def load_cached_klines(root: Path, symbol: str, bar: str, limit: int, *, max_age_sec: int | None = None) -> list[list[Any]] | None:
    path = root / "runtime" / "kline_cache" / _safe_name(symbol, bar, limit)
    try:
        max_age = kline_cache_max_age_sec() if max_age_sec is None else int(max_age_sec)
        if time.time() - path.stat().st_mtime > max_age:
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
