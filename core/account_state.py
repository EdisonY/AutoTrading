"""Central account-state file helpers.

This module defines the durable runtime shape that scanners, replay checks, and
future confirmation logic can share without each process polling Binance.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ACCOUNT_STATE_SCHEMA_VERSION = 1
ACCOUNT_STATE_FILENAME = "account_state_latest.json"
LEGACY_ACCOUNT_SNAPSHOT_FILENAME = "account_snapshot_latest.json"


@dataclass(frozen=True)
class CentralAccountState:
    account: str
    strategy: str
    age_seconds: float
    balance: dict[str, Any]
    positions: list[dict[str, Any]]
    raw: dict[str, Any]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def to_float(value: Any) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def position_to_binance_row(row: dict[str, Any]) -> dict[str, Any]:
    side = str(row.get("side") or row.get("positionSide") or "").upper()
    qty = abs(to_float(row.get("qty") or row.get("positionAmt")))
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


def balance_to_binance_row(account: dict[str, Any]) -> dict[str, Any]:
    wallet = account.get("wallet_usdt", account.get("wallet", 0))
    available = account.get("available_usdt", account.get("available", 0))
    margin = account.get("margin_usdt", account.get("margin", wallet))
    return {
        "totalWalletBalance": str(wallet or 0),
        "availableBalance": str(available or 0),
        "totalMarginBalance": str(margin or wallet or 0),
        "balance": str(wallet or 0),
    }


def normalize_account_row(account: dict[str, Any]) -> dict[str, Any]:
    positions = account.get("positions") if isinstance(account.get("positions"), list) else []
    key = str(account.get("account") or account.get("key") or "")
    version = str(account.get("version") or "")
    strategy = str(account.get("strategy") or (f"{key}/{version}" if key and version else ""))
    return {
        "ts": str(account.get("ts") or account.get("snapshot_ts") or utc_now_iso()),
        "account": key,
        "strategy": strategy,
        "version": version,
        "desc": str(account.get("desc") or ""),
        "stale": bool(account.get("stale")),
        "snapshot_error": str(account.get("snapshot_error") or ""),
        "wallet_usdt": to_float(account.get("wallet_usdt", account.get("wallet"))),
        "available_usdt": to_float(account.get("available_usdt", account.get("available"))),
        "margin_usdt": to_float(account.get("margin_usdt", account.get("margin"))),
        "unrealized_pnl_usdt": to_float(account.get("unrealized_pnl_usdt", account.get("upnl"))),
        "open_positions": int(account.get("open_positions") if account.get("open_positions") is not None else len(positions)),
        "longs": int(account.get("longs") or 0),
        "shorts": int(account.get("shorts") or 0),
        "notional_usdt": to_float(account.get("notional_usdt", account.get("notional"))),
        "used_margin_usdt": to_float(account.get("used_margin_usdt", account.get("used_margin"))),
        "hard_stop_risk_count": int(account.get("hard_stop_risk_count", account.get("over_hard") or 0)),
        "positions": [pos for pos in positions if isinstance(pos, dict)],
    }


def build_account_state_payload(
    accounts: list[dict[str, Any]],
    *,
    status: str = "ok",
    source: str = "",
    errors: list[str] | None = None,
) -> dict[str, Any]:
    rows = [normalize_account_row(row) for row in accounts]
    stale = [row["account"] for row in rows if row.get("stale")]
    return {
        "schema_version": ACCOUNT_STATE_SCHEMA_VERSION,
        "generated_at": utc_now_iso(),
        "status": status,
        "source": source,
        "errors": list(errors or []),
        "summary": {
            "accounts": len(rows),
            "fresh_accounts": len(rows) - len(stale),
            "stale_accounts": stale,
            "wallet_usdt": round(sum(float(row["wallet_usdt"]) for row in rows), 4),
            "available_usdt": round(sum(float(row["available_usdt"]) for row in rows), 4),
            "margin_usdt": round(sum(float(row["margin_usdt"]) for row in rows), 4),
            "unrealized_pnl_usdt": round(sum(float(row["unrealized_pnl_usdt"]) for row in rows), 4),
            "open_positions": sum(int(row["open_positions"]) for row in rows),
            "partial_error_count": len(errors or []),
        },
        "accounts": rows,
    }


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_raw = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
            handle.write("\n")
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def write_account_state(root: str | Path, payload: dict[str, Any]) -> Path:
    path = Path(root) / "runtime" / ACCOUNT_STATE_FILENAME
    atomic_write_json(path, payload)
    return path


def read_account_state_payload(root: str | Path, *, allow_legacy: bool = True) -> dict[str, Any] | None:
    runtime = Path(root) / "runtime"
    for name in ([ACCOUNT_STATE_FILENAME] + ([LEGACY_ACCOUNT_SNAPSHOT_FILENAME] if allow_legacy else [])):
        try:
            payload = json.loads((runtime / name).read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("accounts"), list):
            return payload
    return None


def load_central_account_state(
    root: str | Path,
    strategy: str,
    *,
    max_age_seconds: float,
    min_observed_at: datetime | None = None,
    allow_legacy: bool = True,
) -> CentralAccountState | None:
    payload = read_account_state_payload(root, allow_legacy=allow_legacy)
    if not payload:
        return None
    wanted = strategy.upper()
    for raw in payload.get("accounts") or []:
        if not isinstance(raw, dict):
            continue
        row = normalize_account_row(raw)
        if str(row.get("strategy") or "").upper() != wanted:
            continue
        if row.get("stale"):
            return None
        ts = parse_ts(row.get("ts"))
        if not ts:
            return None
        observed_at = ts.astimezone(timezone.utc)
        if min_observed_at is not None:
            required = min_observed_at if min_observed_at.tzinfo else min_observed_at.replace(tzinfo=timezone.utc)
            if observed_at < required.astimezone(timezone.utc):
                return None
        age = (datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds()
        if age < 0 or age > float(max_age_seconds):
            return None
        return CentralAccountState(
            account=str(row.get("account") or ""),
            strategy=str(row.get("strategy") or strategy),
            age_seconds=age,
            balance=balance_to_binance_row(row),
            positions=[
                position_to_binance_row(pos)
                for pos in row.get("positions") or []
                if isinstance(pos, dict) and str(pos.get("symbol") or "")
            ],
            raw=row,
        )
    return None
