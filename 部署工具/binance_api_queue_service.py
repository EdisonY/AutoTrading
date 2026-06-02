"""Binance API queue service entrypoint.

Construction-mode service: keeps the central queue schema alive and reports
ready work/cooldowns. Future scanner integration will enqueue real REST work.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name == "部署工具" else SCRIPT_DIR
sys.path.insert(0, str(ROOT))

from core.binance_api_executor import execute_next_api_queue_request
from core.binance_api_queue import BinanceApiQueue


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Central Binance API queue service")
    parser.add_argument("--db", default=str(ROOT / "runtime" / "binance_api_queue.sqlite3"))
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--lease-ms", type=int, default=30_000)
    parser.add_argument("--execute", action="store_true", help="Execute leased requests instead of foundation-mode deferral")
    return parser.parse_args(argv)


def run_once(queue: BinanceApiQueue, *, lease_ms: int, execute: bool = False) -> dict[str, object]:
    if execute:
        result_request = execute_next_api_queue_request(queue, worker_id="api_queue_service", lease_ms=lease_ms)
        summary = queue.summary()
        result = {
            "leased": result_request.request_id if result_request else "",
            "status": result_request.status if result_request else "",
            "summary": summary,
        }
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return result
    request = queue.lease_next(worker_id="api_queue_service", lease_ms=lease_ms)
    summary = queue.summary()
    result = {"leased": request.request_id if request else "", "summary": summary}
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if request:
        queue.fail_request(
            request.request_id,
            error="api_queue_service foundation mode: executor not attached",
            retry=True,
            defer_ms=lease_ms,
        )
    return result


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    queue = BinanceApiQueue(args.db)
    while True:
        run_once(queue, lease_ms=args.lease_ms, execute=args.execute)
        if args.once:
            return 0
        time.sleep(max(1.0, float(args.interval)))


if __name__ == "__main__":
    raise SystemExit(main())
