from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


MARKET_BASE_URL = os.environ.get("BINANCE_MARKET_BASE_URL", "https://fapi.binance.com").strip().rstrip("/")
TICKER_URL = f"{MARKET_BASE_URL}/fapi/v1/ticker/24hr"

from core.binance_api_guard import record_public_response, wait_before_public_request
from core.binance_api_queue_client import api_queue_client_enabled, queued_api_request


def fetch_json(url: str, timeout: int = 12):
    if api_queue_client_enabled():
        queue_timeout = max(timeout + 5, int(float(os.environ.get("BINANCE_API_QUEUE_CLIENT_TIMEOUT_SEC", "180"))))
        data = queued_api_request(scope="public", label="market-data-cache", method="GET", path=url, url=url, timeout_sec=queue_timeout)
        if isinstance(data, dict) and data.get("code") is not None and str(data.get("code")) != "200":
            raise RuntimeError(str(data.get("msg") or data))
        return data
    wait_before_public_request("market-data-cache", url)
    req = urllib.request.Request(url, headers={"User-Agent": "AutoTrading-MarketDataService/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if exc.code in {418, 429}:
            record_public_response("market-data-cache", url, exc.code, body)
        raise


def build_payload(prev_volumes: dict[str, float], top_limit: int) -> tuple[dict, dict[str, float]]:
    raw = fetch_json(TICKER_URL)
    rows = []
    current_volumes = {}
    for item in raw:
        sym = str(item.get("symbol") or "").upper()
        if not sym.endswith("USDT"):
            continue
        try:
            quote_volume = float(item.get("quoteVolume") or 0)
            change_pct = float(item.get("priceChangePercent") or 0)
        except Exception:
            quote_volume = 0.0
            change_pct = 0.0
        current_volumes[sym] = quote_volume
        rows.append((sym, quote_volume, change_pct))
    rows.sort(key=lambda x: x[1], reverse=True)
    spikes = []
    for sym, volume in current_volumes.items():
        prev = prev_volumes.get(sym, 0)
        if prev > 0 and volume > prev * 5:
            spikes.append((sym, volume / prev, volume))
    spikes.sort(key=lambda x: x[1], reverse=True)
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "unix_ts": time.time(),
        "source": TICKER_URL,
        "available_symbols": [sym for sym, _, _ in rows],
        "top_symbols": [sym for sym, _, _ in rows[:top_limit]],
        "spike_symbols": [sym for sym, _, _ in spikes[:top_limit]],
        "top_preview": [
            {"symbol": sym, "quote_volume": volume, "change_pct": change}
            for sym, volume, change in rows[:20]
        ],
        "spike_preview": [
            {"symbol": sym, "volume_mult": mult, "quote_volume": volume}
            for sym, mult, volume in spikes[:20]
        ],
    }
    return payload, current_volumes


def atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Unified lightweight market data cache service.")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--interval", type=int, default=15)
    parser.add_argument("--top-limit", type=int, default=160)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    out = args.root / "runtime" / "market_data_cache.json"
    prev_volumes: dict[str, float] = {}
    while True:
        try:
            payload, prev_volumes = build_payload(prev_volumes, args.top_limit)
            atomic_write(out, payload)
            print(json.dumps({"status": "ok", "symbols": len(payload["available_symbols"]), "top": len(payload["top_symbols"]), "spikes": len(payload["spike_symbols"])}), flush=True)
        except Exception as exc:
            print(json.dumps({"status": "error", "error": str(exc)[:240]}), flush=True)
        if args.once:
            return 0
        time.sleep(max(5, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
