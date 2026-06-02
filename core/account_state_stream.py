"""User-data-stream reducers for central account state.

The websocket/listen-key transport can stay outside this module. This reducer
turns Binance Futures `ACCOUNT_UPDATE` payloads into the same account rows that
scanners and execution confirmation already consume.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from core.account_state import build_account_state_payload, normalize_account_row, to_float


def _event_ts(event: dict[str, Any]) -> str:
    raw = event.get("E") or event.get("T") or event.get("event_time")
    try:
        value = float(raw)
        if value > 10_000_000_000:
            value = value / 1000
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()


def _position_side(row: dict[str, Any]) -> str:
    raw_side = str(row.get("ps") or row.get("positionSide") or "").upper()
    if raw_side in {"LONG", "SHORT"}:
        return raw_side
    amt = to_float(row.get("pa") if row.get("pa") is not None else row.get("positionAmt"))
    return "SHORT" if amt < 0 else "LONG"


def _stream_position_row(row: dict[str, Any]) -> dict[str, Any]:
    side = _position_side(row)
    qty = abs(to_float(row.get("pa") if row.get("pa") is not None else row.get("positionAmt")))
    entry = to_float(row.get("ep") if row.get("ep") is not None else row.get("entryPrice"))
    upnl = to_float(row.get("up") if row.get("up") is not None else row.get("unrealizedProfit"))
    notional = to_float(row.get("notional") if row.get("notional") is not None else row.get("notionalValue"))
    return {
        "symbol": str(row.get("s") or row.get("symbol") or "").upper(),
        "side": side,
        "qty": qty,
        "entry": entry,
        "mark": to_float(row.get("mark") or row.get("markPrice")),
        "upnl": upnl,
        "notional": abs(notional),
        "lev": to_float(row.get("leverage")),
        "raw_position_side": str(row.get("ps") or row.get("positionSide") or ""),
    }


def _merge_positions(existing: list[dict[str, Any]], updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for item in existing:
        if not isinstance(item, dict):
            continue
        key = (str(item.get("symbol") or "").upper(), str(item.get("side") or "").upper())
        if key[0] and key[1]:
            merged[key] = dict(item)
    for update in updates:
        if not update.get("symbol"):
            continue
        key = (str(update.get("symbol") or "").upper(), str(update.get("side") or "").upper())
        if abs(float(update.get("qty") or 0)) <= 0.00000001:
            merged.pop(key, None)
        else:
            merged[key] = update
    return sorted(merged.values(), key=lambda row: (str(row.get("symbol") or ""), str(row.get("side") or "")))


def apply_account_update_to_row(account: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    """Return an updated normalized account row for one Binance ACCOUNT_UPDATE."""
    row = normalize_account_row(account)
    body = event.get("a") if isinstance(event.get("a"), dict) else event
    ts = _event_ts(event)

    balances = body.get("B") if isinstance(body.get("B"), list) else []
    for balance in balances:
        if str(balance.get("a") or balance.get("asset") or "").upper() != "USDT":
            continue
        wallet = balance.get("wb") if balance.get("wb") is not None else balance.get("walletBalance")
        available = balance.get("cw") if balance.get("cw") is not None else balance.get("availableBalance")
        if wallet is not None:
            row["wallet_usdt"] = to_float(wallet)
            row["margin_usdt"] = to_float(wallet)
        if available is not None:
            row["available_usdt"] = to_float(available)

    position_updates = [
        _stream_position_row(item)
        for item in (body.get("P") if isinstance(body.get("P"), list) else [])
        if isinstance(item, dict)
    ]
    row["positions"] = _merge_positions(row.get("positions") or [], position_updates)
    row["open_positions"] = len(row["positions"])
    row["longs"] = sum(1 for item in row["positions"] if str(item.get("side") or "").upper() == "LONG")
    row["shorts"] = sum(1 for item in row["positions"] if str(item.get("side") or "").upper() == "SHORT")
    row["unrealized_pnl_usdt"] = round(sum(to_float(item.get("upnl")) for item in row["positions"]), 8)
    row["notional_usdt"] = round(sum(abs(to_float(item.get("notional"))) for item in row["positions"]), 8)
    row["used_margin_usdt"] = round(
        sum(abs(to_float(item.get("notional"))) / max(1.0, to_float(item.get("lev"))) for item in row["positions"]),
        8,
    )
    row["ts"] = ts
    row["stale"] = False
    row["snapshot_error"] = ""
    return row


def apply_user_stream_event(
    payload: dict[str, Any],
    *,
    strategy: str,
    event: dict[str, Any],
    source: str = "user_data_stream",
) -> dict[str, Any]:
    """Apply one user-data-stream event to a central account-state payload."""
    if str(event.get("e") or event.get("event_type") or "").upper() not in {"ACCOUNT_UPDATE", ""}:
        return deepcopy(payload)

    accounts = []
    wanted = strategy.upper()
    updated = False
    for account in payload.get("accounts") or []:
        if not isinstance(account, dict):
            continue
        if str(account.get("strategy") or "").upper() == wanted:
            accounts.append(apply_account_update_to_row(account, event))
            updated = True
        else:
            accounts.append(deepcopy(account))
    if not updated:
        raise KeyError(f"strategy not found in account state: {strategy}")
    return build_account_state_payload(accounts, status="ok", source=source, errors=[])
