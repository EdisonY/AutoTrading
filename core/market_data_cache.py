from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


DEFAULT_CACHE = Path("runtime/market_data_cache.json")


def market_cache_max_age_seconds(default: int = 45) -> int:
    try:
        return max(0, int(float(os.environ.get("SCANNER_MARKET_CACHE_MAX_AGE_SEC", str(default)))))
    except Exception:
        return int(default)


def market_data_network_enabled() -> bool:
    raw = os.environ.get("SCANNER_MARKET_DATA_NETWORK_ENABLED")
    if raw is None:
        raw = os.environ.get("SCANNER_KLINE_NETWORK_ENABLED", "1")
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def load_market_cache(path: Path | None = None, *, max_age_seconds: int = 45) -> dict[str, Any]:
    cache_path = path or DEFAULT_CACHE
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}
    ts = float(payload.get("unix_ts") or 0)
    max_age = market_cache_max_age_seconds(max_age_seconds)
    if ts <= 0 or time.time() - ts > max_age:
        return {}
    return payload if isinstance(payload, dict) else {}


def cached_top_symbols(path: Path | None, limit: int, *, max_age_seconds: int = 45) -> list[str]:
    payload = load_market_cache(path, max_age_seconds=max_age_seconds)
    symbols = payload.get("top_symbols") or []
    return [str(s).upper() for s in symbols[:limit]]


def cached_available_symbols(path: Path | None, *, max_age_seconds: int = 45) -> set[str]:
    payload = load_market_cache(path, max_age_seconds=max_age_seconds)
    symbols = payload.get("available_symbols") or []
    return {str(s).upper() for s in symbols}


def cached_spike_symbols(path: Path | None, limit: int, *, max_age_seconds: int = 45) -> list[str]:
    payload = load_market_cache(path, max_age_seconds=max_age_seconds)
    symbols = payload.get("spike_symbols") or []
    return [str(s).upper() for s in symbols[:limit]]
