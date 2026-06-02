"""三策略每日市场复盘。

生成昨日涨跌幅排行榜，检查 A/v11、B/v16、C/v14 是否开仓、方向是否正确、
未开仓/错过原因，以及每个策略昨日最大盈利/亏损交易明细。

用法:
    python daily_market_review.py
    python daily_market_review.py --date 2026-05-18
    python daily_market_review.py --top 20
"""

from __future__ import annotations

import argparse
import html
import importlib
import json
import math
import re
import sys
import time
import urllib.parse
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

CST = timezone(timedelta(hours=8))
BINANCE_LIVE = "https://fapi.binance.com"
PORTAL_URL = "file:///F:/AutoTrading/reports/index.html"

SCRIPT_DIR = Path(__file__).resolve().parent
if (SCRIPT_DIR / "core").exists():
    ROOT = SCRIPT_DIR
else:
    ROOT = SCRIPT_DIR.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.review_analytics import (  # noqa: E402
    best_symbol_decision,
    decision_layer,
    decision_funnel,
    label_category,
    label_layer,
    layer_funnel,
    summarize_counter,
    summarize_layer_counter,
)
from core.position_utils import infer_position_side, position_unrealized_pnl  # noqa: E402
from core.binance_api_guard import record_public_response, wait_before_public_request  # noqa: E402

REPORTS_DIR = ROOT / "reports"
REPORTS_DIR.mkdir(exist_ok=True)

STRATEGIES = [
    {
        "key": "A",
        "name": "A/v11",
        "events": ROOT / "scanner_data" / "events.jsonl",
        "trades": ROOT / "scanner_data" / "trades.jsonl",
        "signals": ROOT / "logs" / "signals.jsonl",
        "decisions": ROOT / "logs" / "decisions.jsonl",
        "client_module": "binance_client",
    },
    {
        "key": "B",
        "name": "B/v16",
        "events": ROOT / "scanner_data_v16" / "events.jsonl",
        "trades": ROOT / "scanner_data_v16" / "trades.jsonl",
        "signals": ROOT / "logs_v16" / "signals.jsonl",
        "decisions": ROOT / "logs_v16" / "decisions.jsonl",
        "client_module": "binance_client_v2",
    },
    {
        "key": "C",
        "name": "C/v14",
        "events": ROOT / "scanner_data_v14" / "events.jsonl",
        "trades": ROOT / "scanner_data_v14" / "trades.jsonl",
        "signals": ROOT / "logs_v14" / "signals.jsonl",
        "decisions": ROOT / "logs_v14" / "decisions.jsonl",
        "client_module": "binance_client_v3",
    },
]


def strategies_for_root(data_root: Path) -> list[dict[str, Any]]:
    strategies: list[dict[str, Any]] = []
    for strategy in STRATEGIES:
        item = dict(strategy)
        for key in ("events", "trades", "signals", "decisions"):
            rel = Path(strategy[key]).relative_to(ROOT)
            item[key] = data_root / rel
        strategies.append(item)
    return strategies


def fetch_json(url: str, timeout: int = 15) -> Any:
    wait_before_public_request("daily-market-review", url)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        record_public_response("daily-market-review", url, exc.code, body)
        raise


def day_bounds_cst(date_str: str) -> tuple[datetime, datetime, int, int]:
    start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=CST)
    end = start + timedelta(days=1)
    return start, end, int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    text = text.split(" [")[0]
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST)
        return dt.astimezone(CST)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(text[:26], fmt).replace(tzinfo=CST)
        except Exception:
            continue
    return None


def in_day(item: dict, date_str: str, *fields: str) -> bool:
    for field in fields:
        dt = parse_dt(item.get(field))
        if dt and dt.strftime("%Y-%m-%d") == date_str:
            return True
    return False


def read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def read_jsonl_day(path: Path, date_str: str, *fields: str) -> list[dict]:
    shard = path.parent / path.stem / f"{date_str}.jsonl"
    if shard.exists():
        return read_jsonl(shard)
    rows: list[dict] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict) and in_day(row, date_str, *fields):
                rows.append(row)
    return rows


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def is_restored_trade(trade: dict) -> bool:
    """Whether a closed trade came from exchange-state recovery, not a logged strategy entry."""
    entry_time = str(trade.get("entry_time", ""))
    reason = str(trade.get("reason") or trade.get("entry_reason") or "")
    order_id = str(trade.get("order_id") or trade.get("okx_ord_id") or "")
    return "[恢复]" in entry_time or "恢复" in reason or order_id == "restored"


def split_restored_trades(trades: list[dict]) -> tuple[list[dict], list[dict]]:
    restored = [t for t in trades if is_restored_trade(t)]
    attributed = [t for t in trades if not is_restored_trade(t)]
    return attributed, restored


def summarize_trades(trades: list[dict]) -> dict:
    pnl = sum(to_float(t.get("pnl_usd")) for t in trades)
    wins = sum(1 for t in trades if to_float(t.get("pnl_usd")) > 0)
    losses = sum(1 for t in trades if to_float(t.get("pnl_usd")) < 0)
    gross_profit = sum(to_float(t.get("pnl_usd")) for t in trades if to_float(t.get("pnl_usd")) > 0)
    gross_loss = abs(sum(to_float(t.get("pnl_usd")) for t in trades if to_float(t.get("pnl_usd")) < 0))
    return {
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(trades) * 100 if trades else 0,
        "pnl": pnl,
        "avg_pnl": pnl / len(trades) if trades else 0,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else None,
    }


def parse_balance(balance: Any) -> tuple[float, float]:
    """Return (wallet, available) across Binance account/balance response variants."""
    if isinstance(balance, dict):
        assets = balance.get("assets")
        if isinstance(assets, list):
            usdt = next((x for x in assets if x.get("asset") == "USDT"), {})
            return (
                to_float(usdt.get("walletBalance") or usdt.get("balance")),
                to_float(usdt.get("availableBalance")),
            )
        return (
            to_float(balance.get("totalWalletBalance") or balance.get("balance")),
            to_float(balance.get("availableBalance")),
        )
    if isinstance(balance, list):
        usdt = next((x for x in balance if x.get("asset") == "USDT"), {})
        return (
            to_float(usdt.get("walletBalance") or usdt.get("balance") or usdt.get("crossWalletBalance")),
            to_float(usdt.get("availableBalance")),
        )
    return 0.0, 0.0


def load_current_snapshot(strategy: dict) -> dict:
    """Current exchange state. Failure is non-fatal so historical reports still generate."""
    snapshot = {
        "wallet": 0.0,
        "available": 0.0,
        "unrealized": 0.0,
        "positions": 0,
        "longs": 0,
        "shorts": 0,
        "error": "",
    }
    module_name = strategy.get("client_module")
    if not module_name:
        return snapshot
    try:
        module = importlib.import_module(module_name)
        client = module.get_client()
        snapshot["wallet"], snapshot["available"] = parse_balance(client.get_balance())
        positions = client.get_positions()
        for pos in positions:
            amt = to_float(pos.get("positionAmt"))
            if abs(amt) <= 0.0001:
                continue
            snapshot["positions"] += 1
            side = infer_position_side(pos)[0].lower()
            if side == "long":
                snapshot["longs"] += 1
            elif side == "short":
                snapshot["shorts"] += 1
            snapshot["unrealized"] += position_unrealized_pnl(pos, side)[0]
    except Exception as exc:
        snapshot["error"] = str(exc)[:120]
    return snapshot


def side_is_correct(side: str, change_pct: float) -> bool:
    side = (side or "").lower()
    if side in ("long", "buy"):
        return change_pct > 0
    if side in ("short", "sell"):
        return change_pct < 0
    return False


