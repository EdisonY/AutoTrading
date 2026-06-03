"""Evaluate whether OPEN_SKIPPED filters avoided losses or missed profitable trades."""

from __future__ import annotations

import argparse
import html
import json
import math
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR if (SCRIPT_DIR / "core").exists() else SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from daily_market_review import render_markdown_html
except Exception:
    render_markdown_html = None

from core.replay import ReplayEvent, classify_replay_decision
from core.replay_fill import ReplayFillRequest, simulate_replay_fill
from core.binance_api_guard import record_public_response, wait_before_public_request
from core.binance_api_queue_client import api_queue_client_enabled, queued_api_request

CST = timezone(timedelta(hours=8))
UTC = timezone.utc
KLINE_URL = "https://testnet.binancefuture.com/fapi/v1/klines"
HORIZONS = (15, 30, 60, 120)
STRATEGIES = ("A/v11", "B/v16", "C/v14")
NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
A_V11_TRAILING_ACTIVATE_ATR = {"15m": 1.0, "30m": 1.2}
A_V11_TRAILING_PULLBACK_ATR = {"15m": 1.0, "30m": 0.8}


@dataclass
class SkipEvent:
    event_id: int
    ts: datetime
    strategy: str
    symbol: str
    side: str
    timeframe: str
    score: float | None
    stage: str
    layer: str
    reason: str
    sentinel: bool
    replay_decision: str
    replay_gate: str
    payload: dict[str, Any]


@dataclass
class Result:
    event: SkipEvent
    horizon: int
    entry_ts: datetime | None
    entry_price: float | None
    end_price: float | None
    return_pct: float | None
    sim_pnl_usdt: float | None
    mfe_pct: float | None
    mae_pct: float | None
    barrier_outcome: str
    status: str
    replay_fill: dict[str, Any] | None = None


DDL = """
create table if not exists counterfactual_open_skips (
    event_id integer not null,
    horizon_minutes integer not null,
    evaluated_at text not null,
    event_ts text not null,
    strategy text not null,
    symbol text not null,
    side text not null,
    timeframe text not null default '',
    score real,
    decision_stage text not null default '',
    filter_layer text not null default '',
    skip_reason text not null default '',
    sentinel integer not null default 0,
    entry_ts text,
    entry_price real,
    end_price real,
    return_pct real,
    sim_pnl_usdt real,
    mfe_pct real,
    mae_pct real,
    barrier_outcome text not null default '',
    data_status text not null default '',
    payload_json text not null,
    primary key(event_id, horizon_minutes)
);
create index if not exists idx_counterfactual_strategy_horizon
    on counterfactual_open_skips(strategy, horizon_minutes, event_ts);
create index if not exists idx_counterfactual_stage_horizon
    on counterfactual_open_skips(decision_stage, horizon_minutes, event_ts);
"""


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00").split(" [")[0]
    for candidate in (text, text[:26], text[:19]):
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=CST)
            return dt.astimezone(UTC)
        except Exception:
            pass
    return None


