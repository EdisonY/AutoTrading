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
import urllib.parse
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
    return max(0, int(os.environ.get("BINANCE_API_GUARD_MIN_INTERVAL_MS", "650")))


def _ban_grace_ms() -> int:
    return max(0, int(os.environ.get("BINANCE_API_GUARD_BAN_GRACE_MS", str(10 * 60 * 1000))))


def _rate_limit_fallback_ms() -> int:
    return max(60_000, int(os.environ.get("BINANCE_API_GUARD_RATE_LIMIT_FALLBACK_MS", str(30 * 60 * 1000))))


def _rate_limit_fallback_max_ms() -> int:
    return max(_rate_limit_fallback_ms(), int(os.environ.get("BINANCE_API_GUARD_RATE_LIMIT_FALLBACK_MAX_MS", str(4 * 60 * 60 * 1000))))


def _rate_limit_streak_window_ms() -> int:
    return max(60_000, int(os.environ.get("BINANCE_API_GUARD_RATE_LIMIT_STREAK_WINDOW_MS", str(2 * 60 * 60 * 1000))))


def _max_requests_per_minute() -> int:
    return max(1, int(os.environ.get("BINANCE_API_GUARD_MAX_REQUESTS_PER_MIN", "90")))


def _max_requests_per_account_per_minute() -> int:
    return max(1, int(os.environ.get("BINANCE_API_GUARD_MAX_ACCOUNT_REQUESTS_PER_MIN", "45")))


def _trade_priority_reserve_per_minute() -> int:
    return max(0, int(os.environ.get("BINANCE_API_GUARD_TRADE_PRIORITY_RESERVE_PER_MIN", "20")))


def _public_min_interval_ms() -> int:
    return max(0, int(os.environ.get("BINANCE_PUBLIC_API_GUARD_MIN_INTERVAL_MS", "1400")))


def _public_max_requests_per_minute() -> int:
    return max(1, int(os.environ.get("BINANCE_PUBLIC_API_GUARD_MAX_REQUESTS_PER_MIN", "45")))


def current_cooldown_seconds(now_ms: int | None = None) -> float:
    """Return shared Binance REST cooldown seconds from the persisted guard state."""
    try:
        now = _now_ms() if now_ms is None else int(now_ms)
        banned_until = int(_load_state().get("banned_until_ms") or 0)
        return max(0.0, (banned_until - now) / 1000)
    except Exception:
        return 0.0


def _request_priority(method: str, path: str) -> str:
    method_upper = method.upper()
    if method_upper in {"POST", "DELETE"}:
        return "trade"
    if path in {"/fapi/v1/order", "/fapi/v1/leverage", "/fapi/v1/marginType", "/fapi/v1/allOpenOrders"}:
        return "trade"
    return "normal"


