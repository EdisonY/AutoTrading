"""Binance user-data-stream listen-key state helpers.

This module does not open sockets or call Binance. It gives the future account
state stream service a durable listen-key ledger and queue request specs for
start/keepalive/close operations.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.account_state import atomic_write_json, utc_now_iso
from core.binance_api_queue import PRIORITY_HIGH, PRIORITY_TRADE


ROOT = Path(__file__).resolve().parents[1]
LISTEN_KEY_STATE_FILENAME = "binance_user_stream_listen_keys.json"
DEFAULT_TTL_MS = 60 * 60 * 1000
DEFAULT_KEEPALIVE_MARGIN_MS = 15 * 60 * 1000


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class ListenKeyRecord:
    account: str
    strategy: str
    listen_key: str
    created_at_ms: int
    updated_at_ms: int
    expires_at_ms: int
    status: str
    error: str = ""

    @property
    def key(self) -> str:
        return f"{self.account}:{self.strategy}"


def _runtime(root: str | Path) -> Path:
    return Path(root) / "runtime"


def _state_path(root: str | Path) -> Path:
    return _runtime(root) / LISTEN_KEY_STATE_FILENAME


def load_listen_key_state(root: str | Path = ROOT) -> dict[str, Any]:
    try:
        payload = json.loads(_state_path(root).read_text(encoding="utf-8"))
    except Exception:
        return {"schema_version": 1, "generated_at": utc_now_iso(), "records": []}
    if not isinstance(payload, dict):
        return {"schema_version": 1, "generated_at": utc_now_iso(), "records": []}
    if not isinstance(payload.get("records"), list):
        payload["records"] = []
    return payload


def write_listen_key_state(root: str | Path, payload: dict[str, Any]) -> Path:
    payload = dict(payload)
    payload["schema_version"] = 1
    payload["generated_at"] = utc_now_iso()
    path = _state_path(root)
    atomic_write_json(path, payload)
    return path


def upsert_listen_key(
    root: str | Path,
    *,
    account: str,
    strategy: str,
    listen_key: str,
    ttl_ms: int = DEFAULT_TTL_MS,
    at_ms: int | None = None,
    status: str = "active",
    error: str = "",
) -> ListenKeyRecord:
    ts = now_ms() if at_ms is None else int(at_ms)
    payload = load_listen_key_state(root)
    records = []
    found = False
    output_record: ListenKeyRecord | None = None
    for item in payload.get("records") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("account") or "") == account and str(item.get("strategy") or "") == strategy:
            created = int(item.get("created_at_ms") or ts)
            record = ListenKeyRecord(
                account=account,
                strategy=strategy,
                listen_key=listen_key,
                created_at_ms=created,
                updated_at_ms=ts,
                expires_at_ms=ts + int(ttl_ms),
                status=status,
                error=error,
            )
            records.append(record.__dict__)
            output_record = record
            found = True
        else:
            records.append(item)
    if not found:
        record = ListenKeyRecord(
            account=account,
            strategy=strategy,
            listen_key=listen_key,
            created_at_ms=ts,
            updated_at_ms=ts,
            expires_at_ms=ts + int(ttl_ms),
            status=status,
            error=error,
        )
        records.append(record.__dict__)
        output_record = record
    payload["records"] = records
    write_listen_key_state(root, payload)
    return output_record


def mark_listen_key_error(
    root: str | Path,
    *,
    account: str,
    strategy: str,
    error: str,
    at_ms: int | None = None,
) -> None:
    ts = now_ms() if at_ms is None else int(at_ms)
    payload = load_listen_key_state(root)
    changed = False
    for item in payload.get("records") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("account") or "") == account and str(item.get("strategy") or "") == strategy:
            item["status"] = "error"
            item["error"] = error
            item["updated_at_ms"] = ts
            changed = True
    if not changed:
        payload["records"].append({
            "account": account,
            "strategy": strategy,
            "listen_key": "",
            "created_at_ms": ts,
            "updated_at_ms": ts,
            "expires_at_ms": 0,
            "status": "error",
            "error": error,
        })
    write_listen_key_state(root, payload)


def listen_key_due_records(
    root: str | Path = ROOT,
    *,
    at_ms: int | None = None,
    keepalive_margin_ms: int = DEFAULT_KEEPALIVE_MARGIN_MS,
) -> list[dict[str, Any]]:
    ts = now_ms() if at_ms is None else int(at_ms)
    due = []
    for item in load_listen_key_state(root).get("records") or []:
        if not isinstance(item, dict) or str(item.get("status") or "") != "active":
            continue
        expires = int(item.get("expires_at_ms") or 0)
        listen_key = str(item.get("listen_key") or "")
        if not listen_key or expires <= ts:
            item = dict(item)
            item["due_action"] = "restart"
            due.append(item)
        elif expires - ts <= keepalive_margin_ms:
            item = dict(item)
            item["due_action"] = "keepalive"
            due.append(item)
    return due


def listen_key_queue_request(
    *,
    action: str,
    account: str,
    strategy: str,
    listen_key: str = "",
) -> dict[str, Any]:
    action = action.lower()
    if action == "start":
        method = "POST"
        body: dict[str, Any] = {}
        priority = PRIORITY_HIGH
    elif action == "keepalive":
        method = "PUT"
        body = {"listenKey": listen_key}
        priority = PRIORITY_HIGH
    elif action == "close":
        method = "DELETE"
        body = {"listenKey": listen_key}
        priority = PRIORITY_TRADE
    else:
        raise ValueError(f"unknown listen-key action: {action}")
    return {
        "scope": "signed",
        "account": strategy,
        "label": f"user-stream:{strategy}",
        "method": method,
        "path": "/fapi/v1/listenKey",
        "priority": priority,
        "body": body,
        "idempotency_key": f"listen-key:{action}:{account}:{strategy}:{listen_key or 'new'}",
    }
