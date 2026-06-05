"""Read-only external market data helpers.

These helpers are intentionally public-data only. They do not sign requests and
do not submit orders. The first enabled source is OKX, used to reduce pressure
on Binance/Testnet while still keeping Binance as the execution venue.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from typing import Any


OKX_BASE_URL = os.environ.get("OKX_MARKET_BASE_URL", "https://www.okx.com").strip().rstrip("/")
_OKX_REQUEST_TIMES: list[float] = []
_OKX_NEGATIVE_UNTIL: dict[str, float] = {}


def okx_market_data_enabled() -> bool:
    value = os.environ.get("OKX_MARKET_DATA_ENABLED", "0").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _okx_rate_limit() -> bool:
    max_per_min = int(os.environ.get("OKX_MARKET_DATA_MAX_PER_MIN", "90"))
    now = time.time()
    while _OKX_REQUEST_TIMES and _OKX_REQUEST_TIMES[0] < now - 60:
        _OKX_REQUEST_TIMES.pop(0)
    if max_per_min > 0 and len(_OKX_REQUEST_TIMES) >= max_per_min:
        return False
    _OKX_REQUEST_TIMES.append(time.time())
    return True


def _negative_key(kind: str, symbol: str, interval: str | None = None) -> str:
    parts = [kind, okx_inst_id(symbol)]
    if interval:
        parts.append(okx_bar(interval))
    return ":".join(parts)


def _negative_blocked(key: str) -> bool:
    until = _OKX_NEGATIVE_UNTIL.get(key, 0.0)
    if until <= time.time():
        _OKX_NEGATIVE_UNTIL.pop(key, None)
        return False
    return True


def _mark_negative(key: str) -> None:
    ttl = int(os.environ.get("OKX_MARKET_DATA_NEGATIVE_TTL_SEC", "3600"))
    if ttl > 0:
        _OKX_NEGATIVE_UNTIL[key] = time.time() + ttl


def okx_inst_id(symbol: str) -> str:
    sym = str(symbol or "").upper().strip()
    if sym.endswith("-SWAP"):
        return sym
    if sym.endswith("USDT"):
        base = sym[:-4]
        return f"{base}-USDT-SWAP"
    return sym


def okx_symbol_supported(symbol: str) -> bool:
    sym = str(symbol or "").upper().strip()
    if not sym.endswith("USDT"):
        return False
    base = sym[:-4]
    return bool(base) and base.isascii() and base.replace("-", "").isalnum()


def okx_bar(interval: str) -> str:
    value = str(interval or "").strip()
    mapping = {
        "1m": "1m",
        "3m": "3m",
        "5m": "5m",
        "15m": "15m",
        "30m": "30m",
        "1h": "1H",
        "2h": "2H",
        "4h": "4H",
        "1d": "1D",
    }
    return mapping.get(value.lower(), value)


def okx_public_get(path: str, params: dict[str, Any], timeout: int = 10) -> dict[str, Any]:
    if not _okx_rate_limit():
        raise RuntimeError("okx_rate_budget_exhausted")
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{OKX_BASE_URL}{path}"
    if query:
        url = f"{url}?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": "AutoTrading-ExternalMarketData/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    if str(payload.get("code", "0")) != "0":
        raise RuntimeError(str(payload.get("msg") or payload))
    return payload


def fetch_okx_klines(symbol: str, interval: str, limit: int = 200) -> list[list[str]]:
    if not okx_market_data_enabled():
        return []
    if not okx_symbol_supported(symbol):
        return []
    key = _negative_key("klines", symbol, interval)
    if _negative_blocked(key):
        return []
    try:
        payload = okx_public_get(
            "/api/v5/market/candles",
            {"instId": okx_inst_id(symbol), "bar": okx_bar(interval), "limit": int(limit)},
        )
    except Exception as exc:
        if "okx_rate_budget_exhausted" not in str(exc):
            _mark_negative(key)
        raise
    rows = []
    for row in payload.get("data") or []:
        if len(row) < 8:
            continue
        open_ms = str(row[0])
        close_ms = str(int(float(row[0])) + 1)
        rows.append([
            open_ms,
            str(row[1]),
            str(row[2]),
            str(row[3]),
            str(row[4]),
            str(row[5]),
            close_ms,
            str(row[7]),
        ])
    rows.reverse()
    if not rows:
        _mark_negative(key)
    return rows[-int(limit):]


def fetch_okx_ofi(symbol: str, limit: int = 20) -> float | None:
    if not okx_market_data_enabled():
        return None
    if not okx_symbol_supported(symbol):
        return None
    key = _negative_key("books", symbol)
    if _negative_blocked(key):
        return None
    try:
        payload = okx_public_get(
            "/api/v5/market/books",
            {"instId": okx_inst_id(symbol), "sz": int(limit)},
        )
    except Exception as exc:
        if "okx_rate_budget_exhausted" not in str(exc):
            _mark_negative(key)
        raise
    data = (payload.get("data") or [{}])[0]
    bids = data.get("bids") or []
    asks = data.get("asks") or []
    if not bids and not asks:
        _mark_negative(key)
        return None
    bid_q = sum(float(row[1]) for row in bids[:10])
    ask_q = sum(float(row[1]) for row in asks[:10])
    total = bid_q + ask_q
    return (bid_q - ask_q) / total if total > 0 else 0.0


def fetch_okx_funding_rate(symbol: str) -> float | None:
    if not okx_market_data_enabled():
        return None
    if not okx_symbol_supported(symbol):
        return None
    key = _negative_key("funding", symbol)
    if _negative_blocked(key):
        return None
    try:
        payload = okx_public_get(
            "/api/v5/public/funding-rate",
            {"instId": okx_inst_id(symbol)},
        )
    except Exception as exc:
        if "okx_rate_budget_exhausted" not in str(exc):
            _mark_negative(key)
        raise
    data = payload.get("data") or []
    if not data:
        _mark_negative(key)
        return 0.0
    return float(data[0].get("fundingRate") or 0.0) * 100


def fetch_okx_cvd(symbol: str, limit: int = 100) -> float | None:
    if not okx_market_data_enabled():
        return None
    if not okx_symbol_supported(symbol):
        return None
    key = _negative_key("trades", symbol)
    if _negative_blocked(key):
        return None
    try:
        payload = okx_public_get(
            "/api/v5/market/trades",
            {"instId": okx_inst_id(symbol), "limit": int(limit)},
        )
    except Exception as exc:
        if "okx_rate_budget_exhausted" not in str(exc):
            _mark_negative(key)
        raise
    buy_vol = 0.0
    sell_vol = 0.0
    trades = payload.get("data") or []
    if not trades:
        _mark_negative(key)
        return None
    for trade in trades:
        qty = float(trade.get("sz") or 0.0)
        side = str(trade.get("side") or "").lower()
        if side == "buy":
            buy_vol += qty
        elif side == "sell":
            sell_vol += qty
    total = buy_vol + sell_vol
    return (buy_vol - sell_vol) / total if total > 0 else 0.0
