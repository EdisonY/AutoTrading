"""Small filesystem cache for repeated kline pulls across scanner passes."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def _safe_name(symbol: str, bar: str, limit: int) -> str:
    return f"{symbol.upper()}_{bar}_{int(limit)}.json".replace("/", "_")


def _read_rows(path: Path, max_age: int) -> list[list[Any]] | None:
    if time.time() - path.stat().st_mtime > max(0, max_age):
        return None
    payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    rows = payload.get("rows")
    return rows if isinstance(rows, list) else None


def _larger_cache_paths(root: Path, symbol: str, bar: str, limit: int) -> list[tuple[int, Path]]:
    cache_dir = root / "runtime" / "kline_cache"
    prefix = f"{symbol.upper()}_{bar}_".replace("/", "_")
    out: list[tuple[int, Path]] = []
    try:
        paths = list(cache_dir.glob(f"{prefix}*.json"))
    except Exception:
        return out
    for path in paths:
        name = path.name
        if not name.startswith(prefix) or not name.endswith(".json"):
            continue
        try:
            candidate_limit = int(name[len(prefix):-5])
        except Exception:
            continue
        if candidate_limit >= int(limit):
            out.append((candidate_limit, path))
    out.sort(key=lambda item: item[0])
    return out


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
        rows = _read_rows(path, max_age)
        if rows is not None:
            return rows
    except Exception:
        pass
    max_age = kline_cache_max_age_sec() if max_age_sec is None else int(max_age_sec)
    for candidate_limit, candidate_path in _larger_cache_paths(root, symbol, bar, limit):
        if candidate_path == path:
            continue
        try:
            rows = _read_rows(candidate_path, max_age)
            if rows is not None:
                return rows[-int(limit):] if len(rows) > int(limit) else rows
        except Exception:
            continue
    return None


def load_latest_cached_close(root: Path, symbol: str, *, max_age_sec: int | None = None) -> float | None:
    cache_dir = root / "runtime" / "kline_cache"
    max_age = kline_cache_max_age_sec() if max_age_sec is None else int(max_age_sec)
    newest_mtime = -1.0
    newest_close: float | None = None
    try:
        candidates = list(cache_dir.glob(f"{str(symbol).upper()}_*_*.json"))
    except Exception:
        return None
    for path in candidates:
        try:
            mtime = path.stat().st_mtime
            if max_age > 0 and time.time() - mtime > max_age:
                continue
            payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            rows = payload.get("rows")
            if not isinstance(rows, list) or not rows:
                continue
            close = float(rows[-1][4])
            if close <= 0:
                continue
            if mtime > newest_mtime:
                newest_mtime = mtime
                newest_close = close
        except Exception:
            continue
    return newest_close


def save_cached_klines(root: Path, symbol: str, bar: str, limit: int, rows: list[list[Any]]) -> None:
    path = root / "runtime" / "kline_cache" / _safe_name(symbol, bar, limit)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps({"ts": time.time(), "rows": rows}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass
