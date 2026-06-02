"""Read recent central account-state snapshots for non-confirmation gates."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.account_state import load_central_account_state


DEFAULT_MAX_AGE_SECONDS = float(os.environ.get("BINANCE_ACCOUNT_STATE_CACHE_MAX_AGE_SEC", "60"))


@dataclass(frozen=True)
class CachedAccountState:
    account: str
    strategy: str
    age_seconds: float
    balance: dict[str, Any]
    positions: list[dict[str, Any]]


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
        return None
    return CachedAccountState(
        account=state.account,
        strategy=state.strategy,
        age_seconds=state.age_seconds,
        balance=state.balance,
        positions=state.positions,
    )