def _public_path(url_or_path: str) -> str:
    parsed = urllib.parse.urlparse(str(url_or_path))
    path = parsed.path or str(url_or_path)
    return path or "/"


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
    priority = _request_priority(method, path)
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
                account_recent = [item for item in recent if str(item.get("account") or "") == account]
                max_per_min = _max_requests_per_minute()
                account_max = _max_requests_per_account_per_minute()
                reserve = min(_trade_priority_reserve_per_minute(), max(0, max_per_min - 1))
                normal_limit = max(1, max_per_min - reserve)
                normal_over_budget = priority != "trade" and len(recent) >= normal_limit
                if len(recent) >= max_per_min or len(account_recent) >= account_max or normal_over_budget:
                    budget_rows = recent if (len(recent) >= max_per_min or normal_over_budget) else account_recent
                    oldest = min(int(item.get("ts_ms") or now) for item in budget_rows)
                    sleep_seconds = min(10.0, max(1.0, (oldest + 60_000 - now) / 1000))
                    state["recent_requests"] = recent
                    state["rolling_count_60s"] = len(recent)
                    state["rolling_account_count_60s"] = len(account_recent)
                    state["rolling_limited_account"] = account if len(account_recent) >= account_max else ""
                    state["rolling_limited_priority"] = "normal_reserved" if normal_over_budget else ""
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
                        state["last_priority"] = priority
                        state["last_status"] = ""
                        state["updated_at_ms"] = now
                        recent.append({"ts_ms": now, "account": account, "method": method, "path": path, "priority": priority})
                        state["recent_requests"] = recent[-max_per_min:]
                        state["rolling_count_60s"] = len(state["recent_requests"])
                        state["rolling_account_count_60s"] = len(account_recent) + 1
                        state["max_requests_per_min"] = max_per_min
                        state["max_account_requests_per_min"] = account_max
                        state["trade_priority_reserve_per_min"] = reserve
                        state["normal_priority_limit_per_min"] = normal_limit
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
    is_rate_limited = status in {"418", "429"} or code in {"-1003"} or "too many requests" in lowered
    banned_until_ms = 0
    match = BAN_UNTIL_RE.search(text)
    if match:
        banned_until_ms = int(match.group(1)) + _ban_grace_ms()
    with _locked():
        state = _load_state()
        now = _now_ms()
        fallback_ms = 0
        if is_rate_limited and not banned_until_ms:
            previous_at = int(state.get("last_rate_limit_error_at_ms") or 0)
            previous_streak = int(state.get("rate_limit_error_streak") or 0)
            if previous_at and now - previous_at <= _rate_limit_streak_window_ms():
                streak = min(previous_streak + 1, 8)
            else:
                streak = 1
            fallback_ms = min(_rate_limit_fallback_max_ms(), _rate_limit_fallback_ms() * (2 ** max(0, streak - 1)))
            banned_until_ms = now + max(_ban_grace_ms(), fallback_ms)
            state["rate_limit_error_streak"] = streak
            state["rate_limit_fallback_ms"] = fallback_ms
        elif is_rate_limited:
            state["rate_limit_error_streak"] = int(state.get("rate_limit_error_streak") or 0) + 1
            state["rate_limit_fallback_ms"] = 0
        elif status:
            state["rate_limit_error_streak"] = 0
            state["rate_limit_fallback_ms"] = 0
        state["updated_at_ms"] = now
        state["last_status"] = status
        state["last_error_at_ms"] = now
        state["last_error_account"] = account
        state["last_error_method"] = method
        state["last_error_path"] = path
        state["last_error_status"] = status
        state["last_error_body"] = text[:500]
        if banned_until_ms:
            state["banned_until_ms"] = max(int(state.get("banned_until_ms") or 0), banned_until_ms)
            state["last_ban_reason"] = text[:500]
        if is_rate_limited:
            state["last_rate_limit_error_at_ms"] = now
            state["last_rate_limit_error_status"] = status
        _write_state(state)


def wait_before_public_request(label: str, url_or_path: str) -> float:
    """Throttle public Binance REST requests across scanners and market services."""
    total_sleep = 0.0
    path = _public_path(url_or_path)
    while True:
        with _locked():
            state = _load_state()
            now = _now_ms()
            banned_until = int(state.get("banned_until_ms") or 0)
            if banned_until > now:
                sleep_seconds = min(60.0, max(1.0, (banned_until - now) / 1000))
            else:
                last_request_ms = int(state.get("last_public_request_ms") or 0)
                recent = [
                    item for item in state.get("recent_public_requests", [])
                    if now - int(item.get("ts_ms") or 0) <= 60_000
                ]
                max_per_min = _public_max_requests_per_minute()
                if len(recent) >= max_per_min:
                    oldest = min(int(item.get("ts_ms") or now) for item in recent)
                    sleep_seconds = min(10.0, max(1.0, (oldest + 60_000 - now) / 1000))
                    state["recent_public_requests"] = recent
                    state["public_rolling_count_60s"] = len(recent)
                    state["public_max_requests_per_min"] = max_per_min
                    _write_state(state)
                else:
                    wait_ms = _public_min_interval_ms() - (now - last_request_ms)
                    if wait_ms > 0:
                        sleep_seconds = min(2.0, wait_ms / 1000)
                    else:
                        state["last_public_request_ms"] = now
                        state["last_public_label"] = label
                        state["last_public_path"] = path
                        state["updated_at_ms"] = now
                        recent.append({"ts_ms": now, "label": label, "path": path})
                        state["recent_public_requests"] = recent[-max_per_min:]
                        state["public_rolling_count_60s"] = len(state["recent_public_requests"])
                        state["public_max_requests_per_min"] = max_per_min
                        public_stats = state.setdefault("public_stats", {})
                        key = f"{label}:{path}"
                        public_stats[key] = int(public_stats.get(key) or 0) + 1
                        _write_state(state)
                        return total_sleep
        time.sleep(sleep_seconds)
        total_sleep += sleep_seconds


def record_public_response(label: str, url_or_path: str, status_code: int | str | None, body: str = "") -> None:
    """Share public REST ban evidence with signed REST callers."""
    path = _public_path(url_or_path)
    record_response(f"public:{label}", "GET", path, status_code, body)
