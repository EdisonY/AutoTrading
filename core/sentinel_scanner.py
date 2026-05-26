"""Shared scanner-side helpers for sentinel candidates."""

from __future__ import annotations

from typing import Any


def filter_context_by_available(
    context: dict[str, dict[str, Any]],
    available: set[str] | list[str] | tuple[str, ...] | None,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    if not available or not context:
        return context, []
    allowed = {str(symbol).upper() for symbol in available}
    skipped = [symbol for symbol in context if symbol not in allowed]
    return {symbol: meta for symbol, meta in context.items() if symbol in allowed}, skipped


def merge_symbols_with_context(symbols: list[str], context: dict[str, dict[str, Any]]) -> list[str]:
    sentinel = list(context)
    return list(dict.fromkeys(list(symbols) + sentinel))


def fields_from_context(context: dict[str, dict[str, Any]], symbol: str) -> dict[str, Any]:
    meta = context.get(str(symbol).upper())
    if not meta:
        return {}
    return {
        "sentinel": True,
        "sentinel_reason": meta.get("reason", ""),
        "sentinel_change_pct": meta.get("change_pct"),
        "sentinel_velocity_pct": meta.get("velocity_pct"),
        "sentinel_abs_velocity_pct": meta.get("abs_velocity_pct"),
        "sentinel_quote_volume": meta.get("quote_volume"),
        "sentinel_volume_delta": meta.get("volume_delta"),
        "sentinel_last_price": meta.get("last_price"),
        "sentinel_event_id": meta.get("event_id", ""),
        "sentinel_rank": meta.get("rank"),
    }