def fmt_duration(entry: Any, exit_: Any) -> str:
    a = parse_dt(entry)
    b = parse_dt(exit_)
    if not a or not b:
        return "-"
    mins = max(0, int((b - a).total_seconds() // 60))
    if mins < 60:
        return f"{mins}m"
    return f"{mins // 60}h{mins % 60:02d}m"


def get_usdt_perp_symbols() -> list[str]:
    data = fetch_json(f"{BINANCE_LIVE}/fapi/v1/exchangeInfo")
    symbols = []
    for s in data.get("symbols", []):
        if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING":
            symbols.append(s["symbol"])
    return symbols


def fetch_daily_move(symbol: str, start_ms: int, end_ms: int) -> dict | None:
    params = urllib.parse.urlencode({
        "symbol": symbol,
        "interval": "1h",
        "startTime": start_ms,
        "endTime": end_ms - 1,
        "limit": 24,
    })
    try:
        rows = fetch_json(f"{BINANCE_LIVE}/fapi/v1/klines?{params}", timeout=12)
    except Exception:
        return None
    if not rows:
        return None
    try:
        open_price = float(rows[0][1])
        close_price = float(rows[-1][4])
        high = max(float(r[2]) for r in rows)
        low = min(float(r[3]) for r in rows)
        quote_volume = sum(float(r[7]) for r in rows)
    except Exception:
        return None
    if open_price <= 0:
        return None
    return {
        "symbol": symbol,
        "open": open_price,
        "close": close_price,
        "high": high,
        "low": low,
        "change_pct": (close_price - open_price) / open_price * 100,
        "amplitude_pct": (high - low) / low * 100 if low > 0 else 0,
        "quote_volume": quote_volume,
    }


def fetch_market_rank(date_str: str, limit_symbols: int | None = None) -> list[dict]:
    _, _, start_ms, end_ms = day_bounds_cst(date_str)
    symbols = get_usdt_perp_symbols()
    if limit_symbols:
        symbols = symbols[:limit_symbols]
    moves = []
    for idx, sym in enumerate(symbols, 1):
        item = fetch_daily_move(sym, start_ms, end_ms)
        if item:
            moves.append(item)
        if idx % 30 == 0:
            time.sleep(0.2)
    return moves


def load_strategy_day(strategy: dict, date_str: str) -> dict:
    events = read_jsonl_day(strategy["events"], date_str, "time", "ts")
    trades = read_jsonl_day(strategy["trades"], date_str, "exit_time", "time")
    signals = read_jsonl_day(strategy["signals"], date_str, "ts", "time")
    decisions = read_jsonl_day(strategy["decisions"], date_str, "ts", "time")
    trades_all = read_jsonl(strategy["trades"])
    attributed_trades, restored_trades = split_restored_trades(trades)
    all_attributed_trades, all_restored_trades = split_restored_trades([t for t in trades_all if "pnl_usd" in t])
    opens = [e for e in events if e.get("event") == "OPEN"]
    skips = [e for e in events if e.get("event") == "OPEN_SKIPPED"]
    failed = [e for e in events if e.get("event") == "OPEN_FAILED"]
    closes = [e for e in events if e.get("event") in ("CLOSE", "FORCED_CLOSE")]

    return {
        "strategy_name": strategy["name"],
        "events": events,
        "trades": trades,
        "attributed_trades": attributed_trades,
        "restored_trades": restored_trades,
        "all_trades": trades_all,
        "all_attributed_trades": all_attributed_trades,
        "all_restored_trades": all_restored_trades,
        "signals": signals,
        "decisions": decisions,
        "opens": opens,
        "skips": skips,
        "failed": failed,
        "closes": closes,
        "current": load_current_snapshot(strategy),
    }


def latest_for_symbol(rows: list[dict], symbol: str, fields: tuple[str, ...] = ("time", "ts")) -> dict | None:
    matched = [r for r in rows if r.get("symbol") == symbol]
    if not matched:
        return None
    return sorted(matched, key=lambda r: max((parse_dt(r.get(f)) or datetime.min.replace(tzinfo=CST) for f in fields)))[-1]


def explain_no_open(data: dict, symbol: str) -> str:
    skip = latest_for_symbol(data["skips"], symbol)
    if skip:
        return skip.get("skip_reason") or skip.get("reason") or "OPEN_SKIPPED未注明原因"
    failed = latest_for_symbol(data["failed"], symbol)
    if failed:
        return failed.get("reason") or failed.get("msg") or "OPEN_FAILED未注明原因"
    sig = latest_for_symbol(data["signals"], symbol)
    if sig:
        side = sig.get("trade_side", "?")
        score = sig.get("net_score", sig.get("score", sig.get("vpb_score", "?")))
        reasons = sig.get("reasons") or sig.get(f"reasons_{side}") or []
        if isinstance(reasons, list):
            reasons = "+".join(str(x) for x in reasons[:3])
        return f"有信号未开: {side} score={score} {reasons}".strip()
    return "无记录：可能被成交量/ATR/扫描名单/分数前置过滤"


def strategy_symbol_status(data: dict, symbol: str, change_pct: float) -> dict:
    opens = [o for o in data["opens"] if o.get("symbol") == symbol]
    trades = [t for t in data["trades"] if t.get("symbol") == symbol and not is_restored_trade(t)]
    restored_trades = [t for t in data["trades"] if t.get("symbol") == symbol and is_restored_trade(t)]
    rows = opens or trades
    if rows:
        directions = []
        correct = 0
        for r in rows:
            side = (r.get("side") or "").lower()
            directions.append(side or "?")
            if side_is_correct(side, change_pct):
                correct += 1
        return {
            "opened": True,
            "side": "/".join(sorted(set(directions))),
            "correct": correct > 0,
            "detail": "方向正确" if correct > 0 else "方向相反",
            "category": "opened",
        }
    if restored_trades:
        directions = []
        correct = 0
        for r in restored_trades:
            side = (r.get("side") or "").lower()
            directions.append(side or "?")
            if side_is_correct(side, change_pct):
                correct += 1
        return {
            "opened": True,
            "side": "/".join(sorted(set(directions))),
            "correct": correct > 0,
            "detail": "恢复仓方向正确" if correct > 0 else "恢复仓方向相反",
            "category": "restored",
            "restored": True,
        }
    decision = best_symbol_decision(data.get("strategy_name", ""), data, symbol)
    detail = decision.reason or label_category(decision.category)
    return {
        "opened": False,
        "side": "-",
        "correct": False,
        "detail": detail,
        "category": decision.category,
        "layer": decision_layer(decision.category),
    }


def find_entry_detail(data: dict, trade: dict) -> dict:
    symbol = trade.get("symbol")
    side = trade.get("side")
    entry_dt = parse_dt(trade.get("entry_time"))
    opens = [o for o in data["opens"] if o.get("symbol") == symbol and (not side or o.get("side") == side)]
    if not opens:
        return {}
    if not entry_dt:
        return opens[-1]
    before = []
    for o in opens:
        odt = parse_dt(o.get("time") or o.get("ts"))
        if odt and odt <= entry_dt + timedelta(minutes=5):
            before.append((abs((entry_dt - odt).total_seconds()), o))
    return sorted(before, key=lambda x: x[0])[0][1] if before else opens[-1]


def trade_reason(data: dict, trade: dict) -> str:
    detail = find_entry_detail(data, trade)
    parts = []
    for key in ("reason", "entry_reason", "skip_reason"):
        if trade.get(key):
            parts.append(str(trade.get(key)))
    if detail:
        if detail.get("score") is not None:
            parts.append(f"score={detail.get('score')}")
        reasons = detail.get("reasons")
        if isinstance(reasons, list) and reasons:
            parts.append("+".join(str(x) for x in reasons[:4]))
        elif detail.get("reason"):
            parts.append(str(detail.get("reason")))
    return " | ".join(parts)[:140] if parts else "-"


def md_cell(value: Any) -> str:
    return str(value if value is not None else "-").replace("|", "/").replace("\n", " ").strip()


def fmt_dt_short(value: Any) -> str:
    dt = parse_dt(value)
    if not dt:
        return str(value or "-")[:16]
    return dt.strftime("%m-%d %H:%M")


def html_class_pnl(value: Any) -> str:
    pnl = to_float(value)
    if pnl > 0:
        return "pos"
    if pnl < 0:
        return "neg"
    return "neutral"


def html_details_table(title: str, headers: list[str], rows: list[list[Any]], open_: bool = False) -> str:
    open_attr = " open" if open_ else ""
    cells = []
    cells.append(f"<details class=\"section-details\"{open_attr}><summary>{html.escape(title)}</summary>")
    cells.append("<table><tbody>")
    cells.append("<tr>" + "".join(f"<th>{html.escape(str(h))}</th>" for h in headers) + "</tr>")
    for row in rows:
        cells.append("<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in row) + "</tr>")
    cells.append("</tbody></table></details>")
    return "\n".join(cells)


def fetch_intraday_klines(symbol: str, date_str: str, interval: str = "15m") -> list[dict]:
    start, end, start_ms, end_ms = day_bounds_cst(date_str)
    params = urllib.parse.urlencode({
        "symbol": symbol,
        "interval": interval,
        "startTime": start_ms,
        "endTime": end_ms - 1,
        "limit": 1500,
    })
    try:
        rows = fetch_json(f"{BINANCE_LIVE}/fapi/v1/klines?{params}", timeout=12)
    except Exception:
        return []
    items = []
    for row in rows:
        try:
            items.append({
                "time": datetime.fromtimestamp(row[0] / 1000, CST),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
            })
        except Exception:
            continue
    return items


def fetch_klines_range(symbol: str, start_dt: datetime, end_dt: datetime, interval: str = "15m") -> list[dict]:
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=CST)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=CST)
    start_dt = start_dt.astimezone(CST)
    end_dt = end_dt.astimezone(CST)
    if end_dt <= start_dt:
        end_dt = start_dt + timedelta(hours=6)
    params = urllib.parse.urlencode({
        "symbol": symbol,
        "interval": interval,
        "startTime": int(start_dt.timestamp() * 1000),
        "endTime": int(end_dt.timestamp() * 1000) - 1,
        "limit": 1500,
    })
    try:
        rows = fetch_json(f"{BINANCE_LIVE}/fapi/v1/klines?{params}", timeout=12)
    except Exception:
        return []
    items = []
    for row in rows:
        try:
            items.append({
                "time": datetime.fromtimestamp(row[0] / 1000, CST),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
            })
        except Exception:
            continue
    return items


def cached_klines(symbol: str, date_str: str, klines_cache: dict[str, list[dict]]) -> list[dict]:
    if symbol not in klines_cache:
        klines_cache[symbol] = fetch_intraday_klines(symbol, date_str, "15m")
    return klines_cache[symbol]


def cached_klines_range(symbol: str, start_dt: datetime, end_dt: datetime, klines_cache: dict[str, list[dict]]) -> list[dict]:
    key = f"{symbol}|{int(start_dt.timestamp())}|{int(end_dt.timestamp())}"
    if key not in klines_cache:
        klines_cache[key] = fetch_klines_range(symbol, start_dt, end_dt, "15m")
    return klines_cache[key]


def chart_context_bars(entry_dt: datetime, exit_dt: datetime) -> tuple[int, int]:
    """Choose a contextual window that keeps entry/exit near the middle.

    Short trades need more "before/after" market context; longer trades need
    proportionate context without stretching the chart into a flat timeline.
    """
    span_minutes = max(15.0, (exit_dt - entry_dt).total_seconds() / 60.0)
    span_bars = max(1, int(math.ceil(span_minutes / 15.0)))

    if span_bars <= 4:
        return 16, 16
    if span_bars <= 12:
        bars = max(14, span_bars * 2)
        return bars, bars
    if span_bars <= 32:
        bars = max(12, int(math.ceil(span_bars * 1.15)))
        return bars, bars

    bars = min(48, max(14, int(math.ceil(span_bars * 0.6))))
    return bars, bars


def chart_x(dt: datetime, start: datetime, end: datetime, left: float, width: float) -> float:
    total = max(1.0, (end - start).total_seconds())
    return left + max(0.0, min(1.0, (dt - start).total_seconds() / total)) * width


def trade_chart_svg(trade: dict, date_str: str, klines_cache: dict[str, list[dict]]) -> str:
    symbol = str(trade.get("symbol") or "")
    if not symbol:
        return "<div class=\"chart-empty\">缺少币种，无法绘制K线。</div>"
    day_start, day_end, _, _ = day_bounds_cst(date_str)
    entry_dt = parse_dt(trade.get("entry_time") or trade.get("time"))
    exit_dt = parse_dt(trade.get("exit_time") or trade.get("time"))
    if entry_dt and not exit_dt:
        exit_dt = min(day_end, entry_dt + timedelta(hours=3))
    if exit_dt and not entry_dt:
        entry_dt = max(day_start, exit_dt - timedelta(hours=3))
    if not entry_dt or not exit_dt:
        entry_dt, exit_dt = day_start + timedelta(hours=9), day_start + timedelta(hours=15)
    if exit_dt <= entry_dt:
        exit_dt = entry_dt + timedelta(minutes=15)

    pre_bars, post_bars = chart_context_bars(entry_dt, exit_dt)
    start = entry_dt - timedelta(minutes=15 * pre_bars)
    end = exit_dt + timedelta(minutes=15 * post_bars)
    klines = cached_klines_range(symbol, start, end, klines_cache)
    if not klines:
        return f"<div class=\"chart-empty\">{html.escape(symbol)} 当天15m K线获取失败。</div>"

    start = klines[0]["time"]
    end = klines[-1]["time"] + timedelta(minutes=15)
    width, height = 920, 300
    left, right, top, bottom = 54, 18, 20, 34
    plot_w = width - left - right
    plot_h = height - top - bottom
    highs = [k["high"] for k in klines]
    lows = [k["low"] for k in klines]
    hi, lo = max(highs), min(lows)
    pad = (hi - lo) * 0.08 if hi > lo else hi * 0.01 or 1
    hi += pad
    lo -= pad

    def y(price: float) -> float:
        if hi <= lo:
            return top + plot_h / 2
        return top + (hi - price) / (hi - lo) * plot_h

    candle_w = max(2.2, plot_w / max(len(klines), 1) * 0.58)
    parts = [
        f"<svg class=\"kline\" viewBox=\"0 0 {width} {height}\" role=\"img\" aria-label=\"{html.escape(symbol)} 15m K线交易窗口\">",
        f"<rect x=\"0\" y=\"0\" width=\"{width}\" height=\"{height}\" rx=\"8\" class=\"chart-bg\"/>",
    ]
    for frac in (0, 0.25, 0.5, 0.75, 1):
        yy = top + plot_h * frac
        price = hi - (hi - lo) * frac
        parts.append(f"<line x1=\"{left}\" y1=\"{yy:.1f}\" x2=\"{width-right}\" y2=\"{yy:.1f}\" class=\"grid\"/>")
        parts.append(f"<text x=\"8\" y=\"{yy+4:.1f}\" class=\"axis\">{price:.4g}</text>")

    for idx, k in enumerate(klines):
        x = left + (idx + 0.5) / len(klines) * plot_w
        up = k["close"] >= k["open"]
        cls = "candle-up" if up else "candle-down"
        y_open, y_close = y(k["open"]), y(k["close"])
        body_top = min(y_open, y_close)
        body_h = max(1.4, abs(y_close - y_open))
        parts.append(f"<line x1=\"{x:.1f}\" y1=\"{y(k['high']):.1f}\" x2=\"{x:.1f}\" y2=\"{y(k['low']):.1f}\" class=\"wick {cls}\"/>")
        parts.append(f"<rect x=\"{x-candle_w/2:.1f}\" y=\"{body_top:.1f}\" width=\"{candle_w:.1f}\" height=\"{body_h:.1f}\" class=\"{cls}\"/>")

    def marker(kind: str, time_value: Any, price_value: Any, label: str) -> None:
        dt = parse_dt(time_value)
        price = to_float(price_value)
        if not dt or price <= 0:
            return
        x = chart_x(dt, start, end, left, plot_w)
        yy = y(price)
        cls = "entry-marker" if kind == "entry" else "exit-marker"
        text_y = max(14, yy - 10) if kind == "entry" else min(height - 8, yy + 18)
        parts.append(f"<line x1=\"{x:.1f}\" y1=\"{top}\" x2=\"{x:.1f}\" y2=\"{height-bottom}\" class=\"{cls}\"/>")
        parts.append(f"<circle cx=\"{x:.1f}\" cy=\"{yy:.1f}\" r=\"4.5\" class=\"{cls}\"/>")
        parts.append(f"<text x=\"{min(width-140, x+6):.1f}\" y=\"{text_y:.1f}\" class=\"marker-label {cls}\">{html.escape(label)} {price:.4g}</text>")

    side = str(trade.get("side") or "-")
    marker("entry", trade.get("entry_time") or trade.get("time"), trade.get("entry_price") or trade.get("price"), f"入场 {side}")
    marker("exit", trade.get("exit_time") or trade.get("time"), trade.get("exit_price"), "离场")
    parts.append(f"<text x=\"{left}\" y=\"{height-9}\" class=\"axis\">{start.strftime('%m-%d %H:%M')}</text>")
    parts.append(f"<text x=\"{width-150}\" y=\"{height-9}\" class=\"axis\">{end.strftime('%m-%d %H:%M')}</text>")
    parts.append(
        f"<text x=\"{width/2-126:.1f}\" y=\"{height-9}\" class=\"axis\">交易窗口: 前{pre_bars}根 / 后{post_bars}根 15m K线</text>"
    )
    parts.append("</svg>")
    return "\n".join(parts)


def trade_summary_text(trade: dict) -> str:
    symbol = trade.get("symbol", "-")
    side = trade.get("side", "-")
    pnl = to_float(trade.get("pnl_usd"))
    pct = to_float(trade.get("pnl_pct"))
    restored = "恢复仓" if is_restored_trade(trade) else "策略开仓"
    return f"{symbol} {side} | {pnl:+.2f} USDT / {pct:+.2f}% | {restored} | {fmt_duration(trade.get('entry_time'), trade.get('exit_time') or trade.get('time'))}"


def trade_detail_block(strategy_name: str, data: dict, trade: dict, date_str: str, klines_cache: dict[str, list[dict]], open_: bool = False) -> str:
    pnl = to_float(trade.get("pnl_usd"))
    pnl_pct = to_float(trade.get("pnl_pct"))
    side = str(trade.get("side") or "-")
    symbol = str(trade.get("symbol") or "-")
    trade_type = "恢复仓" if is_restored_trade(trade) else "策略开仓"
    reverse = ""
    day_move = None
    for row in klines_cache.get("__moves__", []):
        if row.get("symbol") == symbol:
            day_move = to_float(row.get("change_pct"))
            break
    if day_move is not None and side_is_correct(side, day_move) is False:
        reverse = " reverse-trade"
    open_attr = " open" if open_ else ""
    reason = trade_reason(data, trade)
    exit_reason = trade.get("exit_reason") or trade.get("reason") or "-"
    meta_rows = [
        ("策略", strategy_name),
        ("类型", trade_type),
        ("方向", side),
        ("PnL", f"{pnl:+.2f} USDT / {pnl_pct:+.2f}%"),
        ("入场", f"{fmt_dt_short(trade.get('entry_time'))} @{trade.get('entry_price','-')}"),
        ("离场", f"{fmt_dt_short(trade.get('exit_time') or trade.get('time'))} @{trade.get('exit_price','-')}"),
        ("持仓时间", fmt_duration(trade.get("entry_time"), trade.get("exit_time") or trade.get("time"))),
        ("入场原因", reason),
        ("离场原因", exit_reason),
    ]
    if day_move is not None:
        meta_rows.insert(3, ("当日涨跌", f"{day_move:+.2f}%"))
    meta_html = "".join(
        f"<div><b>{html.escape(k)}</b><span>{html.escape(str(v))}</span></div>" for k, v in meta_rows
    )
    return f"""
<details class="trade-card{reverse}"{open_attr}>
<summary><span class="trade-symbol">{html.escape(symbol)}</span><span>{html.escape(side)}</span><span class="{html_class_pnl(pnl)}">{pnl:+.2f} USDT</span><span>{html.escape(trade_type)}</span></summary>
<div class="trade-grid">
<div class="trade-meta">{meta_html}</div>
<div class="trade-chart">{trade_chart_svg(trade, date_str, klines_cache)}</div>
</div>
</details>
""".strip()


def strategy_trade_cards(strategy_name: str, data: dict, date_str: str, klines_cache: dict[str, list[dict]]) -> str:
    trades = sorted(
        [t for t in data["trades"] if "pnl_usd" in t],
        key=lambda t: parse_dt(t.get("exit_time") or t.get("time")) or datetime.min.replace(tzinfo=CST),
    )
    if not trades:
        return f"<p class=\"empty\">{html.escape(strategy_name)} 昨日没有已平仓交易。</p>"
    total = summarize_trades(trades)
    header = (
        f"<details class=\"strategy-trades\" open><summary>{html.escape(strategy_name)} "
        f"全部交易 {total['trades']} 笔，PnL <span class=\"{html_class_pnl(total['pnl'])}\">{total['pnl']:+.2f}</span>，胜率 {total['win_rate']:.1f}%</summary>"
    )
    cards = "\n".join(trade_detail_block(strategy_name, data, t, date_str, klines_cache) for t in trades)
    return header + "\n" + cards + "\n</details>"




def pct_change(a: float, b: float) -> float:
    return (b - a) / a * 100 if a else 0.0


def side_move(side: str, entry: float, price: float) -> float:
    return pct_change(entry, price) if side == "long" else pct_change(price, entry)


def classify_entry_stage(symbol: str, side: str, entry_time: Any, entry_price: Any, date_str: str, day_change: float) -> dict:
    entry_dt = parse_dt(entry_time)
    entry_px = to_float(entry_price)
    if not entry_dt or entry_px <= 0:
        return {"stage": "无法分析", "detail": "缺少入场时间或价格"}
    klines = fetch_intraday_klines(symbol, date_str, "15m")
    if not klines:
        return {"stage": "无法分析", "detail": "无法获取15m行情"}
    before = [k for k in klines if k["time"] < entry_dt]
    after = [k for k in klines if k["time"] >= entry_dt]
    if not before:
        before = klines[:1]
    prev6 = before[-24:]
    prev3 = before[-12:]
    day_high = max(k["high"] for k in klines)
    day_low = min(k["low"] for k in klines)
    before_high = max(k["high"] for k in before)
    before_low = min(k["low"] for k in before)
    pos_day = (entry_px - day_low) / (day_high - day_low) * 100 if day_high > day_low else 50
    pos_before = (entry_px - before_low) / (before_high - before_low) * 100 if before_high > before_low else 50
    pre6 = pct_change(prev6[0]["open"], prev6[-1]["close"]) if len(prev6) >= 2 else 0
    pre3 = pct_change(prev3[0]["open"], prev3[-1]["close"]) if len(prev3) >= 2 else 0
    if after:
        mfe = max(side_move(side, entry_px, k["high"] if side == "long" else k["low"]) for k in after)
        mae = min(side_move(side, entry_px, k["low"] if side == "long" else k["high"]) for k in after)
    else:
        mfe = mae = 0.0
    reverse_day = (side == "short" and day_change > 0) or (side == "long" and day_change < 0)
    if reverse_day and ((side == "short" and pre6 > 3 and pos_before > 55) or (side == "long" and pre6 < -3 and pos_before < 45)):
        stage = "强趋势中左侧逆势"
        weakness = "趋势过滤不足，过早摸顶/抄底"
    elif reverse_day and ((side == "short" and pre3 < 0) or (side == "long" and pre3 > 0)):
        stage = "趋势日中段回调误判"
        weakness = "把趋势中的回调/反弹当成反转"
    elif reverse_day:
        stage = "短线噪声逆势"
        weakness = "局部信号有效但缺少日内方向确认"
    else:
        stage = "顺势/非反向"
        weakness = "方向未构成反向样本"
    detail = (
        f"入场位于当日区间{pos_day:.0f}%/此前区间{pos_before:.0f}%，"
        f"入场前6h {pre6:+.2f}%、3h {pre3:+.2f}%，"
        f"入场后MFE {mfe:+.2f}% / MAE {mae:+.2f}%"
    )
    return {"stage": stage, "weakness": weakness, "detail": detail, "pre6": pre6, "pre3": pre3, "mfe": mfe, "mae": mae}


def find_entry_rows_for_symbol(data: dict, symbol: str, side: str, date_str: str) -> list[dict]:
    rows = []
    for open_event in data["opens"]:
        if open_event.get("symbol") == symbol and (open_event.get("side") or "").lower() == side:
            rows.append(open_event)
    if rows:
        return rows
    for trade in data["trades"]:
        if trade.get("symbol") == symbol and (trade.get("side") or "").lower() == side and not is_restored_trade(trade):
            rows.append(trade)
    return rows


def top_trades(data: dict, n: int = 5) -> tuple[list[dict], list[dict]]:
    trades = [t for t in data["trades"] if "pnl_usd" in t]
    wins = sorted(trades, key=lambda t: float(t.get("pnl_usd", 0) or 0), reverse=True)[:n]
    losses = sorted(trades, key=lambda t: float(t.get("pnl_usd", 0) or 0))[:n]
    return wins, losses


def is_c_v14_entry_candidate_signal(row: dict) -> bool:
    """Match the C/v14 real entry-candidate gate used by the portal summary."""
    tf = str(row.get("timeframe") or row.get("tf") or "").lower()
    side = str(row.get("trade_side") or row.get("side") or "").lower()
    score = abs(to_float(row.get("net_score", row.get("score", row.get("vpb_score", 0)))))
    if tf != "1h" or score > 80:
        return False
    return (side == "long" and score >= 55) or (side == "short" and score >= 70)


def signal_summary(data: dict) -> dict[str, Any]:
    raw = len(data["signals"])
    if data.get("strategy_name") == "C/v14":
        count = sum(1 for row in data["signals"] if is_c_v14_entry_candidate_signal(row))
        return {
            "count": count,
            "raw": raw,
            "cell": f"{count}<br><small>原始 {raw}</small>" if raw != count else str(count),
            "label": "入场候选",
        }
    return {"count": raw, "raw": raw, "cell": str(raw), "label": "信号数"}


def summarize_strategy(data: dict) -> dict:
    trades = [t for t in data["trades"] if "pnl_usd" in t]
    attributed = [t for t in data["attributed_trades"] if "pnl_usd" in t]
    restored = [t for t in data["restored_trades"] if "pnl_usd" in t]
    full = summarize_trades(trades)
    attributed_summary = summarize_trades(attributed)
    restored_summary = summarize_trades(restored)
    all_full = summarize_trades([t for t in data["all_trades"] if "pnl_usd" in t])
    all_attributed = summarize_trades(data["all_attributed_trades"])
    all_restored = summarize_trades(data["all_restored_trades"])
    sig = signal_summary(data)
    return {
        **full,
        "attributed": attributed_summary,
        "restored": restored_summary,
        "all_full": all_full,
        "all_attributed": all_attributed,
        "all_restored": all_restored,
        "current": data.get("current", {}),
        "opens": len(data["opens"]),
        "skips": len(data["skips"]),
        "signals": sig["count"],
        "raw_signals": sig["raw"],
        "signal_cell": sig["cell"],
    }


def build_report(date_str: str, moves: list[dict], strategy_data: dict[str, dict], top_n: int) -> str:
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    top_gainers = sorted(moves, key=lambda x: x["change_pct"], reverse=True)[:top_n]
    top_losers = sorted(moves, key=lambda x: x["change_pct"])[:top_n]
    watched = top_gainers + top_losers

    lines = [
        f"# 三策略每日市场复盘 - {date_str}",
        "",
        f"生成时间: {now}",
        f"统计口径: Binance USDT 永续，按北京时间 {date_str} 00:00-24:00 的 1h K线合成日涨跌。",
        "",
        "## 一、市场涨跌幅榜",
        "",
    ]

    def rank_table(title: str, rows: list[dict]):
        lines.append(f"### {title}")
        lines.append("")
        lines.append("| # | 币种 | 涨跌幅 | 振幅 | 成交额(亿USDT) |")
        lines.append("|---|------|--------|------|----------------|")
        for i, r in enumerate(rows, 1):
            lines.append(
                f"| {i} | {r['symbol']} | {r['change_pct']:+.2f}% | "
                f"{r['amplitude_pct']:.2f}% | {r['quote_volume']/1e8:.2f} |"
            )
        lines.append("")

    rank_table("涨幅榜", top_gainers)
    rank_table("跌幅榜", top_losers)

    lines += [
        "## 二、三策略是否抓到大行情",
        "",
        "| 币种 | 日涨跌 | A/v11 | B/v16 | C/v14 | 结论 |",
        "|------|--------|-------|-------|-------|------|",
    ]
    missed_counter = Counter()
    missed_category_counter = Counter()
    reverse_hits = []
    for item in watched:
        sym = item["symbol"]
        cells = []
        conclusions = []
        for strategy in STRATEGIES:
            data = strategy_data[strategy["key"]]
            status = strategy_symbol_status(data, sym, item["change_pct"])
            if status["opened"]:
                mark = "对" if status["correct"] else "反"
                prefix = "恢复" if status.get("restored") else "开"
                cells.append(f"{prefix}{status['side']}({mark})")
                if not status["correct"]:
                    reverse_hits.append((strategy["name"], sym, status["side"], item["change_pct"]))
                    conclusions.append(f"{strategy['name']}{'恢复仓' if status.get('restored') else '反向'}")
            else:
                reason = status["detail"]
                category = status.get("category", "other")
                cells.append(f"未开[{label_category(category)}]: {reason[:20]}")
                missed_counter[reason.split(":")[0].split("：")[0]] += 1
                missed_category_counter[category] += 1
        conclusion = "；".join(conclusions) if conclusions else "无反向开仓"
        lines.append(f"| {sym} | {item['change_pct']:+.2f}% | {cells[0]} | {cells[1]} | {cells[2]} | {conclusion} |")

    watched_symbols = [item["symbol"] for item in watched]
    lines += ["", "## 三、大行情决策漏斗", ""]
    lines.append("| 策略 | 大行情样本漏斗 | 全日决策漏斗 |")
    lines.append("|------|----------------|--------------|")
    for strategy in STRATEGIES:
        data = strategy_data[strategy["key"]]
        watched_funnel = decision_funnel(strategy["name"], data, watched_symbols)
        full_funnel = decision_funnel(strategy["name"], data)
        lines.append(
            f"| {strategy['name']} | {summarize_counter(watched_funnel)} | "
            f"{summarize_counter(full_funnel)} |"
        )

    lines += ["", "## 四、横向三层对比", ""]
    lines.append("| 策略 | 大行情层级分布 | 全日层级分布 | 主要卡点 |")
    lines.append("|------|----------------|--------------|----------|")
    for strategy in STRATEGIES:
        data = strategy_data[strategy["key"]]
        watched_layers = layer_funnel(strategy["name"], data, watched_symbols)
        full_layers = layer_funnel(strategy["name"], data)
        full_categories = decision_funnel(strategy["name"], data)
        bottleneck = " / ".join(f"{label_category(k)} {v}" for k, v in full_categories.most_common(4)) or "-"
        lines.append(
            f"| {strategy['name']} | {summarize_layer_counter(watched_layers)} | "
            f"{summarize_layer_counter(full_layers)} | {bottleneck} |"
        )

    lines.append("")
    lines.append("### 大行情逐币卡点")
    lines.append("")
    lines.append("| 币种 | 涨跌 | A/v11卡点 | B/v16卡点 | C/v14卡点 |")
    lines.append("|------|------|-----------|-----------|-----------|")
    for item in watched:
        sym = item["symbol"]
        cells = []
        for strategy in STRATEGIES:
            data = strategy_data[strategy["key"]]
            status = strategy_symbol_status(data, sym, item["change_pct"])
            if status["opened"]:
                mark = "方向正确" if status["correct"] else "方向相反"
                prefix = "恢复仓" if status.get("restored") else "开"
                cells.append(f"{label_layer('execution')}: {prefix}{status['side']} {mark}")
            else:
                cells.append(f"{label_layer(status.get('layer','unknown'))}: {label_category(status.get('category','other'))}")
        lines.append(f"| {sym} | {item['change_pct']:+.2f}% | {cells[0]} | {cells[1]} | {cells[2]} |")

    lines += ["", "## 五、昨日策略交易表现", ""]
    lines.append("复盘结论必须同时看全量已平仓、剔除恢复仓、恢复仓贡献和当前浮盈；策略进化优先参考“剔除恢复仓PnL + 当前浮盈”。")
    lines.append("")
    lines.append("| 策略 | 平仓笔数 | 胜率 | 全量已平仓PnL | 剔除恢复仓PnL | 恢复仓PnL | 当前浮盈 | 当前仓位 | 开仓事件 | 入场候选/信号 | 跳过数 |")
    lines.append("|------|----------|------|---------------|---------------|-----------|----------|----------|----------|--------|--------|")
    for strategy in STRATEGIES:
        data = strategy_data[strategy["key"]]
        s = summarize_strategy(data)
        attr = s["attributed"]
        restored = s["restored"]
        current = s.get("current", {})
        positions = f"{current.get('positions',0)}仓({current.get('longs',0)}多/{current.get('shorts',0)}空)"
        lines.append(
            f"| {strategy['name']} | {s['trades']} | {s['win_rate']:.1f}% | "
            f"{s['pnl']:+.2f} | {attr['pnl']:+.2f} / {attr['trades']}笔 | "
            f"{restored['pnl']:+.2f} / {restored['trades']}笔 | "
            f"{current.get('unrealized',0):+.2f} | {positions} | "
            f"{s['opens']} | {s['signal_cell']} | {s['skips']} |"
        )

    lines += ["", "### 累计口径校准", ""]
    lines.append("| 策略 | 累计全量PnL | 累计剔除恢复仓PnL | 累计恢复仓PnL | 累计全量胜率 | 累计剔除恢复仓胜率 | 盈亏因子(剔除恢复仓) |")
    lines.append("|------|-------------|-------------------|---------------|--------------|--------------------|----------------------|")
    for strategy in STRATEGIES:
        s = summarize_strategy(strategy_data[strategy["key"]])
        all_full = s["all_full"]
        all_attr = s["all_attributed"]
        all_restored = s["all_restored"]
        pf = "-" if all_attr["profit_factor"] is None else f"{all_attr['profit_factor']:.2f}"
        lines.append(
            f"| {strategy['name']} | {all_full['pnl']:+.2f} / {all_full['trades']}笔 | "
            f"{all_attr['pnl']:+.2f} / {all_attr['trades']}笔 | "
            f"{all_restored['pnl']:+.2f} / {all_restored['trades']}笔 | "
            f"{all_full['win_rate']:.1f}% | {all_attr['win_rate']:.1f}% | {pf} |"
        )

    lines += ["", "## 六、各策略最大盈利/最小PnL交易", ""]
    for strategy in STRATEGIES:
        data = strategy_data[strategy["key"]]
        wins, losses = top_trades(data)
        lines.append(f"### {strategy['name']}")
        lines.append("")
        lines.append("| 类型 | 币种 | 方向 | PnL | 入场 | 离场 | 持仓 | 离场原因 | 入场原因 |")
        lines.append("|------|------|------|-----|------|------|------|----------|----------|")
        for label, rows in (("最大盈利", wins), ("最小PnL", losses)):
            if not rows:
                lines.append(f"| {label} | - | - | - | - | - | - | - | - |")
                continue
            for t in rows:
                lines.append(
                    f"| {label} | {t.get('symbol','-')} | {t.get('side','-')} | "
                    f"{float(t.get('pnl_usd',0) or 0):+.2f} / {float(t.get('pnl_pct',0) or 0):+.2f}% | "
                    f"{str(t.get('entry_time','-'))[:16]} @{t.get('entry_price','-')} | "
                    f"{str(t.get('exit_time') or t.get('time') or '-')[:16]} @{t.get('exit_price','-')} | "
                    f"{fmt_duration(t.get('entry_time'), t.get('exit_time') or t.get('time'))} | "
                    f"{md_cell(t.get('exit_reason') or t.get('reason','-'))} | {md_cell(trade_reason(data, t))} |"
                )
        lines.append("")

    lines += ["## 七、反向开仓阶段归因", ""]
    true_reverse_hits = []
    restored_reverse_hits = []
    for name, sym, side, change_pct in reverse_hits:
        strategy = next((s for s in STRATEGIES if s["name"] == name), None)
        if not strategy:
            continue
        data = strategy_data[strategy["key"]]
        status = strategy_symbol_status(data, sym, change_pct)
        if status.get("restored"):
            restored_reverse_hits.append((name, sym, side, change_pct))
            continue
        true_reverse_hits.append((name, sym, side, change_pct))

    if true_reverse_hits:
        lines.append("| 策略 | 币种 | 方向 | 日涨跌 | 入场时间 | 分数/原因 | 入场阶段 | 阶段证据 | 信号短板 |")
        lines.append("|------|------|------|--------|----------|-----------|----------|----------|----------|")
        for name, sym, side, change_pct in true_reverse_hits[:12]:
            strategy = next(s for s in STRATEGIES if s["name"] == name)
            data = strategy_data[strategy["key"]]
            rows = find_entry_rows_for_symbol(data, sym, side, date_str)
            for row in rows[:2]:
                entry_time = row.get("time") or row.get("entry_time")
                entry_price = row.get("price") or row.get("entry_price")
                stage = classify_entry_stage(sym, side, entry_time, entry_price, date_str, change_pct)
                reasons = row.get("reasons") or row.get("reason") or "-"
                if isinstance(reasons, list):
                    reasons = "+".join(str(x) for x in reasons[:4])
                score = row.get("score") or row.get("raw_score") or "-"
                lines.append(
                    f"| {name} | {sym} | {side} | {change_pct:+.2f}% | "
                    f"{str(entry_time)[:16]} @{entry_price} | score={score}; {md_cell(reasons)} | "
                    f"{stage['stage']} | {stage['detail']} | {stage.get('weakness','-')} |"
                )
    else:
        lines.append("- 昨日无本策略当日新开的反向仓。")
    if restored_reverse_hits:
        lines.append("")
        lines.append("恢复仓方向相反样本（不计入当日策略入场错误，但计入账户风险复盘）:")
        for name, sym, side, change_pct in restored_reverse_hits[:10]:
            lines.append(f"- {name} {sym} {side}，日涨跌 {change_pct:+.2f}%")
    lines.append("")

    lines += ["## 八、经验总结与优化建议", ""]
    if missed_category_counter:
        lines.append("错过类别聚合:")
        for category, count in missed_category_counter.most_common(8):
            lines.append(f"- {label_category(category)}: {count} 次")
        lines.append("")
    if missed_counter:
        lines.append("未开仓原因聚合:")
        for reason, count in missed_counter.most_common(8):
            lines.append(f"- {reason}: {count} 次")
        lines.append("")
    if reverse_hits:
        lines.append("反向开仓需复查:")
        for name, sym, side, pct in reverse_hits[:10]:
            lines.append(f"- {name} {sym} 开 {side}，日涨跌 {pct:+.2f}%")
    else:
        lines.append("- 昨日大涨/大跌榜未发现明显反向开仓。")
    lines.append("- 复盘判断必须剔除恢复仓再评价策略质量；恢复仓只说明账户接管后的平仓结果，不能当作策略入场优势。")
    lines.append("- 当前浮盈必须和已平仓PnL一起看；大浮盈若未落袋，应优先检查回撤保护、分批止盈和硬顶尾部。")
    lines.append("- 若大行情多数显示“前置过滤无记录”，说明信号在早期扫描层被挡，优先复查成交量、ATR、扫描名单和分数阈值。")
    lines.append("- 若显示“有信号未开”，优先复查持仓上限、15m确认和方向冲突规则。")
    lines.append("- 若反向开仓集中出现在大涨大跌日，建议加入趋势日禁逆势或逆势半仓规则。")
    lines.append("- 反向仓需分清真实新开与恢复仓；真实新开优先检查入场阶段，恢复仓优先检查接管后的平仓保护。")
    lines.append("")
    lines.append(f"*由 daily_market_review.py 自动生成。*")
    return "\n".join(lines)


def render_markdown_html(markdown: str, title: str) -> str:
    """Render the report into a compact static HTML page.

    This is a deliberately small Markdown subset because reports only use
    headings, bullets, emphasis and pipe tables.
    """
    out: list[str] = []
    table_open = False
    first_table_row = False

    def cell_class(text: str, col_idx: int, header: bool = False) -> str:
        if header:
            return ""
        classes: list[str] = []
        plain = text.strip()
        if re.search(r"(^|[\s|])\+\d", plain):
            classes.append("pos")
        if re.search(r"(^|[\s|])-\d", plain):
            classes.append("neg")
        if "开" in plain and "(对)" in plain:
            classes.append("hit")
        if "方向正确" in plain:
            classes.append("hit")
        if ("反向" in plain and "无反向" not in plain) or "(反)" in plain or "方向相反" in plain:
            classes.append("reverse")
        if "无反向开仓" in plain:
            classes.append("neutral")
        if plain.startswith("未开"):
            classes.append("miss")
        if "总持仓限制" in plain or "总持仓" in plain:
            classes.append("limit")
        if "确认周期过滤" in plain or "15m无" in plain:
            classes.append("confirm")
        if "前置过滤无记录" in plain:
            classes.append("prefilter")
        if "候选信号" in plain:
            classes.append("candidate")
        if "最大盈利" in plain:
            classes.append("best")
        if "最小PnL" in plain:
            classes.append("worst")
        if col_idx == 1 and plain.endswith("USDT"):
            classes.append("symbol")
        return f" class=\"{' '.join(dict.fromkeys(classes))}\"" if classes else ""

    def highlight_cell(text: str) -> str:
        escaped = html.escape(text.strip())
        escaped = escaped.replace("开long(对)", "<span class=\"pill hit\">开long(对)</span>")
        escaped = escaped.replace("开short(对)", "<span class=\"pill hit\">开short(对)</span>")
        escaped = escaped.replace("开long(反)", "<span class=\"pill reverse\">开long(反)</span>")
        escaped = escaped.replace("开short(反)", "<span class=\"pill reverse\">开short(反)</span>")
        escaped = escaped.replace("方向正确", "<span class=\"pill hit\">方向正确</span>")
        escaped = escaped.replace("方向相反", "<span class=\"pill reverse\">方向相反</span>")
        if "未开[" in escaped:
            escaped = escaped.replace("未开[", "<span class=\"pill miss\">未开[", 1)
            escaped = escaped.replace("]:", "]</span>:", 1)
        return escaped

    def close_table():
        nonlocal table_open
        if table_open:
            out.append("</tbody></table>")
            table_open = False

    for raw in markdown.splitlines():
        line = raw.rstrip()
        if not line:
            close_table()
            continue
        if line.startswith("|") and line.endswith("|"):
            raw_cells = [c.strip() for c in line.strip("|").split("|")]
            if set("".join(raw_cells)) <= {"-", ":"}:
                continue
            if not table_open:
                out.append("<table><tbody>")
                table_open = True
                first_table_row = True
            tag = "th" if first_table_row else "td"
            rendered_cells = []
            for idx, cell in enumerate(raw_cells):
                cls = cell_class(cell, idx, header=tag == "th")
                content = html.escape(cell) if tag == "th" else highlight_cell(cell)
                rendered_cells.append(f"<{tag}{cls}>{content}</{tag}>")
            out.append("<tr>" + "".join(rendered_cells) + "</tr>")
            first_table_row = False
            continue
        close_table()
        text = html.escape(line)
        if line.startswith("# "):
            out.append(f"<h1>{text[2:]}</h1>")
        elif line.startswith("## "):
            out.append(f"<h2>{text[3:]}</h2>")
        elif line.startswith("### "):
            out.append(f"<h3>{text[4:]}</h3>")
        elif line.startswith("- "):
            out.append(f"<p class=\"bullet\">{text[2:]}</p>")
        elif line.startswith("*") and line.endswith("*"):
            out.append(f"<p class=\"foot\">{text.strip('*')}</p>")
        else:
            out.append(f"<p>{text}</p>")
    close_table()

    body = "\n".join(out)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(title)}</title>
