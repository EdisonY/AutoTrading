from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


DEFAULT_CACHE = Path("runtime/market_data_cache.json")


def load_market_cache(path: Path | None = None, *, max_age_seconds: int = 45) -> dict[str, Any]:
    cache_path = path or DEFAULT_CACHE
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}
    ts = float(payload.get("unix_ts") or 0)
    if ts <= 0 or time.time() - ts > max_age_seconds:
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
