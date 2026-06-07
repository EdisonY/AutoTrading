"""Read-only external market data helpers.

These helpers are intentionally public-data only. They do not sign requests and
do not submit orders. OKX is the primary source; Bybit and CoinGecko are used
for fallback, market-universe, and sanity checks.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from typing import Any


OKX_BASE_URL = os.environ.get("OKX_MARKET_BASE_URL", "https://www.okx.com").strip().rstrip("/")
BYBIT_BASE_URL = os.environ.get("BYBIT_MARKET_BASE_URL", "https://api.bybit.com").strip().rstrip("/")
COINGECKO_BASE_URL = os.environ.get("COINGECKO_BASE_URL", "https://api.coingecko.com").strip().rstrip("/")
COINGECKO_API_KEY = os.environ.get("COINGECKO_API_KEY", "").strip()
_OKX_REQUEST_TIMES: list[float] = []
_OKX_NEGATIVE_UNTIL: dict[str, float] = {}
_BYBIT_REQUEST_TIMES: list[float] = []
_BYBIT_NEGATIVE_UNTIL: dict[str, float] = {}
_COINGECKO_REQUEST_TIMES: list[float] = []


def okx_market_data_enabled() -> bool:
    value = os.environ.get("OKX_MARKET_DATA_ENABLED", "0").strip().lower()
    return value in {"1", "true", "yes", "on"}


def bybit_market_data_enabled() -> bool:
    value = os.environ.get("BYBIT_MARKET_DATA_ENABLED", "1").strip().lower()
    return value in {"1", "true", "yes", "on"}


def coingecko_market_data_enabled() -> bool:
    value = os.environ.get("COINGECKO_MARKET_DATA_ENABLED", "1").strip().lower()
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


def _rate_limit(times: list[float], max_per_min_env: str, default: str) -> bool:
    max_per_min = int(os.environ.get(max_per_min_env, default))
    now = time.time()
    while times and times[0] < now - 60:
        times.pop(0)
    if max_per_min > 0 and len(times) >= max_per_min:
        return False
    times.append(now)
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


def _bybit_negative_blocked(key: str) -> bool:
    until = _BYBIT_NEGATIVE_UNTIL.get(key, 0.0)
    if until <= time.time():
        _BYBIT_NEGATIVE_UNTIL.pop(key, None)
        return False
    return True


def _mark_bybit_negative(key: str) -> None:
    ttl = int(os.environ.get("BYBIT_MARKET_DATA_NEGATIVE_TTL_SEC", "3600"))
    if ttl > 0:
        _BYBIT_NEGATIVE_UNTIL[key] = time.time() + ttl


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


def bybit_interval(interval: str) -> str:
    value = str(interval or "").strip().lower()
    mapping = {
        "1m": "1",
        "3m": "3",
        "5m": "5",
        "15m": "15",
        "30m": "30",
        "1h": "60",
        "2h": "120",
        "4h": "240",
        "1d": "D",
    }
    return mapping.get(value, value)


def interval_ms(interval: str) -> int:
    value = str(interval or "").strip().lower()
    if value.endswith("m"):
        return int(float(value[:-1])) * 60_000
    if value.endswith("h"):
        return int(float(value[:-1])) * 3_600_000
    if value.endswith("d"):
        return int(float(value[:-1] or 1)) * 86_400_000
    return 60_000


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


def bybit_public_get(path: str, params: dict[str, Any], timeout: int = 10) -> dict[str, Any]:
    if not _rate_limit(_BYBIT_REQUEST_TIMES, "BYBIT_MARKET_DATA_MAX_PER_MIN", "60"):
        raise RuntimeError("bybit_rate_budget_exhausted")
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{BYBIT_BASE_URL}{path}"
    if query:
        url = f"{url}?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": "AutoTrading-ExternalMarketData/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    if str(payload.get("retCode", "0")) != "0":
        raise RuntimeError(str(payload.get("retMsg") or payload))
    return payload


def coingecko_public_get(path: str, params: dict[str, Any], timeout: int = 10) -> dict[str, Any] | list[Any]:
    if not coingecko_market_data_enabled():
        return {}
    if not _rate_limit(_COINGECKO_REQUEST_TIMES, "COINGECKO_MAX_PER_MIN", "90"):
        raise RuntimeError("coingecko_rate_budget_exhausted")
    params = dict(params)
    if COINGECKO_API_KEY:
        params.setdefault("x_cg_demo_api_key", COINGECKO_API_KEY)
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{COINGECKO_BASE_URL}{path}"
    if query:
        url = f"{url}?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": "AutoTrading-ExternalMarketData/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def fetch_okx_tickers() -> list[dict[str, Any]]:
    if not okx_market_data_enabled():
        return []
    payload = okx_public_get("/api/v5/market/tickers", {"instType": "SWAP"})
    rows: list[dict[str, Any]] = []
    for item in payload.get("data") or []:
        inst = str(item.get("instId") or "")
        if not inst.endswith("-USDT-SWAP"):
            continue
        symbol = inst.replace("-USDT-SWAP", "USDT").replace("-", "")
        rows.append({
            "symbol": symbol,
            "quote_volume": float(item.get("volCcy24h") or 0.0),
            "change_pct": 0.0,
            "last": float(item.get("last") or 0.0),
            "source": "okx",
        })
    return rows


def fetch_bybit_tickers() -> list[dict[str, Any]]:
    if not bybit_market_data_enabled():
        return []
    payload = bybit_public_get("/v5/market/tickers", {"category": "linear"})
    rows: list[dict[str, Any]] = []
    for item in ((payload.get("result") or {}).get("list") or []):
        symbol = str(item.get("symbol") or "").upper()
        if not symbol.endswith("USDT") or not symbol.isascii():
            continue
        rows.append({
            "symbol": symbol,
            "quote_volume": float(item.get("turnover24h") or 0.0),
            "change_pct": float(item.get("price24hPcnt") or 0.0) * 100.0,
            "last": float(item.get("lastPrice") or 0.0),
            "source": "bybit",
        })
    return rows


def fetch_coingecko_top_markets(limit: int = 100) -> list[dict[str, Any]]:
    data = coingecko_public_get(
        "/api/v3/coins/markets",
        {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": int(limit),
            "page": 1,
            "sparkline": "false",
        },
    )
    rows: list[dict[str, Any]] = []
    if not isinstance(data, list):
        return rows
    for item in data:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").upper()
        if not symbol or not symbol.isascii():
            continue
        rows.append({
            "id": item.get("id"),
            "base": symbol,
            "symbol": f"{symbol}USDT",
            "market_cap": float(item.get("market_cap") or 0.0),
            "price": float(item.get("current_price") or 0.0),
            "source": "coingecko",
        })
    return rows


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


def fetch_bybit_klines(symbol: str, interval: str, limit: int = 200) -> list[list[str]]:
    if not bybit_market_data_enabled():
        return []
    symbol = str(symbol or "").upper()
    if not symbol.endswith("USDT") or not symbol.isascii():
        return []
    key = f"bybit:klines:{symbol}:{bybit_interval(interval)}"
    if _bybit_negative_blocked(key):
        return []
    try:
        payload = bybit_public_get(
            "/v5/market/kline",
            {"category": "linear", "symbol": symbol, "interval": bybit_interval(interval), "limit": int(limit)},
        )
    except Exception as exc:
        if "bybit_rate_budget_exhausted" not in str(exc):
            _mark_bybit_negative(key)
        raise
    rows = []
    step_ms = interval_ms(interval)
    for row in ((payload.get("result") or {}).get("list") or []):
        if len(row) < 7:
            continue
        open_ms = int(float(row[0]))
        close_ms = open_ms + step_ms - 1
        rows.append([
            str(open_ms),
            str(row[1]),
            str(row[2]),
            str(row[3]),
            str(row[4]),
            str(row[5]),
            str(close_ms),
            str(row[6]),
        ])
    rows.sort(key=lambda item: int(float(item[0])))
    if not rows:
        _mark_bybit_negative(key)
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


def fetch_bybit_ofi(symbol: str, limit: int = 20) -> float | None:
    if not bybit_market_data_enabled():
        return None
    symbol = str(symbol or "").upper()
    key = f"bybit:books:{symbol}"
    if _bybit_negative_blocked(key):
        return None
    try:
        payload = bybit_public_get("/v5/market/orderbook", {"category": "linear", "symbol": symbol, "limit": int(limit)})
    except Exception as exc:
        if "bybit_rate_budget_exhausted" not in str(exc):
            _mark_bybit_negative(key)
        raise
    result = payload.get("result") or {}
    bids = result.get("b") or []
    asks = result.get("a") or []
    if not bids and not asks:
        _mark_bybit_negative(key)
        return None
    bid_q = sum(float(row[1]) for row in bids[:10])
    ask_q = sum(float(row[1]) for row in asks[:10])
    total = bid_q + ask_q
    return (bid_q - ask_q) / total if total > 0 else 0.0


def fetch_bybit_cvd(symbol: str, limit: int = 100) -> float | None:
    if not bybit_market_data_enabled():
        return None
    symbol = str(symbol or "").upper()
    key = f"bybit:trades:{symbol}"
    if _bybit_negative_blocked(key):
        return None
    try:
        payload = bybit_public_get("/v5/market/recent-trade", {"category": "linear", "symbol": symbol, "limit": int(limit)})
    except Exception as exc:
        if "bybit_rate_budget_exhausted" not in str(exc):
            _mark_bybit_negative(key)
        raise
    trades = (payload.get("result") or {}).get("list") or []
    if not trades:
        _mark_bybit_negative(key)
        return None
    buy_vol = 0.0
    sell_vol = 0.0
    for trade in trades:
        qty = float(trade.get("size") or 0.0)
        side = str(trade.get("side") or "").lower()
        if side == "buy":
            buy_vol += qty
        elif side == "sell":
            sell_vol += qty
    total = buy_vol + sell_vol
    return (buy_vol - sell_vol) / total if total > 0 else 0.0


def fetch_bybit_funding_rate(symbol: str) -> float | None:
    if not bybit_market_data_enabled():
        return None
    symbol = str(symbol or "").upper()
    key = f"bybit:funding:{symbol}"
    if _bybit_negative_blocked(key):
        return None
    try:
        payload = bybit_public_get("/v5/market/tickers", {"category": "linear", "symbol": symbol})
    except Exception as exc:
        if "bybit_rate_budget_exhausted" not in str(exc):
            _mark_bybit_negative(key)
        raise
    rows = (payload.get("result") or {}).get("list") or []
    if not rows:
        _mark_bybit_negative(key)
        return None
    return float(rows[0].get("fundingRate") or 0.0) * 100.0
