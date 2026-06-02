"""Synchronous client bridge for the central Binance API queue.

Scanner clients use this module to submit REST intent to the queue and wait for
the executor result. When enabled, scanner processes do not call Binance REST
directly; if the executor is not running, the request fails closed.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from core.binance_api_queue import (
    DEFAULT_DB_PATH,
    PRIORITY_HIGH,
    PRIORITY_NORMAL,
    PRIORITY_TRADE,
    STATUS_DONE,
    STATUS_DEFERRED,
    STATUS_FAILED,
    STATUS_QUEUED,
    BinanceApiQueue,
)


def api_queue_client_enabled() -> bool:
    value = os.environ.get("BINANCE_API_QUEUE_CLIENT_ENABLED", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def default_queue_db_path() -> Path:
    return Path(os.environ.get("BINANCE_API_QUEUE_DB", str(DEFAULT_DB_PATH)))


def priority_for_request(method: str, path: str) -> int:
    method = method.upper()
    if method in {"POST", "DELETE"} and (
        "/fapi/v1/order" in path
        or "/fapi/v1/leverage" in path
        or "/fapi/v1/marginType" in path
        or "/fapi/v1/allOpenOrders" in path
    ):
        return PRIORITY_TRADE
    if "/fapi/v1/listenKey" in path or method in {"POST", "DELETE"}:
        return PRIORITY_HIGH
    return PRIORITY_NORMAL


def queued_api_request(
    *,
    scope: str,
    account: str = "",
    label: str = "",
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    headers: dict[str, Any] | None = None,
    url: str = "",
    priority: int | None = None,
    queue: BinanceApiQueue | None = None,
    timeout_sec: float | None = None,
    poll_interval_sec: float | None = None,
) -> Any:
    queue = queue or BinanceApiQueue(default_queue_db_path())
    timeout = float(timeout_sec if timeout_sec is not None else os.environ.get("BINANCE_API_QUEUE_CLIENT_TIMEOUT_SEC", "60"))
    poll_interval = float(
        poll_interval_sec if poll_interval_sec is not None else os.environ.get("BINANCE_API_QUEUE_CLIENT_POLL_SEC", "0.2")
    )
    cooldown_until, cooldown_reason = queue.active_cooldown(scope=scope, account=account)
    if cooldown_until:
        return {
            "code": "-1",
            "msg": f"queued request blocked by active cooldown: {cooldown_reason}".strip(),
            "queue_status": "cooldown",
            "cooldown_until_ms": cooldown_until,
            "cooldown_reason": cooldown_reason,
        }
    request = queue.submit_request(
        scope=scope,
        account=account,
        label=label,
        method=method,
        path=path,
        url=url,
        headers=headers or {},
        body=body or {},
        priority=priority if priority is not None else priority_for_request(method, path),
    )
    deadline = time.monotonic() + max(0.1, timeout)
    while True:
        current = queue.get_request(request.request_id)
        if current is None:
            return {"code": "-1", "msg": f"queued request disappeared: {request.request_id}"}
        if current.status == STATUS_DONE:
            return current.result_body
        if current.status == STATUS_FAILED:
            return {"code": "-1", "msg": current.error or f"queued request failed: {request.request_id}"}
        if time.monotonic() >= deadline:
            cancelled = False
            if current.status in {STATUS_QUEUED, STATUS_DEFERRED}:
                queue.fail_request(
                    current.request_id,
                    error=f"client timeout cancelled queued request: {current.status} {current.error}".strip(),
                    retry=False,
                )
                cancelled = True
            return {
                "code": "-1",
                "msg": f"queued request timeout: {current.status} {current.error}".strip(),
                "request_id": current.request_id,
                "queue_status": current.status,
                "request_cancelled": cancelled,
            }
        time.sleep(max(0.02, poll_interval))
