"""Shared Binance REST pressure guard.

This module is intentionally small and dependency-free. It coordinates the
three live scanners plus the account snapshot service through a file-backed
lock/state pair, so one process cannot unknowingly hammer Binance while another
process is already in cooldown.
"""

from __future__ import annotations

import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = Path(os.environ.get("BINANCE_API_GUARD_DIR") or ROOT / "runtime")
STATE_PATH = STATE_DIR / "binance_api_guard_state.json"
LOCK_PATH = STATE_DIR / "binance_api_guard_state.lock"
BAN_UNTIL_RE = re.compile(r"banned until\s+(\d{12,})", re.IGNORECASE)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _min_interval_ms() -> int:
    return max(0, int(os.environ.get("BINANCE_API_GUARD_MIN_INTERVAL_MS", "350")))


def _ban_grace_ms() -> int:
    return max(0, int(os.environ.get("BINANCE_API_GUARD_BAN_GRACE_MS", str(5 * 60 * 1000))))


def _max_requests_per_minute() -> int:
    return max(1, int(os.environ.get("BINANCE_API_GUARD_MAX_REQUESTS_PER_MIN", "120")))


def _load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_state(state: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


@contextmanager
def _locked():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+", encoding="utf-8") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def wait_before_request(account: str, method: str, path: str) -> float:
    """Throttle all signed REST requests across local live processes."""
    total_sleep = 0.0
    while True:
        with _locked():
            state = _load_state()
            now = _now_ms()
            banned_until = int(state.get("banned_until_ms") or 0)
            if banned_until > now:
                sleep_seconds = min(60.0, max(1.0, (banned_until - now) / 1000))
            else:
                last_request_ms = int(state.get("last_request_ms") or 0)
                recent = [
                    item for item in state.get("recent_requests", [])
                    if now - int(item.get("ts_ms") or 0) <= 60_000
                ]
                if len(recent) >= _max_requests_per_minute():
                    oldest = min(int(item.get("ts_ms") or now) for item in recent)
                    sleep_seconds = min(10.0, max(1.0, (oldest + 60_000 - now) / 1000))
                    state["recent_requests"] = recent
                    state["rolling_count_60s"] = len(recent)
                    _write_state(state)
                else:
                    wait_ms = _min_interval_ms() - (now - last_request_ms)
                    if wait_ms > 0:
                        sleep_seconds = min(5.0, wait_ms / 1000)
                    else:
                        state["last_request_ms"] = now
                        state["last_account"] = account
                        state["last_method"] = method
                        state["last_path"] = path
                        state["updated_at_ms"] = now
                        recent.append({"ts_ms": now, "account": account, "method": method, "path": path})
                        state["recent_requests"] = recent[-_max_requests_per_minute():]
                        state["rolling_count_60s"] = len(state["recent_requests"])
                        state["max_requests_per_min"] = _max_requests_per_minute()
                        stats = state.setdefault("stats", {})
                        key = f"{account}:{method}:{path}"
                        stats[key] = int(stats.get(key) or 0) + 1
                        _write_state(state)
                        return total_sleep
        time.sleep(sleep_seconds)
        total_sleep += sleep_seconds


def record_response(account: str, method: str, path: str, status_code: int | str | None, body: str = "") -> None:
    """Persist ban/cooldown evidence so sibling processes back off too."""
    text = str(body or "")
    status = str(status_code or "")
    code = ""
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            code = str(parsed.get("code") or "")
            text = f"{text} {parsed.get('msg') or ''}"
    except Exception:
        pass
    lowered = text.lower()
    banned_until_ms = 0
    match = BAN_UNTIL_RE.search(text)
    if match:
        banned_until_ms = int(match.group(1)) + _ban_grace_ms()
    elif status in {"418", "429"} or code in {"-1003"} or "too many requests" in lowered:
        banned_until_ms = _now_ms() + max(_ban_grace_ms(), 15 * 60 * 1000)
    with _locked():
        state = _load_state()
        now = _now_ms()
        state["updated_at_ms"] = now
        state["last_status"] = status
        state["last_error_account"] = account
        state["last_error_method"] = method
        state["last_error_path"] = path
        if banned_until_ms:
            state["banned_until_ms"] = max(int(state.get("banned_until_ms") or 0), banned_until_ms)
            state["last_ban_reason"] = text[:500]
        _write_state(state)
