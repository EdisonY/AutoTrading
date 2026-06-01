"""Helpers for deriving scanner risk state from one exchange snapshot."""

from __future__ import annotations

from typing import Any, Iterable

from core.position_utils import infer_position_side, to_float


PositionRows = Iterable[dict[str, Any]]


def active_positions(rows: PositionRows | None) -> list[dict[str, Any]]:
    """Return non-zero exchange positions from a positionRisk response."""
    if not rows:
        return []
    active: list[dict[str, Any]] = []
    for row in rows:
        try:
            if abs(to_float(row.get("positionAmt"))) > 0.0001:
                active.append(row)
        except AttributeError:
            continue
    return active


def count_active_positions(rows: PositionRows | None) -> int:
    return len(active_positions(rows))


def count_side_positions(rows: PositionRows | None, side: str) -> int:
    wanted = side.lower()
    return sum(1 for row in active_positions(rows) if infer_position_side(row)[0].lower() == wanted)


def find_symbol_position(rows: PositionRows | None, symbol: str) -> dict[str, Any]:
    wanted = symbol.upper()
    for row in active_positions(rows):
        if str(row.get("symbol") or "").upper() == wanted:
            return row
    return {}


def usdt_balance_summary(balance: Any) -> tuple[float, float]:
    """Return (wallet, available) for Binance account/balance variants."""
    if isinstance(balance, dict):
        assets = balance.get("assets")
        if isinstance(assets, list):
            usdt = next((x for x in assets if x.get("asset") == "USDT"), {})
            return (
                to_float(usdt.get("walletBalance", usdt.get("balance"))),
                to_float(usdt.get("availableBalance")),
            )
        return (
            to_float(balance.get("totalWalletBalance", balance.get("balance"))),
            to_float(balance.get("availableBalance")),
        )
    if isinstance(balance, list):
        usdt = next((x for x in balance if x.get("asset") == "USDT"), {})
        return (
            to_float(usdt.get("walletBalance", usdt.get("balance", usdt.get("crossWalletBalance")))),
            to_float(usdt.get("availableBalance")),
        )
    return 0.0, 0.0
