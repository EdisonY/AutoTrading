"""Read recent account-state snapshots for non-confirmation risk gates."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_MAX_AGE_SECONDS = float(os.environ.get("BINANCE_ACCOUNT_STATE_CACHE_MAX_AGE_SEC", "60"))


@dataclass(frozen=True)
class CachedAccountState:
    account: str
    strategy: str
    age_seconds: float
    balance: dict[str, Any]
    positions: list[dict[str, Any]]


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _to_float(value: Any) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _position_to_binance_row(row: dict[str, Any]) -> dict[str, Any]:
    side = str(row.get("side") or row.get("positionSide") or "").upper()
    qty = abs(_to_float(row.get("qty") or row.get("positionAmt")))
    signed_qty = -qty if side == "SHORT" else qty
    return {
        "symbol": str(row.get("symbol") or "").upper(),
        "positionAmt": str(signed_qty),
        "positionSide": side or ("SHORT" if signed_qty < 0 else "LONG"),
        "entryPrice": str(row.get("entry") or row.get("entryPrice") or 0),
        "markPrice": str(row.get("mark") or row.get("markPrice") or 0),
        "unrealizedProfit": str(row.get("upnl") or row.get("unrealizedProfit") or 0),
        "notional": str(row.get("notional") or row.get("notionalValue") or 0),
        "notionalValue": str(row.get("notional") or row.get("notionalValue") or 0),
        "leverage": str(row.get("lev") or row.get("leverage") or 0),
    }


def load_cached_account_state(
    root: str | Path,
    strategy: str,
    *,
    max_age_seconds: float | None = None,
) -> CachedAccountState | None:
    """Return a fresh account snapshot for risk gates, or None.

    This is intentionally not used for order/open/close confirmation. Those
    paths still need fresh exchange proof.
    """
    path = Path(root) / "runtime" / "account_snapshot_latest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    accounts = payload.get("accounts")
    if not isinstance(accounts, list):
        return None
    wanted = strategy.upper()
    for account in accounts:
        if not isinstance(account, dict):
            continue
        if str(account.get("strategy") or "").upper() != wanted:
            continue
        if account.get("stale"):
            return None
        ts = _parse_ts(account.get("ts"))
        if not ts:
            return None
        age = (datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds()
        max_age = DEFAULT_MAX_AGE_SECONDS if max_age_seconds is None else float(max_age_seconds)
        if age < 0 or age > max_age:
            return None
        positions = [
            _position_to_binance_row(pos)
            for pos in account.get("positions") or []
            if isinstance(pos, dict) and str(pos.get("symbol") or "")
        ]
        balance = {
            "totalWalletBalance": str(account.get("wallet_usdt") or 0),
            "availableBalance": str(account.get("available_usdt") or 0),
            "balance": str(account.get("wallet_usdt") or 0),
        }
        return CachedAccountState(
            account=str(account.get("account") or ""),
            strategy=str(account.get("strategy") or strategy),
            age_seconds=age,
            balance=balance,
            positions=positions,
        )
    return None