def num(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def fmt(value: float | None, digits: int = 2, sign: bool = False) -> str:
    if value is None or not math.isfinite(value):
        return "-"
    return f"{value:+.{digits}f}" if sign else f"{value:.{digits}f}"


def md(value: Any) -> str:
    return str(value if value is not None else "-").replace("|", "/").replace("\n", " ")


def normalized_reason(reason: str) -> str:
    return NUMBER_RE.sub("#", reason or "未注明原因")


def filter_name(event: SkipEvent) -> str:
    stage = event.replay_gate or event.stage or event.layer or "unknown"
    return f"{stage}: {normalized_reason(event.reason)}"


def nested_payload_dicts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = [payload]
    for key in ("raw", "raw_event", "raw_signal", "signal"):
        value = payload.get(key)
        if isinstance(value, dict):
            items.append(value)
    return items


def payload_num(payload: dict[str, Any], *keys: str) -> float | None:
    for item in nested_payload_dicts(payload):
        for key in keys:
            value = num(item.get(key))
            if value is not None:
                return value
    return None


def normalized_timeframe(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if text.isdigit():
        return f"{text}m"
    return text


def a_v11_atr_trailing_params(event: SkipEvent) -> dict[str, Any] | None:
    if event.strategy != "A/v11":
        return None
    tf = normalized_timeframe(event.timeframe)
    if tf not in A_V11_TRAILING_ACTIVATE_ATR:
        return None
    atr = payload_num(event.payload, "atr", "atr_at_entry")
    if atr is None or atr <= 0:
        return None
    return {
        "exit_model": "a_v11_atr_trailing",
        "trailing_timeframe": tf,
        "atr": atr,
        "trailing_activation_atr": A_V11_TRAILING_ACTIVATE_ATR[tf],
        "trailing_stop_atr": A_V11_TRAILING_PULLBACK_ATR[tf],
    }


def load_skip_events(db: Path, since: datetime, until: datetime) -> list[SkipEvent]:
    events: list[SkipEvent] = []
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select id, ts, strategy, symbol, side, score, stage, layer, reason, payload_json
            from events
            where event_type = 'OPEN_SKIPPED'
              and strategy in ('A/v11', 'B/v16', 'C/v14')
            order by id
            """
        )
        for row in rows:
            ts = parse_dt(row["ts"])
            side = str(row["side"] or "").lower()
            symbol = str(row["symbol"] or "").upper()
            if not ts or ts < since or ts > until or side not in {"long", "short"} or not symbol:
                continue
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except Exception:
                payload = {}
            raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else payload
            replay_event = ReplayEvent.from_event_store_row(dict(row))
            replay_decision = classify_replay_decision(replay_event)
            events.append(
                SkipEvent(
                    event_id=int(row["id"]),
                    ts=ts,
                    strategy=replay_event.strategy or str(row["strategy"]),
                    symbol=replay_event.symbol or symbol,
                    side=replay_event.side or side,
                    timeframe=replay_event.timeframe or str(raw.get("timeframe") or payload.get("timeframe") or ""),
                    score=replay_event.score if replay_event.score is not None else num(row["score"]),
                    stage=replay_event.stage or str(row["stage"] or raw.get("decision_stage") or payload.get("decision_stage") or ""),
                    layer=replay_event.layer or str(row["layer"] or raw.get("filter_layer") or payload.get("filter_layer") or ""),
                    reason=replay_event.reason or str(row["reason"] or raw.get("skip_reason") or payload.get("skip_reason") or ""),
                    sentinel=bool(raw.get("sentinel", payload.get("sentinel", False))),
                    replay_decision=replay_decision.decision,
                    replay_gate=replay_decision.gate,
                    payload=payload,
                )
            )
    return events


def ceil_next_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0) + timedelta(minutes=1)


def fetch_json(url: str, timeout: int = 15) -> Any:
    if api_queue_client_enabled():
        data = queued_api_request(scope="public", label="counterfactual-open-skips", method="GET", path=url, url=url, timeout_sec=timeout + 5)
        if isinstance(data, dict) and data.get("code") is not None and str(data.get("code")) != "200":
            raise RuntimeError(str(data.get("msg") or data))
        return data
    wait_before_public_request("counterfactual-open-skips", url)
    request = urllib.request.Request(url, headers={"User-Agent": "AutoTrading-Counterfactual/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        record_public_response("counterfactual-open-skips", url, exc.code, body)
        raise


def fetch_klines(symbol: str, start: datetime, end: datetime) -> list[list[Any]]:
    rows: list[list[Any]] = []
    cursor_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    while cursor_ms < end_ms:
        params = urllib.parse.urlencode(
            {
                "symbol": symbol,
                "interval": "1m",
                "startTime": cursor_ms,
                "endTime": end_ms - 1,
                "limit": 1500,
            }
        )
        batch = fetch_json(f"{KLINE_URL}?{params}")
        if not isinstance(batch, list) or not batch:
            break
        rows.extend(batch)
        last_open = int(batch[-1][0])
        next_cursor = last_open + 60_000
        if next_cursor <= cursor_ms:
            break
        cursor_ms = next_cursor
        if len(batch) < 1500:
            break
        time.sleep(0.03)
    return rows


def grouped_bars(events: list[SkipEvent], now: datetime, max_horizon: int) -> tuple[dict[str, dict[int, list[Any]]], list[str]]:
    ranges: dict[str, tuple[datetime, datetime]] = {}
    errors: list[str] = []
    for event in events:
        entry = ceil_next_minute(event.ts)
        end = min(entry + timedelta(minutes=max_horizon), now.replace(second=0, microsecond=0))
        old = ranges.get(event.symbol)
        if not old:
            ranges[event.symbol] = (entry, end)
        else:
            ranges[event.symbol] = (min(old[0], entry), max(old[1], end))
    output: dict[str, dict[int, list[Any]]] = {}
    for index, (symbol, (start, end)) in enumerate(sorted(ranges.items()), 1):
        try:
            rows = fetch_klines(symbol, start, end)
            output[symbol] = {int(row[0]): row for row in rows}
        except Exception as exc:
            errors.append(f"{symbol}: {str(exc)[:120]}")
        if index % 20 == 0:
            time.sleep(0.1)
    return output, errors


def evaluate(
    event: SkipEvent,
    horizon: int,
    bars_by_symbol: dict[str, dict[int, list[Any]]],
    now: datetime,
    margin_usdt: float,
    leverage: float,
    tp_pct: float,
    sl_pct: float,
) -> Result:
    entry_ts = ceil_next_minute(event.ts)
    if entry_ts + timedelta(minutes=horizon) > now.replace(second=0, microsecond=0):
        return Result(event, horizon, entry_ts, None, None, None, None, None, None, "", "pending_horizon")
    symbol_bars = bars_by_symbol.get(event.symbol, {})
    entry_ms = int(entry_ts.timestamp() * 1000)
    entry_bar = symbol_bars.get(entry_ms)
    if not entry_bar:
        return Result(event, horizon, entry_ts, None, None, None, None, None, None, "", "missing_entry_bar")
    entry_price = num(entry_bar[1])
    if not entry_price or entry_price <= 0:
        return Result(event, horizon, entry_ts, None, None, None, None, None, None, "", "invalid_entry_price")
    window: list[list[Any]] = []
    for minute in range(horizon):
        bar = symbol_bars.get(entry_ms + minute * 60_000)
        if bar:
            window.append(bar)
    if len(window) < horizon:
        return Result(event, horizon, entry_ts, entry_price, None, None, None, None, None, "", "missing_window_bars")
    close_price = num(window[-1][4])
    if close_price is None:
        return Result(event, horizon, entry_ts, entry_price, None, None, None, None, None, "", "invalid_close_price")
    direction = 1.0 if event.side == "long" else -1.0
    mfe_pct = 0.0
    mae_pct = 0.0
    for bar in window:
        high = float(bar[2])
        low = float(bar[3])
        favorable = ((high - entry_price) / entry_price * 100) if direction > 0 else ((entry_price - low) / entry_price * 100)
        adverse = ((entry_price - low) / entry_price * 100) if direction > 0 else ((high - entry_price) / entry_price * 100)
        mfe_pct = max(mfe_pct, favorable)
        mae_pct = max(mae_pct, adverse)
    quantity = (margin_usdt * leverage) / entry_price
    if event.side == "short":
        stop_loss = entry_price * (1 + sl_pct / 100)
        take_profit = entry_price * (1 - tp_pct / 100)
    else:
        stop_loss = entry_price * (1 - sl_pct / 100)
        take_profit = entry_price * (1 + tp_pct / 100)
    exit_params = a_v11_atr_trailing_params(event)
    trailing_kwargs: dict[str, Any] = {}
    if exit_params:
        trailing_kwargs = {
            "atr": exit_params["atr"],
            "trailing_stop_atr": exit_params["trailing_stop_atr"],
            "trailing_activation_atr": exit_params["trailing_activation_atr"],
        }
    fill = simulate_replay_fill(
        ReplayFillRequest(
            symbol=event.symbol,
            side=event.side,
            entry_price=entry_price,
            quantity=quantity,
            stop_loss=stop_loss,
            take_profit=take_profit,
            **trailing_kwargs,
            fee_bps=5.0,
            slippage_bps=0.0,
        ),
        [
            {
                "ts": datetime.fromtimestamp(int(bar[0]) / 1000, tz=UTC).isoformat(),
                "open": float(bar[1]),
                "high": float(bar[2]),
                "low": float(bar[3]),
                "close": float(bar[4]),
            }
            for bar in window
        ],
    )
    fill_payload = fill.to_dict()
    if exit_params:
        fill_payload.update(exit_params)
    else:
        fill_payload["exit_model"] = "fixed_pct_barrier"
    return_pct = (fill.net_pnl_usdt / (margin_usdt * leverage)) * 100
    return Result(
        event, horizon, entry_ts, entry_price, fill.exit_price, return_pct,
        fill.net_pnl_usdt, mfe_pct, mae_pct, fill.exit_reason, "complete", fill_payload
    )


def init_results_table(conn: sqlite3.Connection) -> None:
    for stmt in DDL.strip().split(";"):
        if stmt.strip():
            conn.execute(stmt)
    conn.commit()


def store_results(db: Path, results: list[Result], evaluated_at: str) -> None:
    with sqlite3.connect(db, timeout=20) as conn:
        conn.execute("pragma journal_mode=wal")
        init_results_table(conn)
        conn.executemany(
            """
            insert into counterfactual_open_skips(
                event_id, horizon_minutes, evaluated_at, event_ts, strategy, symbol,
                side, timeframe, score, decision_stage, filter_layer, skip_reason,
                sentinel, entry_ts, entry_price, end_price, return_pct, sim_pnl_usdt,
                mfe_pct, mae_pct, barrier_outcome, data_status, payload_json
            ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            on conflict(event_id, horizon_minutes) do update set
                evaluated_at=excluded.evaluated_at,
                entry_ts=excluded.entry_ts,
                entry_price=excluded.entry_price,
                end_price=excluded.end_price,
                return_pct=excluded.return_pct,
                sim_pnl_usdt=excluded.sim_pnl_usdt,
                mfe_pct=excluded.mfe_pct,
                mae_pct=excluded.mae_pct,
                barrier_outcome=excluded.barrier_outcome,
                data_status=excluded.data_status,
                payload_json=excluded.payload_json
            """,
            [
                (
                    r.event.event_id, r.horizon, evaluated_at, r.event.ts.isoformat(),
                    r.event.strategy, r.event.symbol, r.event.side, r.event.timeframe,
                    r.event.score, r.event.stage, r.event.layer, r.event.reason,
                    int(r.event.sentinel), r.entry_ts.isoformat() if r.entry_ts else None,
                    r.entry_price, r.end_price, r.return_pct, r.sim_pnl_usdt,
                    r.mfe_pct, r.mae_pct, r.barrier_outcome, r.status,
                    json.dumps(
                        {
                            **r.event.payload,
                            "replay_decision": r.event.replay_decision,
                            "replay_gate": r.event.replay_gate,
                            "replay_fill": r.replay_fill or {},
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
                for r in results
            ],
        )
        conn.commit()


def aggregate(rows: list[Result]) -> dict[str, Any]:
    completed = [row for row in rows if row.status == "complete" and row.sim_pnl_usdt is not None]
    pnl = [float(row.sim_pnl_usdt) for row in completed]
    winners = [value for value in pnl if value > 0]
    fill_summary = replay_fill_summary(completed)
    return {
        "samples": len(completed),
        "win_rate": len(winners) / len(pnl) * 100 if pnl else None,
        "pnl": sum(pnl) if pnl else None,
        "avg_pnl": mean(pnl) if pnl else None,
        "median_pnl": median(pnl) if pnl else None,
        "avg_mfe": mean([float(r.mfe_pct) for r in completed]) if completed else None,
        "avg_mae": mean([float(r.mae_pct) for r in completed]) if completed else None,
        "tp_first": sum(r.barrier_outcome in {"tp_first", "take_profit"} for r in completed),
        "sl_first": sum(r.barrier_outcome in {"sl_first", "both_same_bar_conservative_sl", "stop_loss"} for r in completed),
        "replay_fill": fill_summary,
    }


def fill_float(payload: dict[str, Any], key: str) -> float:
    value = num(payload.get(key), 0.0)
    return float(value or 0.0)


def count_rows(counter: Counter[str]) -> list[dict[str, Any]]:
    return [
        {"name": name, "count": count}
        for name, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    ]


def replay_fill_summary(rows: list[Result]) -> dict[str, Any]:
    fills = [row.replay_fill for row in rows if isinstance(row.replay_fill, dict) and row.replay_fill]
    exit_model_counts: Counter[str] = Counter()
    exit_reason_counts: Counter[str] = Counter()
    by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for fill in fills:
        model = str(fill.get("exit_model") or "unknown")
        reason = str(fill.get("exit_reason") or "unknown")
        exit_model_counts[model] += 1
        exit_reason_counts[reason] += 1
        by_model[model].append(fill)
    by_model_rows = []
    for model, group in sorted(
        by_model.items(),
        key=lambda item: (sum(fill_float(fill, "net_pnl_usdt") for fill in item[1]), item[0]),
        reverse=True,
    ):
        net_values = [fill_float(fill, "net_pnl_usdt") for fill in group]
        by_model_rows.append(
            {
                "exit_model": model,
                "samples": len(group),
                "net_pnl_usdt": sum(net_values),
                "gross_pnl_usdt": sum(fill_float(fill, "gross_pnl_usdt") for fill in group),
                "fee_usdt": sum(fill_float(fill, "fee_usdt") for fill in group),
                "slippage_usdt": sum(fill_float(fill, "slippage_usdt") for fill in group),
                "win_rate": (sum(value > 0 for value in net_values) / len(net_values) * 100) if net_values else None,
                "avg_bars_held": mean([fill_float(fill, "bars_held") for fill in group]) if group else None,
            }
        )
    return {
        "samples": len(fills),
        "exit_model_counts": count_rows(exit_model_counts),
        "exit_reason_counts": count_rows(exit_reason_counts),
        "gross_pnl_usdt": sum(fill_float(fill, "gross_pnl_usdt") for fill in fills) if fills else None,
        "fee_usdt": sum(fill_float(fill, "fee_usdt") for fill in fills) if fills else None,
        "slippage_usdt": sum(fill_float(fill, "slippage_usdt") for fill in fills) if fills else None,
        "net_pnl_usdt": sum(fill_float(fill, "net_pnl_usdt") for fill in fills) if fills else None,
        "avg_bars_held": mean([fill_float(fill, "bars_held") for fill in fills]) if fills else None,
        "by_exit_model": by_model_rows,
    }


def recommendation(metrics: dict[str, Any], min_samples: int) -> str:
    samples = metrics["samples"]
    pnl = metrics["pnl"]
    win_rate = metrics["win_rate"]
    if samples < min_samples or pnl is None or win_rate is None:
        return "样本不足，继续采集"
    if pnl > 0 and win_rate >= 50:
        return "疑似错杀盈利，进入放宽影子试验"
    if pnl > 0:
        return "错失正收益但胜率低，按条件拆分"
    if pnl < 0 and win_rate < 50:
        return "过滤有价值，暂时保留"
    return "结果混合，继续观察"


def score_bucket(score: float | None) -> str:
    if score is None:
        return "unknown"
    if score < 35:
        return "<35"
    if score < 45:
        return "35-44"
    if score < 55:
        return "45-54"
    if score < 65:
        return "55-64"
    return "65+"


def report_markdown(
    events: list[SkipEvent],
    results: list[Result],
    generated_at: datetime,
    since: datetime,
    args: argparse.Namespace,
    errors: list[str],
) -> str:
    rows_by_horizon: dict[int, list[Result]] = defaultdict(list)
    for row in results:
        rows_by_horizon[row.horizon].append(row)
    primary = [row for row in rows_by_horizon[args.primary_horizon] if row.status == "complete"]
    lines = [
        "# OPEN_SKIPPED 反事实评估",
        "",
        f"生成时间: {generated_at.astimezone(CST).strftime('%Y-%m-%d %H:%M:%S')} CST",
        f"评估窗口: {since.astimezone(CST).strftime('%Y-%m-%d %H:%M')} 至 {generated_at.astimezone(CST).strftime('%Y-%m-%d %H:%M')}，最近 {args.hours} 小时",
        "",
        "## 口径",
        "",
        f"- 对已有明确 `symbol/side` 的 `OPEN_SKIPPED`，使用否决后的下一根 1m K 线开盘作为模拟入场价。",
        f"- 模拟仓位统一为保证金 {args.margin_usdt:.0f} USDT、{args.leverage:.0f}x 杠杆；PnL 由 `core.replay_fill` 计算，含默认 5bps 往返手续费模型。",
        f"- `MFE/MAE` 为顺向/逆向最大价格波动；`TP/SL first` 统计共享 fill kernel 的 `{args.tp_pct:.1f}% / {args.sl_pct:.1f}%` 固定障碍。A/v11 事件若带正 ATR 与 15m/30m 周期，会叠加 approved ATR trailing 出场参数。",
        f"- 默认用 {args.primary_horizon} 分钟结果判断过滤层是否错杀；尚未走满窗口的事件只入库为 pending，不进入结论。",
        "- 事件归一使用 `core.replay.ReplayEvent` / `ReplayDecision`，过滤层分组优先采用统一 replay gate，后续会把实盘门控迁到同一路径。",
        "",
        "## 当前结论",
        "",
    ]
    overall = aggregate(primary)
    if overall["samples"]:
        if float(overall["pnl"] or 0) > 0:
            lines.append(
                f"- 被拒信号在 {args.primary_horizon} 分钟基准下整体为正："
                f"{overall['samples']} 个完整样本，若统一放行模拟 PnL {fmt(overall['pnl'], sign=True)} USDT，"
                f"胜率 {fmt(overall['win_rate'])}%。当前过滤层存在错杀盈利机会的证据。"
            )
        else:
            lines.append(
                f"- 被拒信号在 {args.primary_horizon} 分钟基准下整体为负："
                f"{overall['samples']} 个完整样本，若统一放行模拟 PnL {fmt(overall['pnl'], sign=True)} USDT，"
                f"胜率 {fmt(overall['win_rate'])}%。当前过滤整体具有保护价值。"
            )
    else:
        lines.append("- 当前无完整可评估样本。")
    lines.extend(["", "## 各持仓窗口总体结果", "", "| 窗口 | 完整样本 | 假设放行胜率 | 假设放行PnL USDT | 平均PnL | 平均MFE | 平均MAE | 1% TP先到 | 1% SL先到 |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"])
    for horizon in HORIZONS:
        m = aggregate(rows_by_horizon[horizon])
        lines.append(
            f"| {horizon}m | {m['samples']} | {fmt(m['win_rate'])}% | {fmt(m['pnl'], sign=True)} | "
            f"{fmt(m['avg_pnl'], sign=True)} | {fmt(m['avg_mfe'])}% | {fmt(m['avg_mae'])}% | {m['tp_first']} | {m['sl_first']} |"
        )
    fill_summary = overall.get("replay_fill") or {}
    model_rows = fill_summary.get("by_exit_model") or []
    reason_rows = fill_summary.get("exit_reason_counts") or []
    lines.extend(
        [
            "",
            f"## Replay/fill 出场与成本汇总（{args.primary_horizon}m）",
            "",
            f"- 共享 fill 样本: {int(fill_summary.get('samples') or 0)}；"
            f"gross {fmt(fill_summary.get('gross_pnl_usdt'), sign=True)} USDT，"
            f"fee {fmt(fill_summary.get('fee_usdt'))} USDT，"
            f"slippage {fmt(fill_summary.get('slippage_usdt'))} USDT，"
            f"net {fmt(fill_summary.get('net_pnl_usdt'), sign=True)} USDT；"
            f"平均持仓 {fmt(fill_summary.get('avg_bars_held'))} bars。",
            "",
            "| 出场模型 | 样本 | 胜率 | Gross PnL | Fee | Slippage | Net PnL | Avg bars |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in model_rows:
        lines.append(
            f"| {md(row.get('exit_model'))} | {int(row.get('samples') or 0)} | {fmt(row.get('win_rate'))}% | "
            f"{fmt(row.get('gross_pnl_usdt'), sign=True)} | {fmt(row.get('fee_usdt'))} | "
            f"{fmt(row.get('slippage_usdt'))} | {fmt(row.get('net_pnl_usdt'), sign=True)} | {fmt(row.get('avg_bars_held'))} |"
        )
    if not model_rows:
        lines.append("| - | 0 | - | - | - | - | - | - |")
    if reason_rows:
        reason_text = "；".join(f"{md(row.get('name'))}={int(row.get('count') or 0)}" for row in reason_rows[:8])
        lines.extend(["", f"- 出场原因分布: {reason_text}。"])
    lines.extend(["", f"## 按策略判断（{args.primary_horizon}m）", "", "| 策略 | 被拒样本 | 若放行胜率 | 若放行PnL USDT | 平均MFE | 平均MAE | 判断 |", "| --- | ---: | ---: | ---: | ---: | ---: | --- |"])
    for strategy in STRATEGIES:
        m = aggregate([r for r in primary if r.event.strategy == strategy])
        lines.append(
            f"| {strategy} | {m['samples']} | {fmt(m['win_rate'])}% | {fmt(m['pnl'], sign=True)} | "
            f"{fmt(m['avg_mfe'])}% | {fmt(m['avg_mae'])}% | {recommendation(m, args.min_samples)} |"
        )
    grouped: dict[tuple[str, str], list[Result]] = defaultdict(list)
    for row in primary:
        grouped[(row.event.strategy, filter_name(row.event))].append(row)
    ranking = sorted(grouped.items(), key=lambda item: float(aggregate(item[1])["pnl"] or 0), reverse=True)
    lines.extend(["", f"## 过滤层净价值排行（{args.primary_horizon}m）", "", "正 PnL 表示这层挡掉了原本可能赚钱的单；负 PnL 表示它避免了损失。", "", "| 策略 | 过滤层 / 原因 | 样本 | 若放行胜率 | 若放行PnL USDT | 判断 |", "| --- | --- | ---: | ---: | ---: | --- |"])
    for (strategy, name), group in ranking[:30]:
        m = aggregate(group)
        lines.append(
            f"| {strategy} | {md(name)} | {m['samples']} | {fmt(m['win_rate'])}% | "
            f"{fmt(m['pnl'], sign=True)} | {recommendation(m, args.min_samples)} |"
        )
    bucket_groups: dict[tuple[str, str], list[Result]] = defaultdict(list)
    for row in primary:
        bucket_groups[(row.event.strategy, score_bucket(row.event.score))].append(row)
    lines.extend(["", f"## 被拒分数桶（{args.primary_horizon}m）", "", "| 策略 | 分数桶 | 样本 | 若放行胜率 | 若放行PnL USDT | 判断 |", "| --- | --- | ---: | ---: | ---: | --- |"])
    for key, group in sorted(bucket_groups.items()):
        m = aggregate(group)
        lines.append(f"| {key[0]} | {key[1]} | {m['samples']} | {fmt(m['win_rate'])}% | {fmt(m['pnl'], sign=True)} | {recommendation(m, args.min_samples)} |")
    profitable = sorted(primary, key=lambda r: float(r.sim_pnl_usdt or 0), reverse=True)[:15]
    protected = sorted(primary, key=lambda r: float(r.sim_pnl_usdt or 0))[:15]
    lines.extend(["", "## 最大错杀样本", "", "| 时间 | 策略 | 币种 | 方向 | 分数 | 拒绝原因 | 模拟PnL USDT | MFE | MAE |", "| --- | --- | --- | --- | ---: | --- | ---: | ---: | ---: |"])
    for row in profitable:
        event = row.event
        lines.append(
            f"| {event.ts.astimezone(CST).strftime('%m-%d %H:%M')} | {event.strategy} | {event.symbol} | {event.side} | {fmt(event.score, 1)} | "
            f"{md(event.reason)[:44]} | {fmt(row.sim_pnl_usdt, sign=True)} | {fmt(row.mfe_pct)}% | {fmt(row.mae_pct)}% |"
        )
    lines.extend(["", "## 最大有效拦截样本", "", "| 时间 | 策略 | 币种 | 方向 | 分数 | 拒绝原因 | 若放行PnL USDT | MFE | MAE |", "| --- | --- | --- | --- | ---: | --- | ---: | ---: | ---: |"])
    for row in protected:
        event = row.event
        lines.append(
            f"| {event.ts.astimezone(CST).strftime('%m-%d %H:%M')} | {event.strategy} | {event.symbol} | {event.side} | {fmt(event.score, 1)} | "
            f"{md(event.reason)[:44]} | {fmt(row.sim_pnl_usdt, sign=True)} | {fmt(row.mfe_pct)}% | {fmt(row.mae_pct)}% |"
        )
    pending = sum(row.status != "complete" for row in results)
    lines.extend(["", "## 数据覆盖", "", f"- 有方向的 `OPEN_SKIPPED` 事件: {len(events)} 条；结果记录: {len(results)} 条；不完整窗口或缺失 K 线: {pending} 条。"])
    if errors:
        lines.append(f"- K 线请求失败币种: {len(errors)} 个；首条错误: {md(errors[0])}。")
    else:
        lines.append("- K 线拉取无接口错误。")
    lines.append("")
    return "\n".join(lines)


def fallback_html(markdown: str, title: str) -> str:
    return (
        "<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title>"
        "<style>body{font:14px Arial,'Microsoft YaHei';margin:24px;white-space:pre-wrap;"
        "background:#0b1220;color:#e5e7eb}</style>"
        f"<body>{html.escape(markdown)}</body></html>"
    )


def write_json_report(path: Path, events: list[SkipEvent], results: list[Result], args: argparse.Namespace, generated_at: datetime) -> None:
    primary = [row for row in results if row.horizon == args.primary_horizon and row.status == "complete"]
    groups: dict[tuple[str, str], list[Result]] = defaultdict(list)
    for row in primary:
        groups[(row.event.strategy, filter_name(row.event))].append(row)
    payload = {
        "generated_at": generated_at.isoformat(),
        "hours": args.hours,
        "primary_horizon_minutes": args.primary_horizon,
        "replay_schema": "core.replay/v1",
        "fill_schema": "core.replay_fill/v1",
        "events": len(events),
        "complete_primary_samples": len(primary),
        "overall": aggregate(primary),
        "strategies": {strategy: aggregate([r for r in primary if r.event.strategy == strategy]) for strategy in STRATEGIES},
        "filters": [
            {"strategy": key[0], "filter": key[1], "replay_gate": value[0].event.replay_gate if value else "", **aggregate(value)}
            for key, value in sorted(groups.items(), key=lambda item: float(aggregate(item[1])["pnl"] or 0), reverse=True)
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="OPEN_SKIPPED counterfactual evaluation with 1m historical klines.")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--hours", type=int, default=48)
    parser.add_argument("--primary-horizon", type=int, choices=HORIZONS, default=60)
    parser.add_argument("--margin-usdt", type=float, default=100.0)
    parser.add_argument("--leverage", type=float, default=4.0)
    parser.add_argument("--tp-pct", type=float, default=1.0)
    parser.add_argument("--sl-pct", type=float, default=1.0)
    parser.add_argument("--min-samples", type=int, default=10)
    args = parser.parse_args()
    root = args.root.resolve()
    db = args.db or (root / "runtime" / "event_store.sqlite3")
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(UTC)
    since = generated_at - timedelta(hours=args.hours)
    events = load_skip_events(db, since, generated_at)
    bars_by_symbol, errors = grouped_bars(events, generated_at, max(HORIZONS))
    results = [
        evaluate(event, horizon, bars_by_symbol, generated_at, args.margin_usdt, args.leverage, args.tp_pct, args.sl_pct)
        for event in events
        for horizon in HORIZONS
    ]
    store_results(db, results, generated_at.isoformat())
    markdown = report_markdown(events, results, generated_at, since, args, errors)
    md_latest = reports / "counterfactual_open_skips_latest.md"
    html_latest = reports / "counterfactual_open_skips_latest.html"
    json_latest = reports / "counterfactual_open_skips_latest.json"
    dated = reports / f"counterfactual_open_skips_{generated_at.astimezone(CST).strftime('%Y-%m-%d')}.md"
    md_latest.write_text(markdown, encoding="utf-8")
    dated.write_text(markdown, encoding="utf-8")
    title = "OPEN_SKIPPED 反事实评估"
    page = render_markdown_html(markdown, title) if render_markdown_html else fallback_html(markdown, title)
    html_latest.write_text(page, encoding="utf-8")
    write_json_report(json_latest, events, results, args, generated_at)
    primary = aggregate([row for row in results if row.horizon == args.primary_horizon])
    print(
        json.dumps(
            {
                "status": "ok",
                "events": len(events),
                "result_rows": len(results),
                "primary_complete_samples": primary["samples"],
                "primary_win_rate": primary["win_rate"],
                "primary_sim_pnl_usdt": primary["pnl"],
                "kline_errors": len(errors),
                "report": str(md_latest),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
