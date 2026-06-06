"""Create clearly labeled paper sample opens from existing local data.

This tool never calls Binance or any external API. It is used when the system is
running in paper mode and natural strategy gates have not produced enough OPEN
rows for same-day report/backtest plumbing.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.event_store import EventStoreWriter

CST = timezone(timedelta(hours=8))
STRATEGIES = ("A/v11", "B/v16", "C/v14")


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def payload(row: sqlite3.Row) -> dict[str, Any]:
    try:
        data = json.loads(row["payload_json"] or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def side_from_candidate(row: sqlite3.Row, data: dict[str, Any]) -> str:
    side = str(data.get("side") or row["side"] or "").lower()
    if side in {"long", "short"}:
        return side
    score = data.get("score", row["score"])
    try:
        return "short" if float(score or 0) < 0 else "long"
    except Exception:
        return "long"


def score_from_candidate(row: sqlite3.Row, data: dict[str, Any]) -> float:
    for key in ("score", "raw_score", "net_score"):
        if data.get(key) not in (None, ""):
            try:
                return abs(float(data[key]))
            except Exception:
                pass
    try:
        return abs(float(row["score"] or 0))
    except Exception:
        return 0.0


def market_candidates(runtime_dir: Path) -> list[dict[str, Any]]:
    cache = read_json(runtime_dir / "market_data_cache.json")
    rows = cache.get("top_preview")
    if isinstance(rows, list) and rows:
        return [row for row in rows if isinstance(row, dict)]
    symbols = cache.get("top_symbols") or cache.get("available_symbols") or []
    return [{"symbol": sym, "change_pct": 0.0, "quote_volume": 0.0} for sym in symbols if sym]


def default_price(symbol: str, market_row: dict[str, Any] | None = None) -> float:
    if market_row:
        for key in ("close", "last", "price"):
            try:
                value = float(market_row.get(key) or 0)
            except Exception:
                value = 0.0
            if value > 0:
                return value
    if symbol.startswith("BTC"):
        return 100000.0
    if symbol.startswith("ETH"):
        return 3000.0
    if symbol.startswith("BNB"):
        return 1000.0
    if symbol.startswith("SOL"):
        return 200.0
    return 1.0


def existing_symbols(conn: sqlite3.Connection, strategy: str, since: str) -> set[str]:
    return {
        str(row["symbol"])
        for row in conn.execute(
            """
            select symbol from events
            where strategy=? and event_type='OPEN' and ts>=?
              and (source like '%paper%' or payload_json like '%"paper"%'
                   or payload_json like '%paper_sample%')
            """,
            (strategy, since),
        )
        if row["symbol"]
    }


def recent_signal_candidates(
    conn: sqlite3.Connection,
    strategy: str,
    since: str,
    already_open: set[str],
    limit: int,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in conn.execute(
        """
        select id, ts, strategy, event_type, symbol, side, score, reason, payload_json
        from events
        where strategy=? and ts>=? and event_type in ('SIGNAL','OPEN_SKIPPED')
          and symbol<>''
        order by id desc
        limit 200
        """,
        (strategy, since),
    ):
        data = payload(row)
        symbol = str(row["symbol"] or data.get("symbol") or "").upper()
        if not symbol or symbol in seen or symbol in already_open:
            continue
        seen.add(symbol)
        price = data.get("price") or data.get("entry_price")
        try:
            price_value = float(price or 0)
        except Exception:
            price_value = 0.0
        candidates.append(
            {
                "symbol": symbol,
                "side": side_from_candidate(row, data),
                "score": score_from_candidate(row, data),
                "price": price_value if price_value > 0 else default_price(symbol),
                "timeframe": str(data.get("timeframe") or "paper"),
                "reason": str(data.get("reason") or row["reason"] or "recent_strategy_candidate"),
                "candidate_event_id": row["id"],
                "candidate_event_type": row["event_type"],
            }
        )
        if len(candidates) >= limit:
            break
    return candidates


def fallback_candidates(
    strategy: str,
    runtime_dir: Path,
    already_open: set[str],
    limit: int,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    rows = market_candidates(runtime_dir)
    offset = {"A/v11": 0, "B/v16": 1, "C/v14": 2}.get(strategy, 0)
    for row in rows[offset:] + rows[:offset]:
        symbol = str(row.get("symbol") or "").upper()
        if not symbol or symbol in already_open:
            continue
        try:
            change = float(row.get("change_pct") or 0.0)
        except Exception:
            change = 0.0
        side = "long" if change >= 0 else "short"
        if strategy == "A/v11":
            score = 100.0 + min(abs(change), 30.0)
        elif strategy == "B/v16":
            score = 40.0 + min(abs(change), 30.0)
        else:
            score = 25.0 + min(abs(change), 30.0)
        out.append(
            {
                "symbol": symbol,
                "side": side,
                "score": score,
                "price": default_price(symbol, row),
                "timeframe": "paper_sample",
                "reason": "Top100行情采样：用于模拟盘数据骨架，不代表真实策略放宽",
                "candidate_event_id": None,
                "candidate_event_type": "MARKET_CACHE",
            }
        )
        if len(out) >= limit:
            break
    return out


def build_open_event(strategy: str, item: dict[str, Any], now: datetime, risk_usdt: float, leverage: int) -> dict[str, Any]:
    price = float(item["price"] or default_price(str(item["symbol"])))
    qty = round((risk_usdt * leverage) / price, 8) if price > 0 else 0.0
    side = str(item["side"])
    if side == "long":
        stop_loss = round(price * 0.98, 8)
        take_profit = round(price * 1.04, 8)
    else:
        stop_loss = round(price * 1.02, 8)
        take_profit = round(price * 0.96, 8)
    return {
        "time": now.isoformat(),
        "event": "OPEN",
        "strategy": strategy,
        "symbol": item["symbol"],
        "side": side,
        "price": price,
        "qty": qty,
        "exchange_qty": qty,
        "leverage": leverage,
        "sl": stop_loss,
        "tp": take_profit,
        "score": float(item.get("score") or 0.0),
        "timeframe": item.get("timeframe") or "paper_sample",
        "reason": "paper_sample_open",
        "entry_reason": item.get("reason") or "paper_sample_open",
        "category": "opened",
        "decision_stage": "open",
        "filter_layer": "paper_sample",
        "order_id": f"PAPER-SAMPLE-{strategy.replace('/', '-')}-{item['symbol']}-{int(now.timestamp())}",
        "paper": True,
        "mode": "paper",
        "simulation_only": True,
        "paper_sample": True,
        "paper_sample_policy": "first_version_data_skeleton_v1",
        "target_margin_usdt": risk_usdt,
        "expected_notional_usdt": round(qty * price, 6),
        "source_candidate_event_id": item.get("candidate_event_id"),
        "source_candidate_event_type": item.get("candidate_event_type"),
        "warning": "模拟采样开仓：用于report/backtest数据链路，不代表真实交易所成交，也不代表真实策略门已放宽。",
    }


def run(root: Path, *, per_strategy: int, since_hours: int, risk_usdt: float, leverage: int, dry_run: bool = False) -> dict[str, Any]:
    runtime_dir = root / "runtime"
    db_path = runtime_dir / "event_store.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    since = (datetime.now(CST) - timedelta(hours=since_hours)).isoformat()
    now = datetime.now(CST)
    events: list[dict[str, Any]] = []
    by_strategy: dict[str, int] = {}
    for strategy in STRATEGIES:
        already = existing_symbols(conn, strategy, since)
        candidates = recent_signal_candidates(conn, strategy, since, already, per_strategy)
        if len(candidates) < per_strategy:
            candidates.extend(
                fallback_candidates(strategy, runtime_dir, already | {c["symbol"] for c in candidates}, per_strategy - len(candidates))
            )
        selected = candidates[:per_strategy]
        by_strategy[strategy] = len(selected)
        events.extend(build_open_event(strategy, item, now, risk_usdt, leverage) for item in selected)
    conn.close()
    if not dry_run and events:
        writer = EventStoreWriter(db_path)
        for event in events:
            writer.write_event(event, source=f"{event['strategy']}/paper_sample")
        writer.close()
    return {
        "ts": now.isoformat(),
        "dry_run": dry_run,
        "db": str(db_path),
        "per_strategy": per_strategy,
        "created": 0 if dry_run else len(events),
        "planned": len(events),
        "by_strategy": by_strategy,
        "events": events,
        "safety": "no_binance_no_external_network_no_real_orders",
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create labeled paper sample OPEN rows without external requests")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--per-strategy", type=int, default=int(os.environ.get("PAPER_SAMPLE_OPENS_PER_STRATEGY", "2")))
    parser.add_argument("--since-hours", type=int, default=24)
    parser.add_argument("--risk-usdt", type=float, default=float(os.environ.get("PAPER_SAMPLE_RISK_USDT", "100")))
    parser.add_argument("--leverage", type=int, default=int(os.environ.get("PAPER_SAMPLE_LEVERAGE", "4")))
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = run(
        args.root,
        per_strategy=max(0, args.per_strategy),
        since_hours=max(1, args.since_hours),
        risk_usdt=max(1.0, args.risk_usdt),
        leverage=max(1, args.leverage),
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
