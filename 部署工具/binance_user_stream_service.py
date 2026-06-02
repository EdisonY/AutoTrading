"""Binance user-data-stream service.

Construction-mode entrypoint: can replay local message files into central
account state now, and can later run websocket mode once services resume.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any


if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name == "部署工具" else SCRIPT_DIR
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "部署工具"))

from account_state_service import apply_stream_events_once
from core.binance_api_queue import BinanceApiQueue
from core.binance_user_stream import refresh_listen_key_via_queue
from core.binance_user_stream_runtime import process_stream_messages, user_stream_url


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Binance user-data-stream service")
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--account", default="")
    parser.add_argument("--listen-key", default="")
    parser.add_argument("--messages-file", default="")
    parser.add_argument("--apply-state", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--ws-base", default=os.environ.get("BINANCE_USER_STREAM_WS_BASE", "wss://stream.binancefuture.com/ws"))
    parser.add_argument("--root", default=str(ROOT))
    return parser.parse_args(argv)


def _read_message_file(path: str | Path) -> list[str]:
    return Path(path).read_text(encoding="utf-8").splitlines()


def run_messages(
    *,
    root: str | Path,
    strategy: str,
    messages: list[str],
    apply_state: bool,
) -> dict[str, Any]:
    result = process_stream_messages(root, strategy=strategy, messages=messages)
    if apply_state and result.get("last_event_file"):
        apply_stream_events_once(events_path=result["last_event_file"], strategy=strategy, root=root)
    print(json.dumps({"status": "ok", **result}, ensure_ascii=False), flush=True)
    return result


def run_websocket(*, root: str | Path, account: str, strategy: str, listen_key: str, apply_state: bool, ws_base: str, once: bool) -> int:
    if not listen_key:
        account = account or strategy.split("/", 1)[0]
        record = refresh_listen_key_via_queue(root, BinanceApiQueue(Path(root) / "runtime" / "binance_api_queue.sqlite3"), account=account, strategy=strategy)
        listen_key = record.listen_key
    if not listen_key:
        raise RuntimeError("--listen-key is required for websocket mode")
    try:
        import websocket  # type: ignore
    except Exception as exc:
        raise RuntimeError("websocket-client package is required for websocket mode") from exc

    ws = websocket.create_connection(user_stream_url(listen_key, base_url=ws_base), timeout=30)
    try:
        while True:
            raw = ws.recv()
            result = process_stream_messages(root, strategy=strategy, messages=[raw])
            if apply_state and result.get("last_event_file"):
                apply_stream_events_once(events_path=result["last_event_file"], strategy=strategy, root=root)
            print(json.dumps({"status": "ok", **result}, ensure_ascii=False), flush=True)
            if once:
                return 0
    finally:
        ws.close()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.messages_file:
            run_messages(
                root=args.root,
                strategy=args.strategy,
                messages=_read_message_file(args.messages_file),
                apply_state=args.apply_state,
            )
            return 0
        return run_websocket(
            root=args.root,
            account=args.account,
            strategy=args.strategy,
            listen_key=args.listen_key,
            apply_state=args.apply_state,
            ws_base=args.ws_base,
            once=args.once,
        )
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
