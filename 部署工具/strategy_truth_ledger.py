"""Strategy Truth Ledger - separate active strategy PnL from recovery positions.

Reads from SQLite event_store.sqlite3 and account_snapshots to produce:
- runtime/strategy_truth_latest.json
- reports/strategy_truth_latest.md
- Optional SQLite tables for aggregation

Run on Aliyun analysis node after syncing data from Tencent.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

CST = timezone(timedelta(hours=8))

STRATEGY_MAP = {
    "A/v11": {"account": "A", "name": "半木夏"},
    "B/v16": {"account": "B", "name": "订单流"},
    "C/v14": {"account": "C", "name": "四维度"},
}

FEE_RATE_TAKER = 0.0005  # 0.05% taker fee per side


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST)
        return dt.astimezone(CST)
    except Exception:
        return None


def safe_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val) if val is not None else default
    except (ValueError, TypeError):
        return default


def load_open_events(con: sqlite3.Connection, days: int = 30) -> list[dict[str, Any]]:
    """Load OPEN events from the events table."""
    cutoff = (datetime.now(CST) - timedelta(days=days)).isoformat()
    rows = con.execute(
        """SELECT id, ts, strategy, symbol, side, score, payload_json
           FROM events
           WHERE event_type = 'OPEN' AND ts >= ?
           ORDER BY ts""",
        (cutoff,),
    ).fetchall()
    events = []
    for row in rows:
        payload = json.loads(row[6]) if row[6] else {}
        events.append({
            "id": row[0],
            "ts": row[1],
            "strategy": row[2],
            "symbol": row[3],
            "side": row[4],
            "score": safe_float(row[5]),
            "entry_price": safe_float(payload.get("price")),
            "leverage": safe_float(payload.get("leverage"), 4.0),
            "atr": safe_float(payload.get("atr")),
            "reasons": payload.get("reasons", ""),
        })
    return events


def load_close_events(con: sqlite3.Connection, days: int = 30) -> list[dict[str, Any]]:
    """Load CLOSE and FORCED_CLOSE events."""
    cutoff = (datetime.now(CST) - timedelta(days=days)).isoformat()
    rows = con.execute(
        """SELECT id, ts, strategy, symbol, event_type, side, payload_json
           FROM events
           WHERE event_type IN ('CLOSE', 'FORCED_CLOSE') AND ts >= ?
           ORDER BY ts""",
        (cutoff,),
    ).fetchall()
    events = []
    for row in rows:
        payload = json.loads(row[6]) if row[6] else {}
        events.append({
            "id": row[0],
            "ts": row[1],
            "strategy": row[2],
            "symbol": row[3],
            "event_type": row[4],
            "side": row[5],
            "exit_price": safe_float(payload.get("exit_price")),
            "pnl_usd": safe_float(payload.get("pnl_usd")),
            "pnl_pct": safe_float(payload.get("pnl_pct")),
            "reason": payload.get("reason", ""),
            "entry_price": safe_float(payload.get("entry_price")),
            "entry_time": payload.get("entry_time", ""),
        })
    return events


def load_latest_snapshots(con: sqlite3.Connection) -> list[dict[str, Any]]:
    """Load the latest account snapshots with positions."""
    rows = con.execute(
        """SELECT id, ts, account, wallet_usdt, margin_usdt, available_usdt,
                  unrealized_pnl_usdt, open_positions, payload_json
           FROM account_snapshots
           ORDER BY id DESC LIMIT 30"""
    ).fetchall()
    # Group by account, take latest per account
    by_account: dict[str, dict] = {}
    for row in rows:
        acct = row[2]
        if acct in by_account:
            continue
        payload = json.loads(row[8]) if row[8] else {}
        positions = payload.get("positions", [])
        by_account[acct] = {
            "ts": row[1],
            "account": acct,
            "wallet_usdt": safe_float(row[3]),
            "margin_usdt": safe_float(row[4]),
            "available_usdt": safe_float(row[5]),
            "unrealized_pnl_usdt": safe_float(row[6]),
            "open_positions": int(row[7] or 0),
            "positions": positions,
        }
    return list(by_account.values())


def match_trades(
    open_events: list[dict],
    close_events: list[dict],
) -> list[dict[str, Any]]:
    """Match OPEN events with CLOSE events to create trade records."""
    # Index close events by (strategy, symbol, side)
    close_index: dict[tuple, list[dict]] = {}
    for ce in close_events:
        key = (ce["strategy"], ce["symbol"], ce["side"])
        close_index.setdefault(key, []).append(ce)

    trades = []
    for oe in open_events:
        key = (oe["strategy"], oe["symbol"], oe["side"])
        closes = close_index.get(key, [])

        # Find the earliest close after this open
        matched_close = None
        for ce in closes:
            if ce["ts"] >= oe["ts"]:
                matched_close = ce
                break

        entry_dt = parse_dt(oe["ts"])
        if matched_close:
            exit_dt = parse_dt(matched_close["ts"])
            holding_minutes = (exit_dt - entry_dt).total_seconds() / 60 if entry_dt and exit_dt else 0
            pnl = safe_float(matched_close["pnl_usd"])
            # Estimate fee: entry + exit, based on notional
            entry_price = oe["entry_price"]
            notional = 100.0 * oe["leverage"]  # approximate
            fee = notional * FEE_RATE_TAKER * 2 if entry_price else 0

            trades.append({
                "strategy": oe["strategy"],
                "symbol": oe["symbol"],
                "side": oe["side"],
                "entry_time": oe["ts"],
                "exit_time": matched_close["ts"],
                "holding_minutes": round(holding_minutes, 1),
                "entry_price": entry_price,
                "exit_price": matched_close["exit_price"],
                "score": oe["score"],
                "pnl_usd": pnl,
                "pnl_pct": safe_float(matched_close["pnl_pct"]),
                "fee_estimate": round(fee, 2),
                "net_pnl": round(pnl - fee, 2),
                "close_reason": matched_close["reason"],
                "close_type": matched_close["event_type"],
                "is_active_trade": True,
                "is_recovery": False,
                "is_open": False,
            })
        else:
            # Still open
            trades.append({
                "strategy": oe["strategy"],
                "symbol": oe["symbol"],
                "side": oe["side"],
                "entry_time": oe["ts"],
                "exit_time": None,
                "holding_minutes": None,
                "entry_price": oe["entry_price"],
                "exit_price": None,
                "score": oe["score"],
                "pnl_usd": None,
                "pnl_pct": None,
                "fee_estimate": 0,
                "net_pnl": None,
                "close_reason": None,
                "close_type": None,
                "is_active_trade": True,
                "is_recovery": False,
                "is_open": True,
            })

    return trades


def identify_recovery_positions(
    snapshots: list[dict],
    open_events: list[dict],
) -> list[dict[str, Any]]:
    """Identify positions in snapshots that have no matching OPEN event."""
    # Build set of (strategy, symbol, side) from recent OPEN events
    open_keys = set()
    for oe in open_events:
        open_keys.add((oe["strategy"], oe["symbol"], oe["side"]))

    # Also map account -> strategy
    account_to_strategy = {v["account"]: k for k, v in STRATEGY_MAP.items()}

    recovery = []
    for snap in snapshots:
        acct = snap["account"]
        strategy = account_to_strategy.get(acct, acct)
        for pos in snap["positions"]:
            sym = pos.get("symbol", "")
            side = pos.get("side", "").lower()
            key = (strategy, sym, side)
            if key not in open_keys:
                recovery.append({
                    "strategy": strategy,
                    "symbol": sym,
                    "side": side,
                    "account": acct,
                    "entry_price": safe_float(pos.get("entry")),
                    "mark_price": safe_float(pos.get("mark")),
                    "qty": safe_float(pos.get("qty")),
                    "leverage": safe_float(pos.get("lev"), 4.0),
                    "notional": safe_float(pos.get("notional")),
                    "margin": safe_float(pos.get("margin")),
                    "unrealized_pnl": safe_float(pos.get("upnl")),
                    "snapshot_ts": snap["ts"],
                    "is_active_trade": False,
                    "is_recovery": True,
                    "is_open": True,
                })
    return recovery


def evaluate_recovery_exit_policies(recovery: list[dict]) -> dict[str, Any]:
    """Shadow-test candidate exit policies for recovery positions."""
    now = datetime.now(CST)
    policies = {
        "age_4h": {"label": "4小时时间退出", "would_exit": 0, "would_hold": 0},
        "age_8h": {"label": "8小时时间退出", "would_exit": 0, "would_hold": 0},
        "age_24h": {"label": "24小时时间退出", "would_exit": 0, "would_hold": 0},
        "trailing_2pct": {"label": "2%回撤退出", "would_exit": 0, "would_hold": 0},
        "opposite_signal": {"label": "反向信号退出", "would_exit": 0, "would_hold": 0},
    }
    for pos in recovery:
        snap_dt = parse_dt(pos.get("snapshot_ts"))
        age_hours = (now - snap_dt).total_seconds() / 3600 if snap_dt else 0
        upnl = pos.get("unrealized_pnl", 0)
        margin = pos.get("margin", 0)
        upnl_pct = (upnl / margin * 100) if margin > 0 else 0

        # Age-based exits
        if age_hours >= 4:
            policies["age_4h"]["would_exit"] += 1
        else:
            policies["age_4h"]["would_hold"] += 1
        if age_hours >= 8:
            policies["age_8h"]["would_exit"] += 1
        else:
            policies["age_8h"]["would_hold"] += 1
        if age_hours >= 24:
            policies["age_24h"]["would_exit"] += 1
        else:
            policies["age_24h"]["would_hold"] += 1

        # Trailing stop after adoption (2% drawdown from entry)
        if upnl_pct < -2:
            policies["trailing_2pct"]["would_exit"] += 1
        else:
            policies["trailing_2pct"]["would_hold"] += 1

        # Opposite signal (placeholder - would need strategy signal data)
        policies["opposite_signal"]["would_hold"] += 1

    return policies


def compute_strategy_stats(trades: list[dict]) -> dict[str, dict]:
    """Compute per-strategy statistics."""
    stats: dict[str, dict] = {}
    for strategy in STRATEGY_MAP:
        strats = [t for t in trades if t["strategy"] == strategy]
        closed = [t for t in strats if not t["is_open"] and t["pnl_usd"] is not None]
        active = [t for t in strats if t["is_open"]]
        wins = [t for t in closed if t["pnl_usd"] > 0]
        losses = [t for t in closed if t["pnl_usd"] <= 0]

        total_pnl = sum(t["pnl_usd"] for t in closed)
        total_fee = sum(t["fee_estimate"] for t in closed)
        net_pnl = total_pnl - total_fee
        avg_win = sum(t["pnl_usd"] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t["pnl_usd"] for t in losses) / len(losses) if losses else 0
        payoff = abs(avg_win / avg_loss) if avg_loss != 0 else 0
        gross_profit = sum(t["pnl_usd"] for t in wins)
        gross_loss = abs(sum(t["pnl_usd"] for t in losses))
        pf = gross_profit / gross_loss if gross_loss > 0 else float("inf") if gross_profit > 0 else 0

        stats[strategy] = {
            "strategy": strategy,
            "account": STRATEGY_MAP[strategy]["account"],
            "name": STRATEGY_MAP[strategy]["name"],
            "total_trades": len(strats),
            "closed_trades": len(closed),
            "open_trades": len(active),
            "win_count": len(wins),
            "loss_count": len(losses),
            "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0,
            "total_pnl_usd": round(total_pnl, 2),
            "total_fee_usd": round(total_fee, 2),
            "net_pnl_usd": round(net_pnl, 2),
            "avg_win_usd": round(avg_win, 2),
            "avg_loss_usd": round(avg_loss, 2),
            "payoff_ratio": round(payoff, 2),
            "profit_factor": round(pf, 2) if pf != float("inf") else "inf",
            "hard_stop_count": len([t for t in closed if "hard" in (t.get("close_reason") or "").lower()]),
        }
    return stats


def compute_daily_facts(trades: list[dict]) -> dict[str, dict]:
    """Compute daily per-strategy facts."""
    daily: dict[tuple, list[dict]] = {}
    for t in trades:
        if t["is_open"] or t["pnl_usd"] is None:
            continue
        dt = parse_dt(t["entry_time"])
        if not dt:
            continue
        day = dt.strftime("%Y-%m-%d")
        key = (t["strategy"], day)
        daily.setdefault(key, []).append(t)

    facts = {}
    for (strategy, day), day_trades in daily.items():
        wins = [t for t in day_trades if t["pnl_usd"] > 0]
        losses = [t for t in day_trades if t["pnl_usd"] <= 0]
        total_pnl = sum(t["pnl_usd"] for t in day_trades)
        total_fee = sum(t["fee_estimate"] for t in day_trades)

        facts[f"{strategy}_{day}"] = {
            "strategy": strategy,
            "date": day,
            "trades": len(day_trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(day_trades) * 100, 1) if day_trades else 0,
            "total_pnl": round(total_pnl, 2),
            "total_fee": round(total_fee, 2),
            "net_pnl": round(total_pnl - total_fee, 2),
        }
    return facts


def build_output(
    trades: list[dict],
    recovery: list[dict],
    snapshots: list[dict],
) -> dict[str, Any]:
    """Build the complete truth ledger output."""
    now = datetime.now(CST)
    strategy_stats = compute_strategy_stats(trades)
    daily_facts = compute_daily_facts(trades)

    # Recovery stats per strategy with enhanced details
    recovery_stats: dict[str, dict] = {}
    for strategy in STRATEGY_MAP:
        recs = [r for r in recovery if r["strategy"] == strategy]
        total_upnl = sum(r["unrealized_pnl"] for r in recs)
        total_margin = sum(r["margin"] for r in recs)
        recovery_stats[strategy] = {
            "count": len(recs),
            "total_unrealized_pnl": round(total_upnl, 2),
            "total_margin": round(total_margin, 2),
            "symbols": [r["symbol"] for r in recs],
            "positions": [
                {
                    "symbol": r["symbol"],
                    "side": r["side"],
                    "entry_price": r["entry_price"],
                    "mark_price": r["mark_price"],
                    "unrealized_pnl": round(r["unrealized_pnl"], 2),
                    "margin": round(r["margin"], 2),
                    "leverage": r["leverage"],
                }
                for r in recs
            ],
        }

    # Recovery exit policy evaluation
    recovery_exit_policies = evaluate_recovery_exit_policies(recovery)

    # Account summary
    account_summary = []
    for snap in snapshots:
        account_summary.append({
            "account": snap["account"],
            "wallet_usdt": round(snap["wallet_usdt"], 2),
            "unrealized_pnl_usdt": round(snap["unrealized_pnl_usdt"], 2),
            "open_positions": snap["open_positions"],
        })

    # Overall
    all_active = [t for t in trades if not t["is_open"] and t["pnl_usd"] is not None]
    total_active_pnl = sum(t["pnl_usd"] for t in all_active)
    total_recovery_upnl = sum(r["unrealized_pnl"] for r in recovery)

    return {
        "generated_at": now.isoformat(),
        "summary": {
            "total_active_trades": len([t for t in trades if t["is_active_trade"]]),
            "total_closed_trades": len([t for t in trades if not t["is_open"] and t["pnl_usd"] is not None]),
            "total_open_trades": len([t for t in trades if t["is_open"]]),
            "total_recovery_positions": len(recovery),
            "total_active_pnl_usd": round(total_active_pnl, 2),
            "total_recovery_unrealized_pnl_usd": round(total_recovery_upnl, 2),
        },
        "strategy_stats": strategy_stats,
        "recovery_stats": recovery_stats,
        "recovery_exit_policies": recovery_exit_policies,
        "daily_facts": daily_facts,
        "account_summary": account_summary,
    }


def write_json(output: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def write_markdown(output: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 策略真相台账",
        "",
        f"- 生成时间: {output['generated_at']}",
        "",
        "## 总览",
        "",
        f"- 主动策略交易总数: {output['summary']['total_active_trades']}",
        f"- 已平仓交易: {output['summary']['total_closed_trades']}",
        f"- 当前持仓: {output['summary']['total_open_trades']}",
        f"- 恢复仓数量: {output['summary']['total_recovery_positions']}",
        f"- 主动策略累计 PnL: {output['summary']['total_active_pnl_usd']:.2f} USDT",
        f"- 恢复仓未实现 PnL: {output['summary']['total_recovery_unrealized_pnl_usd']:.2f} USDT",
        "",
        "## 各策略质量（主动策略，剔除恢复仓）",
        "",
        "| 策略 | 已平仓 | 胜率 | 净PnL | PF | 盈亏比 | 硬顶次数 | 恢复仓数 | 恢复仓浮盈 |",
        "|------|-------:|-----:|------:|---:|------:|--------:|--------:|----------:|",
    ]
    for strategy in ["A/v11", "B/v16", "C/v14"]:
        s = output["strategy_stats"].get(strategy, {})
        r = output["recovery_stats"].get(strategy, {})
        lines.append(
            f"| {strategy} | {s.get('closed_trades', 0)} | "
            f"{s.get('win_rate', 0):.1f}% | "
            f"{s.get('net_pnl_usd', 0):.2f} | "
            f"{s.get('profit_factor', 0)} | "
            f"{s.get('payoff_ratio', 0):.2f} | "
            f"{s.get('hard_stop_count', 0)} | "
            f"{r.get('count', 0)} | "
            f"{r.get('total_unrealized_pnl', 0):.2f} |"
        )

    lines.extend(["", "## 账户快照", ""])
    for acct in output.get("account_summary", []):
        lines.append(f"- **{acct['account']}**: 钱包 {acct['wallet_usdt']:.2f} USDT, 浮盈 {acct['unrealized_pnl_usdt']:.2f} USDT, {acct['open_positions']} 持仓")

    lines.extend(["", "## 每日明细", ""])
    daily = output.get("daily_facts", {})
    if daily:
        lines.append("| 策略 | 日期 | 交易数 | 胜率 | 净PnL |")
        lines.append("|------|------|-------:|-----:|------:|")
        for key in sorted(daily.keys(), reverse=True)[:30]:
            d = daily[key]
            lines.append(
                f"| {d['strategy']} | {d['date']} | {d['trades']} | "
                f"{d['win_rate']:.1f}% | {d['net_pnl']:.2f} |"
            )
    else:
        lines.append("暂无已平仓交易数据。")

    # Recovery exit policies
    policies = output.get("recovery_exit_policies", {})
    if policies:
        lines.extend(["", "## 恢复仓退出策略 Shadow 测试", ""])
        lines.append("| 退出策略 | 会退出 | 会持有 |")
        lines.append("|----------|-------:|-------:|")
        for key, pol in policies.items():
            lines.append(f"| {pol['label']} | {pol['would_exit']} | {pol['would_hold']} |")
        lines.append("")
        lines.append("注：以上为 shadow 评估，不自动执行。朴素时间退出可能截断大赢家，需结合证据决定。")

    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Strategy Truth Ledger")
    parser.add_argument("--db", default=None, help="Path to event_store.sqlite3")
    parser.add_argument("--runtime-dir", default=None, help="Runtime output directory")
    parser.add_argument("--reports-dir", default=None, help="Reports output directory")
    parser.add_argument("--days", type=int, default=30, help="Lookback days")
    args = parser.parse_args(argv)

    script_dir = Path(__file__).resolve().parent
    root = script_dir.parent if script_dir.name == "部署工具" else script_dir

    db_path = Path(args.db) if args.db else root / "runtime" / "event_store.sqlite3"
    runtime_dir = Path(args.runtime_dir) if args.runtime_dir else root / "runtime"
    reports_dir = Path(args.reports_dir) if args.reports_dir else root / "reports"

    if not db_path.exists():
        print(f"ERROR: Database not found: {db_path}")
        return 1

    con = sqlite3.connect(str(db_path))
    try:
        print(f"Loading data from {db_path} (last {args.days} days)...")
        open_events = load_open_events(con, days=args.days)
        close_events = load_close_events(con, days=args.days)
        snapshots = load_latest_snapshots(con)

        print(f"  OPEN events: {len(open_events)}")
        print(f"  CLOSE events: {len(close_events)}")
        print(f"  Snapshots: {len(snapshots)} accounts")

        trades = match_trades(open_events, close_events)
        recovery = identify_recovery_positions(snapshots, open_events)

        print(f"  Matched trades: {len(trades)}")
        print(f"  Recovery positions: {len(recovery)}")

        output = build_output(trades, recovery, snapshots)

        json_path = runtime_dir / "strategy_truth_latest.json"
        md_path = reports_dir / "strategy_truth_latest.md"
        write_json(output, json_path)
        write_markdown(output, md_path)

        print(f"\nOutput:")
        print(f"  JSON: {json_path}")
        print(f"  MD:   {md_path}")

        # Print summary
        s = output["summary"]
        print(f"\n=== Summary ===")
        print(f"Active trades: {s['total_active_trades']} (closed: {s['total_closed_trades']}, open: {s['total_open_trades']})")
        print(f"Recovery positions: {s['total_recovery_positions']}")
        print(f"Active PnL: {s['total_active_pnl_usd']:.2f} USDT")
        print(f"Recovery unrealized PnL: {s['total_recovery_unrealized_pnl_usd']:.2f} USDT")
        print()
        for strategy in ["A/v11", "B/v16", "C/v14"]:
            st = output["strategy_stats"].get(strategy, {})
            rec = output["recovery_stats"].get(strategy, {})
            print(f"  {strategy}: closed={st.get('closed_trades',0)} win_rate={st.get('win_rate',0):.1f}% "
                  f"net_pnl={st.get('net_pnl_usd',0):.2f} PF={st.get('profit_factor',0)} "
                  f"recovery={rec.get('count',0)} rec_upnl={rec.get('total_unrealized_pnl',0):.2f}")

        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