<style>
:root {{
  --bg:#0b1220; --panel:#101827; --line:#263244; --text:#e5e7eb;
  --muted:#94a3b8; --up:#22c55e; --down:#ef4444; --accent:#38bdf8;
  --warn:#f59e0b; --info:#60a5fa; --violet:#a78bfa;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font-family:Inter,"Microsoft YaHei",Arial,sans-serif; }}
.wrap {{ max-width:1500px; margin:0 auto; padding:28px; }}
h1 {{ margin:0 0 10px; font-size:30px; letter-spacing:0; }}
h2 {{ margin:28px 0 12px; font-size:20px; color:#f8fafc; }}
h3 {{ margin:20px 0 10px; font-size:16px; color:var(--accent); }}
p {{ margin:8px 0; color:var(--muted); line-height:1.65; }}
.bullet {{ padding-left:14px; position:relative; }}
.bullet:before {{ content:""; width:5px; height:5px; border-radius:50%; background:var(--accent); position:absolute; left:0; top:13px; }}
.foot {{ color:var(--muted); font-size:12px; margin-top:24px; }}
table {{ width:100%; border-collapse:collapse; margin:10px 0 18px; background:var(--panel); border:1px solid var(--line); border-radius:8px; overflow:hidden; box-shadow:0 12px 28px rgba(0,0,0,.18); }}
th,td {{ padding:9px 10px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; font-size:13px; line-height:1.45; color:#dbeafe; }}
th {{ color:#cbd5e1; background:rgba(148,163,184,.08); font-weight:700; }}
tr:hover td {{ background:rgba(255,255,255,.045); }}
td:nth-child(2), th:nth-child(2) {{ white-space:nowrap; }}
.pos {{ color:var(--up); font-weight:700; }}
.neg {{ color:var(--down); font-weight:700; }}
.symbol {{ color:#f8fafc; font-weight:800; }}
.hit {{ color:var(--up); font-weight:800; }}
.reverse {{ color:var(--down); font-weight:900; }}
.neutral {{ color:#94a3b8; }}
.miss {{ color:#cbd5e1; }}
.limit {{ color:var(--warn); font-weight:800; }}
.confirm {{ color:var(--violet); font-weight:700; }}
.prefilter {{ color:#93c5fd; }}
.candidate {{ color:#67e8f9; }}
.best {{ color:var(--up); font-weight:800; }}
.worst {{ color:var(--down); font-weight:800; }}
.pill {{ display:inline-block; padding:2px 7px; border-radius:999px; border:1px solid currentColor; background:rgba(255,255,255,.05); line-height:1.2; }}
.pill.hit {{ background:rgba(34,197,94,.12); }}
.pill.reverse {{ background:rgba(239,68,68,.13); }}
.pill.miss {{ color:#cbd5e1; background:rgba(148,163,184,.10); border-color:#475569; font-weight:700; }}
@media (max-width: 900px) {{ .wrap {{ padding:16px; }} table {{ display:block; overflow-x:auto; }} }}
</style>
</head>
<body><main class="wrap">{body}</main></body>
</html>
"""

 
def build_report(date_str: str, moves: list[dict], strategy_data: dict[str, dict], top_n: int) -> str:
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    top_gainers = sorted(moves, key=lambda x: x["change_pct"], reverse=True)[:top_n]
    top_losers = sorted(moves, key=lambda x: x["change_pct"])[:top_n]
    watched = top_gainers + top_losers
    watched_symbols = [item["symbol"] for item in watched]
    klines_cache: dict[str, list[dict]] = {"__moves__": moves}

    missed_counter = Counter()
    missed_category_counter = Counter()
    reverse_hits: list[tuple[str, str, str, float]] = []
    capture_rows: list[list[str]] = []

    for item in watched:
        sym = item["symbol"]
        cells = []
        conclusions = []
        for strategy in STRATEGIES:
            data = strategy_data[strategy["key"]]
            status = strategy_symbol_status(data, sym, item["change_pct"])
            if status["opened"]:
                mark = "方向正确" if status["correct"] else "方向相反"
                prefix = "恢复仓" if status.get("restored") else "策略开仓"
                cells.append(f"执行层: {prefix} {status['side']} {mark}")
                if not status["correct"]:
                    reverse_hits.append((strategy["name"], sym, status["side"], item["change_pct"]))
                    conclusions.append(f"{strategy['name']} {'恢复仓反向' if status.get('restored') else '真实反向'}")
            else:
                reason = status["detail"]
                category = status.get("category", "other")
                layer = label_layer(status.get("layer", "unknown"))
                cells.append(f"{layer}: {label_category(category)} - {reason[:60]}")
                missed_counter[reason.split(":")[0].split("，")[0]] += 1
                missed_category_counter[category] += 1
        conclusion = "；".join(conclusions) if conclusions else "无反向开仓"
        capture_rows.append([
            sym,
            f"{item['change_pct']:+.2f}%",
            f"{item['amplitude_pct']:.2f}%",
            cells[0],
            cells[1],
            cells[2],
            conclusion,
        ])

    lines = [
        f"# 三策略每日市场复盘 - {date_str}",
        "",
        f"生成时间: {now}",
        f"统计口径: Binance USDT 永续，北京时间 {date_str} 00:00-24:00；复盘口径同时看全量PnL、剔除恢复仓PnL、恢复仓PnL和当前浮盈。",
        "",
        "<div class=\"checkpoint\"><b>分析阶段节点</b><span>策略保持暂停，不新增交易；本报告用于复盘、归因和下一轮参数/结构优化。</span></div>",
        "",
        "## 一、策略账户与交易表现",
        "",
        "| 策略 | 平仓笔数 | 胜率 | 全量已平仓PnL | 剔除恢复仓PnL | 恢复仓PnL | 当前浮盈 | 当前仓位 | 开仓事件 | 入场候选/信号 | 跳过数 |",
        "|------|----------|------|---------------|---------------|-----------|----------|----------|----------|--------|--------|",
    ]

    for strategy in STRATEGIES:
        data = strategy_data[strategy["key"]]
        s = summarize_strategy(data)
        attr = s["attributed"]
        restored = s["restored"]
        current = s.get("current", {})
        positions = f"{current.get('positions',0)}仓({current.get('longs',0)}多/{current.get('shorts',0)}空)"
        lines.append(
            f"| {strategy['name']} | {s['trades']} | {s['win_rate']:.1f}% | "
            f"{s['pnl']:+.2f} | {attr['pnl']:+.2f} / {attr['trades']}笔 | "
            f"{restored['pnl']:+.2f} / {restored['trades']}笔 | "
            f"{current.get('unrealized',0):+.2f} | {positions} | "
            f"{s['opens']} | {s['signal_cell']} | {s['skips']} |"
        )

    lines += ["", "### 累计口径校准", ""]
    lines.append("| 策略 | 累计全量PnL | 累计剔除恢复仓PnL | 累计恢复仓PnL | 累计全量胜率 | 累计剔除恢复仓胜率 | 盈亏因子(剔除恢复仓) |")
    lines.append("|------|-------------|-------------------|---------------|--------------|--------------------|----------------------|")
    for strategy in STRATEGIES:
        s = summarize_strategy(strategy_data[strategy["key"]])
        all_full = s["all_full"]
        all_attr = s["all_attributed"]
        all_restored = s["all_restored"]
        pf = "-" if all_attr["profit_factor"] is None else f"{all_attr['profit_factor']:.2f}"
        lines.append(
            f"| {strategy['name']} | {all_full['pnl']:+.2f} / {all_full['trades']}笔 | "
            f"{all_attr['pnl']:+.2f} / {all_attr['trades']}笔 | "
            f"{all_restored['pnl']:+.2f} / {all_restored['trades']}笔 | "
            f"{all_full['win_rate']:.1f}% | {all_attr['win_rate']:.1f}% | {pf} |"
        )

    lines += [
        "",
        "## 二、大行情捕捉与卡点归因",
        "",
        "涨跌榜样本、三策略是否抓到、以及逐币卡点已经合并到同一张表，避免重复看两块信息。",
        "",
        "| 币种 | 日涨跌 | 振幅 | A/v11捕捉/卡点 | B/v16捕捉/卡点 | C/v14捕捉/卡点 | 结论 |",
        "|------|--------|------|----------------|----------------|----------------|------|",
    ]
    for row in capture_rows:
        lines.append("| " + " | ".join(md_cell(x) for x in row) + " |")

    lines += ["", "## 三、横向漏斗与三层结构", ""]
    lines.append("| 策略 | 大行情样本漏斗 | 全日决策漏斗 | 大行情层级分布 | 全日层级分布 | 主要卡点 |")
    lines.append("|------|----------------|--------------|----------------|--------------|----------|")
    for strategy in STRATEGIES:
        data = strategy_data[strategy["key"]]
        watched_funnel = decision_funnel(strategy["name"], data, watched_symbols)
        full_funnel = decision_funnel(strategy["name"], data)
        watched_layers = layer_funnel(strategy["name"], data, watched_symbols)
        full_layers = layer_funnel(strategy["name"], data)
        bottleneck = " / ".join(f"{label_category(k)} {v}" for k, v in full_funnel.most_common(4)) or "-"
        lines.append(
            f"| {strategy['name']} | {summarize_counter(watched_funnel)} | {summarize_counter(full_funnel)} | "
            f"{summarize_layer_counter(watched_layers)} | {summarize_layer_counter(full_layers)} | {bottleneck} |"
        )

    lines += ["", "## 四、各策略全部持仓/交易明细", ""]
    lines.append("默认只展示每笔交易摘要；点击具体仓位可展开入场原因、离场原因、持仓时间，以及当天15m K线上的入场/离场点。")
    for strategy in STRATEGIES:
        lines.append(strategy_trade_cards(strategy["name"], strategy_data[strategy["key"]], date_str, klines_cache))
        lines.append("")

    true_reverse_hits = []
    restored_reverse_hits = []
    for name, sym, side, change_pct in reverse_hits:
        strategy = next((s for s in STRATEGIES if s["name"] == name), None)
        if not strategy:
            continue
        data = strategy_data[strategy["key"]]
        status = strategy_symbol_status(data, sym, change_pct)
        if status.get("restored"):
            restored_reverse_hits.append((name, sym, side, change_pct))
        else:
            true_reverse_hits.append((name, sym, side, change_pct))

    lines += ["## 五、反向开仓阶段归因与K线定位", ""]
    if true_reverse_hits:
        lines.append("| 策略 | 币种 | 方向 | 日涨跌 | 入场时间 | 分数/原因 | 入场阶段 | 阶段证据 | 信号短板 |")
        lines.append("|------|------|------|--------|----------|-----------|----------|----------|----------|")
        for name, sym, side, change_pct in true_reverse_hits[:20]:
            strategy = next(s for s in STRATEGIES if s["name"] == name)
            data = strategy_data[strategy["key"]]
            rows = find_entry_rows_for_symbol(data, sym, side, date_str)
            for row in rows[:2]:
                entry_time = row.get("time") or row.get("entry_time")
                entry_price = row.get("price") or row.get("entry_price")
                stage = classify_entry_stage(sym, side, entry_time, entry_price, date_str, change_pct)
                reasons = row.get("reasons") or row.get("reason") or "-"
                if isinstance(reasons, list):
                    reasons = "+".join(str(x) for x in reasons[:4])
                score = row.get("score") or row.get("raw_score") or "-"
                lines.append(
                    f"| {name} | {sym} | {side} | {change_pct:+.2f}% | "
                    f"{fmt_dt_short(entry_time)} @{entry_price} | score={score}; {md_cell(reasons)} | "
                    f"{stage['stage']} | {stage['detail']} | {stage.get('weakness','-')} |"
                )
        lines.append("")
        lines.append("<div class=\"reverse-cards\">")
        for name, sym, side, _ in true_reverse_hits[:20]:
            strategy = next(s for s in STRATEGIES if s["name"] == name)
            data = strategy_data[strategy["key"]]
            trades = [
                t for t in data["trades"]
                if t.get("symbol") == sym and (t.get("side") or "").lower() == side and not is_restored_trade(t)
            ]
            for t in trades[:2]:
                lines.append(trade_detail_block(name, data, t, date_str, klines_cache, open_=True))
        lines.append("</div>")
    else:
        lines.append("- 昨日无本策略当日新开的反向仓。")

    if restored_reverse_hits:
        lines.append("")
        lines.append("恢复仓方向相反样本（不计入当日策略入场错误，但计入账户接管与平仓风险复盘）:")
        for name, sym, side, change_pct in restored_reverse_hits[:12]:
            lines.append(f"- {name} {sym} {side}，日涨跌 {change_pct:+.2f}%")
        lines.append("<div class=\"reverse-cards\">")
        for name, sym, side, _ in restored_reverse_hits[:12]:
            strategy = next(s for s in STRATEGIES if s["name"] == name)
            data = strategy_data[strategy["key"]]
            trades = [
                t for t in data["trades"]
                if t.get("symbol") == sym and (t.get("side") or "").lower() == side and is_restored_trade(t)
            ]
            for t in trades[:1]:
                lines.append(trade_detail_block(name, data, t, date_str, klines_cache, open_=False))
        lines.append("</div>")
    lines.append("")

    rank_headers = ["#", "币种", "涨跌幅", "振幅", "成交额(亿USDT)"]
    gain_rows = [[i, r["symbol"], f"{r['change_pct']:+.2f}%", f"{r['amplitude_pct']:.2f}%", f"{r['quote_volume']/1e8:.2f}"] for i, r in enumerate(top_gainers, 1)]
    loss_rows = [[i, r["symbol"], f"{r['change_pct']:+.2f}%", f"{r['amplitude_pct']:.2f}%", f"{r['quote_volume']/1e8:.2f}"] for i, r in enumerate(top_losers, 1)]
    lines += ["## 六、市场涨跌榜单", ""]
    lines.append(html_details_table("展开查看涨幅榜", rank_headers, gain_rows))
    lines.append(html_details_table("展开查看跌幅榜", rank_headers, loss_rows))
    lines.append("")

    lines += ["## 七、经验总结与优化建议", ""]
    if missed_category_counter:
        lines.append("错过类别聚合:")
        for category, count in missed_category_counter.most_common(8):
            lines.append(f"- {label_category(category)}: {count} 次")
        lines.append("")
    if missed_counter:
        lines.append("未开仓原因聚合:")
        for reason, count in missed_counter.most_common(8):
            lines.append(f"- {reason}: {count} 次")
        lines.append("")
    if reverse_hits:
        lines.append("反向开仓需重点复查:")
        for name, sym, side, pct in reverse_hits[:10]:
            lines.append(f"- {name} {sym} 开 {side}，日涨跌 {pct:+.2f}%")
    else:
        lines.append("- 昨日大涨/大跌榜未发现明显反向开仓。")
    lines.append("- 复盘判断必须剔除恢复仓再评价策略质量；恢复仓只说明账户接管后的平仓结果，不能当作策略入场优势。")
    lines.append("- 当前浮盈必须和已平仓PnL一起看；大浮盈若未落袋，应优先检查回撤保护、分批止盈和硬顶尾部。")
    lines.append("- 若大行情多数显示“前置过滤无记录”，优先复查成交量、ATR、扫描名单和分数阈值。")
    lines.append("- 若显示“有信号未开”，优先复查持仓上限、15m确认和方向冲突规则。")
    lines.append("- 真实反向仓要按K线阶段拆解：强趋势中左侧逆势、趋势中段回调误判、短线噪声逆势，对应不同优化方向。")
    lines.append("")
    lines.append("*由 daily_market_review.py 自动生成。*")
    return "\n".join(lines)


def render_markdown_html(markdown: str, title: str) -> str:
    out: list[str] = []
    table_open = False
    first_table_row = False

    def cell_class(text: str, col_idx: int, header: bool = False) -> str:
        if header:
            return ""
        plain = text.strip()
        classes: list[str] = []
        if re.search(r"(^|[\s|])\+\d", plain):
            classes.append("pos")
        if re.search(r"(^|[\s|])-\d", plain):
            classes.append("neg")
        if "方向正确" in plain or "策略开仓" in plain and "方向相反" not in plain:
            classes.append("hit")
        if ("方向相反" in plain or "反向" in plain) and "无反向" not in plain:
            classes.append("reverse")
        if plain.startswith("未开") or "无记录" in plain:
            classes.append("miss")
        if "持仓上限" in plain or "总持仓" in plain:
            classes.append("limit")
        if "15m" in plain or "确认" in plain:
            classes.append("confirm")
        if "前置过滤" in plain:
            classes.append("prefilter")
        if "候选信号" in plain or "有信号未开" in plain:
            classes.append("candidate")
        if col_idx == 0 and plain.endswith("USDT"):
            classes.append("symbol")
        return f" class=\"{' '.join(dict.fromkeys(classes))}\"" if classes else ""

    def highlight_cell(text: str) -> str:
        escaped = html.escape(text.strip())
        escaped = escaped.replace("方向正确", "<span class=\"pill hit\">方向正确</span>")
        escaped = escaped.replace("方向相反", "<span class=\"pill reverse\">方向相反</span>")
        escaped = escaped.replace("真实反向", "<span class=\"pill reverse\">真实反向</span>")
        escaped = escaped.replace("恢复仓反向", "<span class=\"pill reverse\">恢复仓反向</span>")
        escaped = escaped.replace("无反向开仓", "<span class=\"pill neutral\">无反向开仓</span>")
        return escaped

    def close_table() -> None:
        nonlocal table_open
        if table_open:
            out.append("</tbody></table>")
            table_open = False

    for raw in markdown.splitlines():
        line = raw.rstrip()
        if not line:
            close_table()
            continue
        if line.startswith("<"):
            close_table()
            out.append(line)
            continue
        if line.startswith("|") and line.endswith("|"):
            raw_cells = [c.strip() for c in line.strip("|").split("|")]
            if set("".join(raw_cells)) <= {"-", ":"}:
                continue
            if not table_open:
                out.append("<table><tbody>")
                table_open = True
                first_table_row = True
            tag = "th" if first_table_row else "td"
            rendered_cells = []
            for idx, cell in enumerate(raw_cells):
                cls = cell_class(cell, idx, header=tag == "th")
                content = html.escape(cell) if tag == "th" else highlight_cell(cell)
                rendered_cells.append(f"<{tag}{cls}>{content}</{tag}>")
            out.append("<tr>" + "".join(rendered_cells) + "</tr>")
            first_table_row = False
            continue
        close_table()
        text = html.escape(line)
        if line.startswith("# "):
            out.append(f"<h1>{text[2:]}</h1>")
        elif line.startswith("## "):
            out.append(f"<h2>{text[3:]}</h2>")
        elif line.startswith("### "):
            out.append(f"<h3>{text[4:]}</h3>")
        elif line.startswith("- "):
            out.append(f"<p class=\"bullet\">{text[2:]}</p>")
        elif line.startswith("*") and line.endswith("*"):
            out.append(f"<p class=\"foot\">{text.strip('*')}</p>")
        else:
            out.append(f"<p>{text}</p>")
    close_table()

    body = "\n".join(out)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(title)}</title>
<style>
:root {{
  --bg:#09111f; --panel:#101827; --panel2:#0f172a; --line:#263244; --text:#e5e7eb;
  --muted:#94a3b8; --up:#22c55e; --down:#ef4444; --accent:#38bdf8;
  --warn:#f59e0b; --info:#60a5fa; --violet:#a78bfa; --amber:#fbbf24;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font-family:Inter,"Microsoft YaHei",Arial,sans-serif; }}
.wrap {{ max-width:1520px; margin:0 auto; padding:24px; }}
.topbar {{ display:flex; justify-content:space-between; align-items:center; gap:12px; margin-bottom:18px; }}
.back-link {{ display:inline-flex; align-items:center; gap:8px; text-decoration:none; color:#e5e7eb; background:rgba(15,23,42,.92); border:1px solid rgba(148,163,184,.22); border-radius:8px; padding:10px 12px; font-weight:700; }}
.back-link:hover {{ border-color:#38bdf8; color:#fff; }}
h1 {{ margin:0 0 10px; font-size:30px; letter-spacing:0; }}
h2 {{ margin:30px 0 12px; padding-top:6px; font-size:20px; color:#f8fafc; border-top:1px solid rgba(148,163,184,.16); }}
h3 {{ margin:20px 0 10px; font-size:16px; color:var(--accent); }}
p {{ margin:8px 0; color:var(--muted); line-height:1.65; }}
.bullet {{ padding-left:14px; position:relative; }}
.bullet:before {{ content:""; width:5px; height:5px; border-radius:50%; background:var(--accent); position:absolute; left:0; top:13px; }}
.foot {{ color:var(--muted); font-size:12px; margin-top:24px; }}
.checkpoint {{ display:flex; gap:14px; align-items:center; margin:18px 0; padding:13px 16px; border:1px solid rgba(56,189,248,.35); background:rgba(56,189,248,.08); border-radius:8px; }}
.checkpoint b {{ color:#e0f2fe; white-space:nowrap; }}
.checkpoint span {{ color:#bfdbfe; }}
table {{ width:100%; border-collapse:collapse; margin:10px 0 18px; background:var(--panel); border:1px solid var(--line); border-radius:8px; overflow:hidden; box-shadow:0 12px 28px rgba(0,0,0,.18); }}
th,td {{ padding:9px 10px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; font-size:13px; line-height:1.45; color:#dbeafe; }}
th {{ color:#cbd5e1; background:rgba(148,163,184,.08); font-weight:700; }}
tr:hover td {{ background:rgba(255,255,255,.045); }}
.pos {{ color:var(--up); font-weight:800; }}
.neg {{ color:var(--down); font-weight:800; }}
.symbol,.trade-symbol {{ color:#f8fafc; font-weight:850; }}
.hit {{ color:var(--up); font-weight:800; }}
.reverse {{ color:var(--down); font-weight:900; }}
.neutral {{ color:#94a3b8; }}
.miss {{ color:#cbd5e1; }}
.limit {{ color:var(--warn); font-weight:800; }}
.confirm {{ color:var(--violet); font-weight:700; }}
.prefilter {{ color:#93c5fd; }}
.candidate {{ color:#67e8f9; }}
.pill {{ display:inline-block; padding:2px 7px; border-radius:999px; border:1px solid currentColor; background:rgba(255,255,255,.05); line-height:1.2; }}
.pill.hit {{ background:rgba(34,197,94,.12); }}
.pill.reverse {{ background:rgba(239,68,68,.13); }}
.pill.neutral {{ background:rgba(148,163,184,.10); }}
details {{ margin:12px 0; }}
summary {{ cursor:pointer; color:#f8fafc; font-weight:800; }}
.section-details,.strategy-trades,.trade-card {{ border:1px solid var(--line); background:rgba(15,23,42,.82); border-radius:8px; overflow:hidden; }}
.section-details>summary,.strategy-trades>summary {{ padding:12px 14px; background:rgba(148,163,184,.08); }}
.trade-card {{ margin:10px 0; }}
.trade-card>summary {{ display:grid; grid-template-columns:1.2fr .65fr .95fr .8fr; gap:12px; align-items:center; padding:11px 14px; background:rgba(255,255,255,.035); font-size:13px; }}
.trade-card[open]>summary {{ border-bottom:1px solid var(--line); }}
.reverse-trade {{ border-color:rgba(239,68,68,.55); box-shadow:0 0 0 1px rgba(239,68,68,.12) inset; }}
.trade-grid {{ display:grid; grid-template-columns:minmax(260px,360px) 1fr; gap:14px; padding:14px; }}
.trade-meta {{ display:grid; gap:8px; align-content:start; }}
.trade-meta div {{ display:grid; grid-template-columns:86px 1fr; gap:10px; padding:8px 10px; border:1px solid rgba(148,163,184,.16); border-radius:7px; background:rgba(2,6,23,.35); }}
.trade-meta b {{ color:#cbd5e1; font-size:12px; }}
.trade-meta span {{ color:#e5e7eb; font-size:12px; line-height:1.45; overflow-wrap:anywhere; }}
.trade-chart {{ min-width:0; }}
.kline {{ width:100%; height:auto; display:block; }}
.chart-bg {{ fill:#0b1220; stroke:#263244; }}
.grid {{ stroke:#1f2a3a; stroke-width:1; }}
.axis {{ fill:#94a3b8; font-size:11px; }}
.wick {{ stroke-width:1.1; }}
.candle-up {{ fill:#22c55e; stroke:#22c55e; }}
.candle-down {{ fill:#ef4444; stroke:#ef4444; }}
.entry-marker {{ stroke:#38bdf8; fill:#38bdf8; stroke-width:1.5; }}
.exit-marker {{ stroke:#f59e0b; fill:#f59e0b; stroke-width:1.5; }}
.marker-label {{ fill:#e5e7eb; font-size:11px; font-weight:800; paint-order:stroke; stroke:#0b1220; stroke-width:3px; }}
.chart-empty,.empty {{ padding:14px; color:#94a3b8; border:1px dashed #334155; border-radius:8px; }}
.reverse-cards {{ margin-top:12px; }}
@media (max-width: 980px) {{
  .wrap {{ padding:16px; }}
  table {{ display:block; overflow-x:auto; }}
  .trade-grid {{ grid-template-columns:1fr; }}
  .trade-card>summary {{ grid-template-columns:1fr; gap:5px; }}
  .checkpoint {{ align-items:flex-start; flex-direction:column; }}
}}
</style>
</head>
<body><main class="wrap"><div class="topbar"><a class="back-link" href="{PORTAL_URL}">返回总入口</a></div>{body}</main></body>
</html>
"""


def run(
    date_str: str | None,
    top_n: int,
    limit_symbols: int | None = None,
    html_output: bool = True,
    data_root: Path | None = None,
) -> Path:
    global STRATEGIES
    if data_root is not None:
        STRATEGIES = strategies_for_root(data_root)
    if not date_str:
        date_str = (datetime.now(CST) - timedelta(days=1)).strftime("%Y-%m-%d")
    print(f"生成 {date_str} 三策略市场复盘...")
    moves = fetch_market_rank(date_str, limit_symbols=limit_symbols)
    print(f"市场样本: {len(moves)} 个 USDT 永续")
    strategy_data = {s["key"]: load_strategy_day(s, date_str) for s in STRATEGIES}
    for s in STRATEGIES:
        d = strategy_data[s["key"]]
        sig = signal_summary(d)
        raw_note = f", raw_signals={sig['raw']}" if sig["raw"] != sig["count"] else ""
        print(
            f"{s['name']}: opens={len(d['opens'])}, trades={len(d['trades'])}, "
            f"entry_candidates_or_signals={sig['count']}{raw_note}, skips={len(d['skips'])}"
        )
    content = build_report(date_str, moves, strategy_data, top_n)
    out = REPORTS_DIR / f"market_review_{date_str}.md"
    out.write_text(content, encoding="utf-8")
    latest_md = REPORTS_DIR / "market_review_latest.md"
    latest_md.write_text(content, encoding="utf-8")
    print(f"报告已生成: {out}")
    snapshot = {
        "date": date_str,
        "generated_at": datetime.now(CST).isoformat(timespec="seconds"),
        "top_n": top_n,
        "moves": moves,
    }
    snapshot_path = REPORTS_DIR / f"market_snapshot_{date_str}.json"
    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (REPORTS_DIR / "market_snapshot_latest.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    if html_output:
        html_path = REPORTS_DIR / f"market_review_{date_str}.html"
        html_path.write_text(render_markdown_html(content, f"三策略每日市场复盘 - {date_str}"), encoding="utf-8")
        latest_html = REPORTS_DIR / "market_review_latest.html"
        latest_html.write_text(html_path.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"HTML已生成: {html_path}")
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="三策略每日市场复盘")
    parser.add_argument("--date", default=None, help="复盘日期 YYYY-MM-DD，默认昨日")
    parser.add_argument("--top", type=int, default=15, help="涨跌榜各取 N 个")
    parser.add_argument("--limit-symbols", type=int, default=None, help="调试用，仅扫描前 N 个合约")
    parser.add_argument("--data-root", default=None, help="日志根目录，默认项目根目录")
    parser.add_argument("--no-html", action="store_true", help="只生成Markdown，不生成HTML")
    args = parser.parse_args()
    run(
        args.date,
        args.top,
        args.limit_symbols,
        html_output=not args.no_html,
        data_root=Path(args.data_root) if args.data_root else None,
    )
