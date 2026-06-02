"""Executor for requests persisted in the central Binance API queue."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from core.binance_api_queue import ApiQueueRequest, BinanceApiQueue, now_ms


DEFAULT_BASE_URL = "https://testnet.binancefuture.com"
BAN_UNTIL_RE = re.compile(r"banned until\s+(\d{12,})", re.IGNORECASE)


class HttpTransport(Protocol):
    def request(self, method: str, url: str, *, headers: dict[str, str], timeout: int) -> tuple[int, str]:
        ...


class UrllibTransport:
    def request(self, method: str, url: str, *, headers: dict[str, str], timeout: int) -> tuple[int, str]:
        req = urllib.request.Request(url, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return int(resp.status), resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            return int(exc.code), exc.read().decode("utf-8", errors="replace")


@dataclass(frozen=True)
class BinanceCredentials:
    api_key: str
    api_secret: str


def credentials_for_account(account: str) -> BinanceCredentials:
    key = str(account or "").upper()
    if key.startswith("A"):
        prefix = "BINANCE_A"
    elif key.startswith("B"):
        prefix = "BINANCE_B"
    elif key.startswith("C"):
        prefix = "BINANCE_C"
    else:
        raise RuntimeError(f"unknown Binance account for queued signed request: {account}")
    api_key = os.environ.get(f"{prefix}_API_KEY", "")
    api_secret = os.environ.get(f"{prefix}_API_SECRET", "")
    if not api_key or not api_secret:
        raise RuntimeError(f"missing {prefix}_API_KEY / {prefix}_API_SECRET")
    return BinanceCredentials(api_key=api_key, api_secret=api_secret)


def _body_params(request: ApiQueueRequest) -> dict[str, Any]:
    body = request.body if isinstance(request.body, dict) else {}
    return {str(k): v for k, v in body.items() if v is not None}


def _signed_url(
    request: ApiQueueRequest,
    *,
    credentials: BinanceCredentials,
    base_url: str,
    timestamp_ms: int,
) -> tuple[str, dict[str, str]]:
    params = _body_params(request)
    params.setdefault("timestamp", timestamp_ms)
    params.setdefault("recvWindow", 5000)
    query = urllib.parse.urlencode(params)
    signature = hmac.new(credentials.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"{base_url}{request.path}?{query}&signature={signature}"
    headers = {
        "X-MBX-APIKEY": credentials.api_key,
        "Content-Type": "application/json",
    }
    headers.update({str(k): str(v) for k, v in request.headers.items()})
    return url, headers


def _public_url(request: ApiQueueRequest, *, base_url: str) -> tuple[str, dict[str, str]]:
    params = _body_params(request)
    query = urllib.parse.urlencode(params)
    if request.url:
        url = request.url
    else:
        url = f"{base_url}{request.path}"
    if query:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{query}"
    headers = {str(k): str(v) for k, v in request.headers.items()}
    return url, headers


def _decode_body(raw: str) -> Any:
    try:
        return json.loads(raw)
    except Exception:
        return raw


def _rate_limit_cooldown_ms(status: int, body: str, *, fallback_ms: int) -> int:
    lowered = body.lower()
    if status not in {418, 429} and "-1003" not in body and "too many requests" not in lowered:
        return 0
    match = BAN_UNTIL_RE.search(body)
    if match:
        return int(match.group(1)) + 10 * 60 * 1000
    return now_ms() + max(60_000, int(fallback_ms))


def execute_api_queue_request(
    queue: BinanceApiQueue,
    request: ApiQueueRequest,
    *,
    transport: HttpTransport | None = None,
    base_url: str = DEFAULT_BASE_URL,
    timeout: int = 15,
    timestamp_ms: int | None = None,
    rate_limit_fallback_ms: int = 30 * 60 * 1000,
) -> ApiQueueRequest:
    transport = transport or UrllibTransport()
    try:
        if request.scope == "signed":
            credentials = credentials_for_account(request.account)
            url, headers = _signed_url(
                request,
                credentials=credentials,
                base_url=base_url,
                timestamp_ms=int(timestamp_ms if timestamp_ms is not None else time.time() * 1000),
            )
        else:
            url, headers = _public_url(request, base_url=base_url)
        status, raw_body = transport.request(request.method, url, headers=headers, timeout=timeout)
        decoded = _decode_body(raw_body)
        cooldown = _rate_limit_cooldown_ms(status, raw_body, fallback_ms=rate_limit_fallback_ms)
        if cooldown:
            queue.set_cooldown(scope=request.scope, account=request.account, until_ms=cooldown, reason=f"HTTP {status}")
            return queue.fail_request(request.request_id, error=f"HTTP {status}: {raw_body[:500]}", retry=True, defer_ms=max(60_000, cooldown - now_ms()))
        if status >= 400:
            return queue.fail_request(request.request_id, error=f"HTTP {status}: {raw_body[:500]}", retry=False)
        return queue.complete_request(request.request_id, result_status=status, result_body=decoded)
    except Exception as exc:
        return queue.fail_request(request.request_id, error=str(exc), retry=True, defer_ms=5_000)


def execute_next_api_queue_request(
    queue: BinanceApiQueue,
    *,
    worker_id: str = "api_queue_executor",
    lease_ms: int = 30_000,
    transport: HttpTransport | None = None,
    base_url: str = DEFAULT_BASE_URL,
    timeout: int = 15,
) -> ApiQueueRequest | None:
    request = queue.lease_next(worker_id=worker_id, lease_ms=lease_ms)
    if request is None:
        return None
    return execute_api_queue_request(queue, request, transport=transport, base_url=base_url, timeout=timeout)
