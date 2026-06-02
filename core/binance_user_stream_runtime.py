"""Runtime helpers for Binance user-data-stream messages."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from core.audit_log import write_jsonl_with_daily_shard


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVENT_LOG = ROOT / "logs" / "binance_user_stream_events.jsonl"
DEFAULT_WS_BASE = "wss://stream.binancefuture.com/ws"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def user_stream_url(listen_key: str, *, base_url: str = DEFAULT_WS_BASE) -> str:
    base = base_url.rstrip("/")
    return f"{base}/{listen_key}"


def parse_stream_message(raw: str | bytes | dict[str, Any]) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def stream_event_record(strategy: str, event: dict[str, Any], *, source: str = "user_data_stream") -> dict[str, Any]:
    event_ts = event.get("E") or event.get("T") or ""
    return {
        "ts": utc_now_iso(),
        "strategy": strategy,
        "source": source,
        "event_type": str(event.get("e") or event.get("event_type") or ""),
        "event_time": event_ts,
        "event": event,
    }


def append_stream_event(
    root: str | Path,
    *,
    strategy: str,
    event: dict[str, Any],
    log_path: str | Path | None = None,
) -> Path:
    path = Path(log_path) if log_path else Path(root) / "logs" / "binance_user_stream_events.jsonl"
    write_jsonl_with_daily_shard(path, stream_event_record(strategy, event))
    return path


def write_single_event_file(root: str | Path, *, strategy: str, event: dict[str, Any]) -> Path:
    safe = strategy.replace("/", "_").replace("\\", "_")
    path = Path(root) / "runtime" / f"user_stream_event_{safe}_latest.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stream_event_record(strategy, event), ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    return path


def process_stream_messages(
    root: str | Path,
    *,
    strategy: str,
    messages: Iterable[str | bytes | dict[str, Any]],
    log_path: str | Path | None = None,
) -> dict[str, Any]:
    parsed = 0
    ignored = 0
    last_event_file = ""
    output_log = None
    for raw in messages:
        event = parse_stream_message(raw)
        if not event:
            ignored += 1
            continue
        parsed += 1
        output_log = append_stream_event(root, strategy=strategy, event=event, log_path=log_path)
        last_event_file = str(write_single_event_file(root, strategy=strategy, event=event))
    return {
        "parsed": parsed,
        "ignored": ignored,
        "log_path": str(output_log) if output_log else str(log_path or Path(root) / "logs" / "binance_user_stream_events.jsonl"),
        "last_event_file": last_event_file,
    }
