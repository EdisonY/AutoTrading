"""Shared market-mover watchlist helpers.

The sentinel writes a small JSON file with fast moving symbols. Scanners read
it and merge the symbols into their normal scan universe.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

WATCHLIST_RELATIVE_PATH = Path("runtime") / "market_mover_watchlist.json"


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def watchlist_path(root: Path | None = None) -> Path:
    return (root or project_root()) / WATCHLIST_RELATIVE_PATH


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def load_sentinel_items(
    path: Path | None = None,
    *,
    max_age_sec: int = 180,
    limit: int = 40,
) -> list[dict[str, Any]]:
    """Return fresh sentinel rows, keeping order and removing duplicates."""
    file_path = path or watchlist_path()
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except Exception:
        return []

    ts = _parse_ts(payload.get("ts"))
    if not ts:
        return []
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    if age < 0 or age > max_age_sec:
        return []

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in payload.get("symbols") or []:
        sym = str(item.get("symbol") if isinstance(item, dict) else item).upper().strip()
        if not sym.endswith("USDT") or sym in seen:
            continue
        seen.add(sym)
        if isinstance(item, dict):
            row = dict(item)
            row["symbol"] = sym
        else:
            row = {"symbol": sym}
        items.append(row)
        if len(items) >= limit:
            break
    return items


def load_sentinel_context(
    path: Path | None = None,
    *,
    max_age_sec: int = 180,
    limit: int = 40,
) -> dict[str, dict[str, Any]]:
    """Return a fresh symbol -> sentinel metadata mapping."""
    return {
        str(item.get("symbol") or "").upper(): item
        for item in load_sentinel_items(path, max_age_sec=max_age_sec, limit=limit)
    }


def load_sentinel_symbols(
    path: Path | None = None,
    *,
    max_age_sec: int = 180,
    limit: int = 40,
) -> list[str]:
    """Return fresh sentinel symbols, keeping order and removing duplicates."""
    return [
        str(item.get("symbol") or "").upper()
        for item in load_sentinel_items(path, max_age_sec=max_age_sec, limit=limit)
    ]
