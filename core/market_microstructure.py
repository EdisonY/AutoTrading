"""Small cache reader for B/v16 market microstructure features."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def load_microstructure_latest(root: Path, symbol: str, *, max_age_sec: int = 240) -> dict[str, Any]:
    path = Path(root) / "runtime" / "market_microstructure_latest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}
    features = payload.get("features") if isinstance(payload, dict) else {}
    row = features.get(str(symbol).upper()) if isinstance(features, dict) else None
    if not isinstance(row, dict):
        return {}
    ts = float(row.get("unix_ts") or 0.0)
    if ts <= 0 or time.time() - ts > max_age_sec:
        return {}
    return row
