"""Read recent central account-state snapshots for non-confirmation gates."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.account_state import (
    balance_to_binance_row,
    load_central_account_state,
    normalize_account_row,
    parse_ts,
    read_account_state_payload,
)


DEFAULT_MAX_AGE_SECONDS = float(os.environ.get("BINANCE_ACCOUNT_STATE_CACHE_MAX_AGE_SEC", "60"))


@dataclass(frozen=True)
class CachedAccountState:
    account: str
    strategy: str
    age_seconds: float
    balance: dict[str, Any]
    positions: list[dict[str, Any]]
    assumed: bool = False
    assumption_reason: str = ""


def _env_enabled(name: str) -> bool:
    value = os.environ.get(name, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except Exception:
        return default


def _load_stale_empty_testnet_assumption(root: str | Path, strategy: str) -> CachedAccountState | None:
    if not _env_enabled("BINANCE_ACCOUNT_STATE_ALLOW_STALE_EMPTY_TESTNET"):
        return None
    payload = read_account_state_payload(root, allow_legacy=False)
    if not payload:
        return None
    wanted = str(strategy or "").upper()
    assumed_balance = _env_float("BINANCE_ACCOUNT_STATE_TESTNET_BALANCE_USDT", 5000.0)
    if assumed_balance <= 0:
        return None
    for raw in payload.get("accounts") or []:
        if not isinstance(raw, dict):
            continue
        row = normalize_account_row(raw)
        if str(row.get("strategy") or "").upper() != wanted:
            continue
        if not row.get("stale"):
            return None
        if int(row.get("open_positions") or 0) != 0 or row.get("positions"):
            return None
        ts = parse_ts(row.get("ts"))
        age = (datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds() if ts else 0.0
        assumed_row = dict(row)
        assumed_row.update({
            "wallet_usdt": assumed_balance,
            "available_usdt": assumed_balance,
            "margin_usdt": assumed_balance,
        })
        return CachedAccountState(
            account=str(row.get("account") or ""),
            strategy=str(row.get("strategy") or strategy),
            age_seconds=max(0.0, age),
            balance=balance_to_binance_row(assumed_row),
            positions=[],
            assumed=True,
            assumption_reason="stale_empty_testnet",
        )
    return None


def load_cached_account_state(
    root: str | Path,
    strategy: str,
    *,
    max_age_seconds: float | None = None,
) -> CachedAccountState | None:
    """Return fresh central account state for risk gates, or None.

    The preferred source is `runtime/account_state_latest.json`; the old
    `account_snapshot_latest.json` remains a compatibility fallback. Order
    submit paths still need exchange proof until confirmation-state service is
    complete.
    """
    state = load_central_account_state(
        root,
        strategy,
        max_age_seconds=DEFAULT_MAX_AGE_SECONDS if max_age_seconds is None else float(max_age_seconds),
        allow_legacy=True,
    )
    if not state:
        return _load_stale_empty_testnet_assumption(root, strategy)
    return CachedAccountState(
        account=state.account,
        strategy=state.strategy,
        age_seconds=state.age_seconds,
        balance=state.balance,
        positions=state.positions,
    )
