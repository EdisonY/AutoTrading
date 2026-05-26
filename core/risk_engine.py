"""Unified entry risk checks shared by scanners."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class RiskLimits:
    max_total_positions: int
    max_positions_per_side: int
    min_available_balance_pct: float
    min_available_balance_usdt: float


@dataclass(slots=True)
class RiskDecision:
    allowed: bool
    category: str = "allowed"
    reason: str = ""
    total_positions: int = 0
    side_positions: int = 0
    total_balance: float = 0.0
    available_balance: float = 0.0
    reserve_required: float = 0.0


def parse_usdt_balance(balance: Any) -> tuple[float, float]:
    """Return (total, available) for Binance account/balance variants."""
    if isinstance(balance, dict):
        assets = balance.get("assets")
        if isinstance(assets, list):
            usdt = next((x for x in assets if x.get("asset") == "USDT"), {})
            return (
                float(usdt.get("walletBalance") or usdt.get("balance") or 0),
                float(usdt.get("availableBalance") or 0),
            )
        return (
            float(balance.get("totalWalletBalance") or balance.get("balance") or 0),
            float(balance.get("availableBalance") or 0),
        )
    if isinstance(balance, list):
        usdt = next((x for x in balance if x.get("asset") == "USDT"), {})
        return (
            float(usdt.get("walletBalance") or usdt.get("balance") or usdt.get("crossWalletBalance") or 0),
            float(usdt.get("availableBalance") or 0),
        )
    return 0.0, 0.0


class RiskEngine:
    def __init__(self, limits: RiskLimits):
        self.limits = limits

    def check_entry(
        self,
        *,
        total_positions: int,
        side_positions: int,
        balance: Any,
        risk_usdt: float,
    ) -> RiskDecision:
        total, available = parse_usdt_balance(balance)
        reserve = max(self.limits.min_available_balance_usdt, total * self.limits.min_available_balance_pct)
        if total_positions >= self.limits.max_total_positions:
            return RiskDecision(
                False,
                "position_limit",
                f"总持仓{total_positions}>={self.limits.max_total_positions}",
                total_positions,
                side_positions,
                total,
                available,
                reserve,
            )
        if side_positions >= self.limits.max_positions_per_side:
            return RiskDecision(
                False,
                "side_limit",
                f"方向持仓{side_positions}>={self.limits.max_positions_per_side}",
                total_positions,
                side_positions,
                total,
                available,
                reserve,
            )
        if available < risk_usdt + reserve:
            return RiskDecision(
                False,
                "capital_guard",
                f"可用余额保护 available={available:.2f}, reserve={reserve:.2f}",
                total_positions,
                side_positions,
                total,
                available,
                reserve,
            )
        return RiskDecision(True, "allowed", "", total_positions, side_positions, total, available, reserve)

