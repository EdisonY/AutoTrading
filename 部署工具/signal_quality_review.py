"""Signal quality baseline report for A/v11, B/v16, and C/v14.

The daily review answers "what happened yesterday".  This report answers a
slightly different question: which signals are worth trusting, which filters
are blocking useful trades, and where false positives are concentrated.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
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
if (SCRIPT_DIR / "core").exists():
    ROOT = SCRIPT_DIR
else:
    ROOT = SCRIPT_DIR.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from core.models import DecisionRecord, SignalRecord, categorize_decision  # noqa: E402
from daily_market_review import render_markdown_html  # noqa: E402

CST = timezone(timedelta(hours=8))
REPORTS_DIR = ROOT / "reports"
REPORTS_DIR.mkdir(exist_ok=True)

def strategy_paths(data_root: Path) -> list[dict[str, Any]]:
    return [
        {
            "key": "A",
            "name": "A/v11",
            "events": data_root / "scanner_data" / "events.jsonl",
            "trades": data_root / "scanner_data" / "trades.jsonl",
            "signals": data_root / "logs" / "signals.jsonl",
            "decisions": data_root / "logs" / "decisions.jsonl",
        },
        {
            "key": "B",
            "name": "B/v16",
            "events": data_root / "scanner_data_v16" / "events.jsonl",
            "trades": data_root / "scanner_data_v16" / "trades.jsonl",
            "signals": data_root / "logs_v16" / "signals.jsonl",
            "decisions": data_root / "logs_v16" / "decisions.jsonl",
        },
        {
            "key": "C",
            "name": "C/v14",
            "events": data_root / "scanner_data_v14" / "events.jsonl",
            "trades": data_root / "scanner_data_v14" / "trades.jsonl",
            "signals": data_root / "logs_v14" / "signals.jsonl",
            "decisions": data_root / "logs_v14" / "decisions.jsonl",
        },
    ]

CATEGORY_LABELS = {
    "opened": "已开仓",
    "closed": "已平仓",
    "signal_candidate": "候选信号",
    "sentinel_no_signal": "哨兵扫描无信号",
    "sentinel_strategy_rejected": "哨兵策略层否决",
    "sentinel_score_rejected": "哨兵分数/阈值否决",
    "sentinel_cooldown_rejected": "哨兵冷却否决",
    "sentinel_pre_filter_rejected": "哨兵前置过滤",
    "sentinel_position_rejected": "哨兵持仓限制",
    "sentinel_risk_rejected": "哨兵风控否决",
    "sentinel_analysis_error": "哨兵分析异常",
    "sentinel_scanned": "哨兵已扫描",
    "position_limit": "总持仓限制",
    "side_limit": "方向持仓限制",
    "sector_limit": "赛道限制",
    "capital_guard": "可用余额保护",
    "confirmation": "确认周期过滤",
    "score_threshold": "分数/阈值未达",
    "cooldown": "止损冷却",
    "market_microstructure": "ATR/数量/交易约束",
    "order_failed": "下单失败",
    "signal_only": "有信号未开",
    "pre_filter_no_record": "前置过滤无记录",
    "other": "其他",
}

CATEGORY_LAYERS = {
    "opened": "执行层",
    "closed": "持仓管理",
    "signal_candidate": "策略层",
    "sentinel_no_signal": "策略层",
    "sentinel_strategy_rejected": "策略层",
    "sentinel_score_rejected": "策略层",
    "sentinel_pre_filter_rejected": "策略层",
    "sentinel_analysis_error": "策略层",
    "sentinel_cooldown_rejected": "风控层",
    "sentinel_position_rejected": "风控层",
    "sentinel_risk_rejected": "风控层",
    "sentinel_scanned": "策略层",
    "signal_only": "策略层",
    "pre_filter_no_record": "策略层",
    "confirmation": "策略层",
    "score_threshold": "策略层",
    "position_limit": "风控层",
    "side_limit": "风控层",
    "sector_limit": "风控层",
    "capital_guard": "风控层",
    "cooldown": "风控层",
    "market_microstructure": "风控层",
    "order_failed": "执行层",
    "other": "未知层",
}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                item = json.loads(line)
            except Exception:
                continue
            if isinstance(item, dict):
                rows.append(item)
    return rows


def read_jsonl_days(path: Path, days: set[str], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    shard_dir = path.parent / path.stem
    if shard_dir.exists():
        for day in sorted(days):
            rows.extend(read_jsonl(shard_dir / f"{day}.jsonl"))
        if rows:
            return [r for r in rows if in_days(r, days, fields)]
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict) and in_days(row, days, fields):
                rows.append(row)
    return rows


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00").split(" [")[0]
    for candidate in (text, text[:26], text[:19]):
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=CST)
            return dt.astimezone(CST)
        except Exception:
            pass
    return None


def in_days(row: dict[str, Any], days: set[str], fields: tuple[str, ...]) -> bool:
    for field in fields:
        dt = parse_dt(row.get(field))
        if dt and dt.strftime("%Y-%m-%d") in days:
            return True
    return False


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def md_cell(value: Any) -> str:
    return str(value if value is not None else "-").replace("|", "/").replace("\n", " ").strip()


def label_category(category: str) -> str:
    return CATEGORY_LABELS.get(category, category or "其他")


def summarize_counter(counter: Counter, limit: int = 8) -> str:
    if not counter:
        return "-"
    return " / ".join(f"{label_category(str(k))} {v}" for k, v in counter.most_common(limit))


def summarize_layer_counter(counter: Counter, limit: int = 6) -> str:
    if not counter:
        return "-"
    return " / ".join(f"{k} {v}" for k, v in counter.most_common(limit))


def fmt_num(value: Any, digits: int = 2, suffix: str = "") -> str:
    try:
        num = float(value)
    except Exception:
        return "-"
    return f"{num:,.{digits}f}{suffix}"


def fmt_signed(value: Any, digits: int = 2, suffix: str = "%") -> str:
    try:
        num = float(value)
    except Exception:
        return "-"
    return f"{num:+.{digits}f}{suffix}"


def compact_time(value: Any) -> str:
    dt = parse_dt(value)
    if not dt:
        return md_cell(value or "-")
    return dt.strftime("%m-%d %H:%M:%S")


def sentinel_reason_text(record: DecisionRecord) -> str:
    raw = record.raw
    nested = raw.get("raw") if isinstance(raw.get("raw"), dict) else {}
    reason = raw.get("sentinel_reason") or nested.get("sentinel_reason") or "-"
    change = fmt_signed(raw.get("sentinel_change_pct", nested.get("sentinel_change_pct")), 2, "%")
    velocity = fmt_signed(raw.get("sentinel_velocity_pct", nested.get("sentinel_velocity_pct")), 2, "pct")
    qv = fmt_num(to_float(raw.get("sentinel_quote_volume", nested.get("sentinel_quote_volume"))) / 1e6, 1, "M")
    vd = fmt_num(to_float(raw.get("sentinel_volume_delta", nested.get("sentinel_volume_delta"))) / 1e6, 2, "M")
    return f"{reason}; 涨跌{change}; 加速{velocity}; 额{qv}; 增量{vd}"


def decision_stage_text(record: DecisionRecord) -> str:
    raw = record.raw
    nested = raw.get("raw") if isinstance(raw.get("raw"), dict) else {}
    stage = raw.get("decision_stage") or nested.get("decision_stage") or "-"
    layer = raw.get("filter_layer") or nested.get("filter_layer") or CATEGORY_LAYERS.get(record.category, "-")
    result = raw.get("sentinel_scan_result") or nested.get("sentinel_scan_result") or record.category
    return f"{stage}/{layer}/{result}"


def decision_reason_text(record: DecisionRecord) -> str:
    raw = record.raw
    reason = (
        record.reason
        or raw.get("skip_reason")
        or (raw.get("raw") if isinstance(raw.get("raw"), dict) else {}).get("skip_reason")
        or raw.get("confirm_reason")
        or (raw.get("raw") if isinstance(raw.get("raw"), dict) else {}).get("confirm_reason")
        or raw.get("entry_reason")
        or (raw.get("raw") if isinstance(raw.get("raw"), dict) else {}).get("entry_reason")
        or raw.get("reason")
        or (raw.get("raw") if isinstance(raw.get("raw"), dict) else {}).get("reason")
        or "-"
    )
    return md_cell(reason)


def sentinel_priority(record: DecisionRecord) -> tuple[int, float]:
    priority = {
        "opened": 0,
        "order_failed": 1,
        "signal_candidate": 2,
        "confirmation": 3,
        "score_threshold": 4,
        "sentinel_score_rejected": 4,
        "sentinel_strategy_rejected": 5,
        "sentinel_no_signal": 6,
        "sentinel_pre_filter_rejected": 7,
        "sentinel_analysis_error": 8,
    }
    ts = parse_dt(record.time)
    stamp = (ts or datetime.min.replace(tzinfo=CST)).timestamp()
    return (priority.get(record.category, 9), -stamp)


def date_range(start: datetime, end: datetime) -> list[str]:
    start_d = start.astimezone(CST).date()
    end_d = end.astimezone(CST).date()
    days: list[str] = []
    cur = start_d
    while cur <= end_d:
        days.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return days


def is_restored_trade(trade: dict[str, Any]) -> bool:
    entry_time = str(trade.get("entry_time", ""))
    reason = str(trade.get("reason") or trade.get("entry_reason") or "")
    order_id = str(trade.get("order_id") or trade.get("okx_ord_id") or "")
    return (
        "[恢复]" in entry_time
        or "[鎭㈠]" in entry_time
        or "恢复" in reason
        or "鎭㈠" in reason
        or order_id == "restored"
    )


def bucket_score(score: float) -> str:
    score = abs(score)
    if score <= 20:
        return "<=20"
    if score <= 40:
        return "21-40"
    if score <= 60:
        return "41-60"
    if score <= 80:
        return "61-80"
    return "81+"


def summarize_trades(trades: list[dict[str, Any]]) -> dict[str, Any]:
    pnl = sum(to_float(t.get("pnl_usd")) for t in trades)
    wins = sum(1 for t in trades if to_float(t.get("pnl_usd")) > 0)
    losses = sum(1 for t in trades if to_float(t.get("pnl_usd")) < 0)
    hard = sum(
        1
        for t in trades
        if any(x in str(t.get("exit_reason") or t.get("reason") or "") for x in ("最大亏损", "硬顶", "强平"))
    )
    return {
        "trades": len(trades),
        "pnl": pnl,
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(trades) * 100 if trades else 0.0,
        "hard_stop_rate": hard / len(trades) * 100 if trades else 0.0,
    }


def event_category(row: dict[str, Any]) -> str:
    event = str(row.get("event") or row.get("status") or "").upper()
    reason = str(row.get("skip_reason") or row.get("reason") or row.get("msg") or "")
    if row.get("category"):
        return str(row["category"])
    if event in {"CLOSE", "FORCED_CLOSE"}:
        return "closed"
    return categorize_decision(event, reason)


def build_decision_records(strategy_name: str, data: dict[str, list[dict[str, Any]]]) -> list[DecisionRecord]:
    records: list[DecisionRecord] = []
    for row in data.get("decisions", []):
        status = str(row.get("status") or row.get("event") or "").upper()
        category = str(row.get("category") or categorize_decision(status, str(row.get("reason") or "")))
        records.append(
            DecisionRecord(
                strategy=str(row.get("strategy") or strategy_name),
                symbol=str(row.get("symbol") or ""),
                status=status,
                category=category,
                side=str(row.get("side") or "").lower(),
                score=to_float(row.get("score") or row.get("raw_score")),
                timeframe=str(row.get("timeframe") or row.get("tf") or ""),
                time=str(row.get("time") or row.get("ts") or ""),
                reason=str(row.get("reason") or ""),
                raw=row,
            )
        )
    if records:
        return records

    for row in data.get("opens", []) + data.get("skips", []) + data.get("failed", []) + data.get("closes", []):
        category = event_category(row)
        records.append(
            DecisionRecord(
                strategy=strategy_name,
                symbol=str(row.get("symbol") or ""),
                status=str(row.get("event") or "").upper(),
                category=category,
                side=str(row.get("side") or "").lower(),
                score=to_float(row.get("score") or row.get("raw_score")),
                timeframe=str(row.get("timeframe") or row.get("tf") or ""),
                time=str(row.get("time") or row.get("ts") or ""),
                reason=str(row.get("skip_reason") or row.get("reason") or row.get("msg") or ""),
                raw=row,
            )
        )
    for row in data.get("signals", []):
        records.append(DecisionRecord.from_signal(SignalRecord.from_row(strategy_name, row)))
    return records


def decision_funnel(strategy_name: str, data: dict[str, list[dict[str, Any]]]) -> Counter:
    return Counter(r.category for r in build_decision_records(strategy_name, data))


def layer_funnel(strategy_name: str, data: dict[str, list[dict[str, Any]]]) -> Counter:
    layer_counter: Counter = Counter()
    for category, count in decision_funnel(strategy_name, data).items():
        layer_counter[CATEGORY_LAYERS.get(str(category), "未知层")] += count
    return layer_counter


def sentinel_decisions(strategy_name: str, data: dict[str, list[dict[str, Any]]]) -> list[DecisionRecord]:
    return [
        record for record in build_decision_records(strategy_name, data)
        if record.raw.get("sentinel") or str(record.category).startswith("sentinel_")
    ]


def sentinel_action_records(strategy_name: str, data: dict[str, list[dict[str, Any]]]) -> list[DecisionRecord]:
    records = sentinel_decisions(strategy_name, data)
    return [
        r for r in records
        if r.category in {"opened", "order_failed", "signal_candidate", "confirmation", "score_threshold"}
        or r.status in {"OPEN", "OPEN_FAILED", "SIGNAL", "OPEN_SKIPPED"}
    ]


def load_strategy_range(strategy: dict[str, Any], days: list[str]) -> dict[str, list[dict[str, Any]]]:
    day_set = set(days)
    events = read_jsonl_days(strategy["events"], day_set, ("time", "ts"))
    trades = read_jsonl_days(strategy["trades"], day_set, ("exit_time", "time"))
    signals = read_jsonl_days(strategy["signals"], day_set, ("ts", "time"))
    decisions = read_jsonl_days(strategy["decisions"], day_set, ("ts", "time"))
    return {
        "events": events,
        "trades": trades,
        "attributed_trades": [t for t in trades if not is_restored_trade(t)],
        "restored_trades": [t for t in trades if is_restored_trade(t)],
        "signals": signals,
        "decisions": decisions,
        "opens": [e for e in events if e.get("event") == "OPEN"],
        "skips": [e for e in events if e.get("event") == "OPEN_SKIPPED"],
        "failed": [e for e in events if e.get("event") == "OPEN_FAILED"],
        "closes": [e for e in events if e.get("event") in ("CLOSE", "FORCED_CLOSE")],
    }


@dataclass(slots=True)
class MatchedTrade:
    trade: dict[str, Any]
    signal: dict[str, Any] | None
    score: float
    reasons: str
    entry_time: datetime | None


def signal_score(row: dict[str, Any]) -> float:
    return to_float(row.get("net_score", row.get("score", row.get("vpb_score", 0))))


def signal_reasons(row: dict[str, Any], side: str) -> str:
    reasons = row.get("reasons") or row.get(f"reasons_{side}") or row.get("reason") or []
    if isinstance(reasons, list):
        return "+".join(str(x) for x in reasons[:5]) or "-"
    return str(reasons or "-")


def match_trades_to_signals(signals: list[dict[str, Any]], trades: list[dict[str, Any]]) -> list[MatchedTrade]:
    by_key: dict[tuple[str, str], list[tuple[datetime | None, dict[str, Any]]]] = defaultdict(list)
    for sig in signals:
        side = str(sig.get("trade_side") or sig.get("side") or "").lower()
        by_key[(str(sig.get("symbol") or ""), side)].append((parse_dt(sig.get("time") or sig.get("ts")), sig))
    for key in by_key:
        by_key[key].sort(key=lambda item: item[0] or datetime.min.replace(tzinfo=CST))

    matched: list[MatchedTrade] = []
    for trade in trades:
        entry_dt = parse_dt(trade.get("entry_time") or trade.get("time"))
        side = str(trade.get("side") or "").lower()
        key = (str(trade.get("symbol") or ""), side)
        chosen: dict[str, Any] | None = None
        if entry_dt and key in by_key:
            for sig_dt, sig in reversed(by_key[key]):
                if sig_dt and sig_dt <= entry_dt + timedelta(hours=24):
                    chosen = sig
                    break
        if chosen:
            score = signal_score(chosen)
            reasons = signal_reasons(chosen, side)
        else:
            score = to_float(trade.get("score") or trade.get("entry_score"))
            reasons = str(trade.get("entry_reason") or trade.get("reason") or "-")
        matched.append(MatchedTrade(trade, chosen, score, reasons, entry_dt))
    return matched


def build_report(
    days: list[str],
    strategies: list[dict[str, Any]],
    strategy_data: dict[str, dict[str, list[dict[str, Any]]]],
) -> str:
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    total_signals = sum(len(strategy_data[s["name"]]["signals"]) for s in strategies)
    total_trades = sum(len(strategy_data[s["name"]]["trades"]) for s in strategies)
    lines = [
        f"# 三策略信号质量基线 - {days[0]} ~ {days[-1]}",
        "",
        f"生成时间: {now}",
        "口径: 汇总 signals、decisions、events、trades 四层日志，重点看信号质量、转化漏斗、误判来源和可优化方向。",
    ]
    if total_signals == 0 and total_trades == 0:
        lines += [
            "",
            "> 当前范围没有读取到可用 signals / trades 样本。这通常代表本地镜像还没有同步远端最新日志，或日志目录尚未挂载。",
            "> 因此本报告只能证明基线管道已跑通，不能代表策略真实表现。",
        ]

    lines += [
        "",
        "## 一、总览",
        "",
        "| 策略 | 信号数 | 开仓数 | 跳过数 | 失败数 | 平仓数 | 胜率 | 全量PnL | 剔除恢复仓PnL | 硬顶率 |",
        "|------|--------|--------|--------|--------|--------|------|---------|---------------|--------|",
    ]
    for strategy in strategies:
        data = strategy_data[strategy["name"]]
        all_trades = [t for t in data["trades"] if "pnl_usd" in t]
        attr_trades = [t for t in all_trades if not is_restored_trade(t)]
        s_all = summarize_trades(all_trades)
        s_attr = summarize_trades(attr_trades)
        lines.append(
            f"| {strategy['name']} | {len(data['signals'])} | {len(data['opens'])} | {len(data['skips'])} | "
            f"{len(data['failed'])} | {len(all_trades)} | {s_all['win_rate']:.1f}% | "
            f"{s_all['pnl']:+.2f} | {s_attr['pnl']:+.2f} | {s_all['hard_stop_rate']:.1f}% |"
        )

    lines += [
        "",
        "## 二、决策漏斗",
        "",
        "| 策略 | 全量决策漏斗 | 层级漏斗 | 观察重点 |",
        "|------|--------------|----------|----------|",
    ]
    for strategy in strategies:
        data = strategy_data[strategy["name"]]
        funnel = decision_funnel(strategy["name"], data)
        layers = layer_funnel(strategy["name"], data)
        if funnel.get("confirmation", 0) > funnel.get("opened", 0):
            focus = "确认层偏严或候选质量不足"
        elif funnel.get("position_limit", 0) or funnel.get("side_limit", 0):
            focus = "持仓/方向限制影响表达"
        elif funnel.get("order_failed", 0):
            focus = "执行层失败需要排查"
        elif funnel.get("signal_only", 0) and not funnel.get("opened", 0):
            focus = "有信号但未形成开仓链路"
        else:
            focus = "样本继续积累"
        lines.append(f"| {strategy['name']} | {summarize_counter(funnel)} | {summarize_layer_counter(layers)} | {focus} |")

    lines += [
        "",
        "## 三、哨兵扫描追踪",
        "",
        "| 策略 | 哨兵决策数 | 哨兵结果分布 | 哨兵层级分布 | 代表币种 |",
        "|------|------------|--------------|--------------|----------|",
    ]
    for strategy in strategies:
        data = strategy_data[strategy["name"]]
        records = sentinel_decisions(strategy["name"], data)
        category_counter = Counter(r.category for r in records)
        layer_counter: Counter = Counter()
        symbol_counter: Counter = Counter()
        for record in records:
            layer_counter[CATEGORY_LAYERS.get(record.category, "未知层")] += 1
            if record.symbol:
                label = record.symbol
                reason = record.raw.get("sentinel_reason")
                if reason:
                    label = f"{label}({reason})"
                symbol_counter[label] += 1
        examples = " / ".join(f"{sym} {count}" for sym, count in symbol_counter.most_common(5)) or "-"
        lines.append(
            f"| {strategy['name']} | {len(records)} | {summarize_counter(category_counter)} | "
            f"{summarize_layer_counter(layer_counter)} | {examples} |"
        )

    lines += [
        "",
        "### 哨兵开仓/候选链路",
        "",
        "| 时间 | 策略 | 币种 | 周期 | 方向 | 分数 | 哨兵上下文 | 阶段/层级/结果 | 结论原因 |",
        "|------|------|------|------|------|------:|------------|----------------|----------|",
    ]
    action_rows: list[DecisionRecord] = []
    for strategy in strategies:
        action_rows.extend(sentinel_action_records(strategy["name"], strategy_data[strategy["name"]]))
    action_rows.sort(key=sentinel_priority)
    for record in action_rows[:40]:
        lines.append(
            f"| {compact_time(record.time)} | {record.strategy} | {record.symbol} | {record.timeframe or '-'} | "
            f"{record.side or '-'} | {record.score:+.1f} | {md_cell(sentinel_reason_text(record))} | "
            f"{md_cell(decision_stage_text(record))} | {decision_reason_text(record)} |"
        )
    if not action_rows:
        lines.append("| - | - | - | - | - | - | - | - | 当前窗口没有哨兵候选/开仓链路 |")

    lines += [
        "",
        "### 哨兵逐币明细",
        "",
        "| 时间 | 策略 | 币种 | 周期 | 方向 | 分数 | 分类 | 哨兵上下文 | 阶段/层级/结果 | 结论原因 |",
        "|------|------|------|------|------|------:|------|------------|----------------|----------|",
    ]
    detail_rows: list[DecisionRecord] = []
    for strategy in strategies:
        rows = sentinel_decisions(strategy["name"], strategy_data[strategy["name"]])
        rows.sort(key=lambda r: parse_dt(r.time) or datetime.min.replace(tzinfo=CST), reverse=True)
        detail_rows.extend(rows[:25])
    detail_rows.sort(key=lambda r: parse_dt(r.time) or datetime.min.replace(tzinfo=CST), reverse=True)
    for record in detail_rows[:75]:
        lines.append(
            f"| {compact_time(record.time)} | {record.strategy} | {record.symbol} | {record.timeframe or '-'} | "
            f"{record.side or '-'} | {record.score:+.1f} | {label_category(record.category)} | "
            f"{md_cell(sentinel_reason_text(record))} | {md_cell(decision_stage_text(record))} | "
            f"{decision_reason_text(record)} |"
        )
    if not detail_rows:
        lines.append("| - | - | - | - | - | - | - | - | - | 当前窗口没有哨兵扫描明细 |")

    lines += [
        "",
        "## 四、分数段表现",
        "",
        "| 策略 | 分数段 | 对应平仓数 | 胜率 | PnL | 平均PnL | 解释 |",
        "|------|--------|------------|------|-----|---------|------|",
    ]
    for strategy in strategies:
        data = strategy_data[strategy["name"]]
        trades = [t for t in data["trades"] if "pnl_usd" in t and not is_restored_trade(t)]
        matched = match_trades_to_signals(data["signals"], trades)
        bucket_map: dict[str, list[MatchedTrade]] = defaultdict(list)
        for item in matched:
            bucket_map[bucket_score(item.score)].append(item)
        for bucket in ["<=20", "21-40", "41-60", "61-80", "81+"]:
            rows = bucket_map.get(bucket, [])
            s = summarize_trades([x.trade for x in rows])
            avg = s["pnl"] / s["trades"] if s["trades"] else 0.0
            note = "低分仍开仓需重点看" if bucket in {"<=20", "21-40"} and rows else "继续积累"
            if bucket in {"61-80", "81+"} and rows and s["pnl"] < 0:
                note = "高分亏损说明权重/场景识别有问题"
            lines.append(f"| {strategy['name']} | {bucket} | {s['trades']} | {s['win_rate']:.1f}% | {s['pnl']:+.2f} | {avg:+.2f} | {note} |")

    lines += ["", "## 五、信号原因聚类", ""]
    for strategy in strategies:
        data = strategy_data[strategy["name"]]
        trades = [t for t in data["trades"] if "pnl_usd" in t and not is_restored_trade(t)]
        matched = match_trades_to_signals(data["signals"], trades)
        win_reasons: Counter = Counter()
        loss_reasons: Counter = Counter()
        skip_reasons: Counter = Counter(str(row.get("skip_reason") or row.get("reason") or "-") for row in data["skips"])
        for item in matched:
            if to_float(item.trade.get("pnl_usd")) > 0:
                win_reasons[item.reasons] += 1
            else:
                loss_reasons[item.reasons] += 1
        lines += [f"### {strategy['name']}", ""]
        lines.append("胜单常见原因:")
        lines.extend(f"- {md_cell(reason)}: {count}" for reason, count in win_reasons.most_common(8))
        if not win_reasons:
            lines.append("- 暂无样本")
        lines += ["", "亏单常见原因:"]
        lines.extend(f"- {md_cell(reason)}: {count}" for reason, count in loss_reasons.most_common(8))
        if not loss_reasons:
            lines.append("- 暂无样本")
        lines += ["", "跳过原因:"]
        lines.extend(f"- {md_cell(reason)}: {count}" for reason, count in skip_reasons.most_common(8))
        if not skip_reasons:
            lines.append("- 暂无样本")
        lines.append("")

    lines += [
        "## 六、典型误判样本",
        "",
        "| 策略 | 币种 | 方向 | 信号分数 | PnL | 入场原因 | 离场原因 | 初步归因 |",
        "|------|------|------|----------|-----|----------|----------|----------|",
    ]
    for strategy in strategies:
        data = strategy_data[strategy["name"]]
        trades = [t for t in data["trades"] if "pnl_usd" in t and not is_restored_trade(t)]
        matched = sorted(match_trades_to_signals(data["signals"], trades), key=lambda x: to_float(x.trade.get("pnl_usd")))[:5]
        for item in matched:
            trade = item.trade
            pnl = to_float(trade.get("pnl_usd"))
            exit_reason = str(trade.get("exit_reason") or trade.get("reason") or "-")
            if "硬顶" in exit_reason or "最大亏损" in exit_reason:
                cause = "硬顶尾部: 方向/入场阶段错后需要复核"
            elif item.score >= 60 and pnl < 0:
                cause = "高分误判: 因子权重或行情状态过滤不足"
            elif item.score <= 40 and pnl < 0:
                cause = "低质开仓: 阈值或低分放行规则需要收紧"
            else:
                cause = "需要结合K线窗口复核"
            lines.append(
                f"| {strategy['name']} | {trade.get('symbol','-')} | {trade.get('side','-')} | {item.score:+.1f} | "
                f"{pnl:+.2f} | {md_cell(item.reasons)} | {md_cell(exit_reason)} | {cause} |"
            )

    lines += [
        "",
        "## 七、下一步校准顺序",
        "",
        "1. 先补样本: 每天同步远端 signals / decisions / events / trades，日报和信号基线使用同一批日志。",
        "2. 再分层: 把亏单按强趋势逆势、回调左侧、震荡噪音、执行失败、硬顶尾部五类标注。",
        "3. 后调参: 只对明确问题做小步修改，优先减少明显错单，不追求单纯增加开仓数。",
        "4. 最后回放: 每个改动用同一日期窗口重跑报告，对比剔除恢复仓PnL、当前浮盈、硬顶率和错失大行情数量。",
        "",
        "*由 signal_quality_review.py 自动生成。*",
    ]
    return "\n".join(lines)


def run(start: str | None, end: str | None, days_back: int, data_root: Path) -> Path:
    if end:
        end_dt = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=CST)
    else:
        end_dt = (datetime.now(CST) - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if start:
        start_dt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=CST)
    else:
        start_dt = end_dt - timedelta(days=max(1, days_back - 1))

    days = date_range(start_dt, end_dt)
    strategies = strategy_paths(data_root)
    strategy_data = {s["name"]: load_strategy_range(s, days) for s in strategies}
    content = build_report(days, strategies, strategy_data)
    name = f"signal_quality_{days[0]}_to_{days[-1]}"
    md_path = REPORTS_DIR / f"{name}.md"
    html_path = REPORTS_DIR / f"{name}.html"
    md_path.write_text(content, encoding="utf-8")
    html_path.write_text(render_markdown_html(content, f"三策略信号质量基线 - {days[0]} ~ {days[-1]}"), encoding="utf-8")
    latest_html = REPORTS_DIR / "signal_quality_latest.html"
    latest_html.write_text(html_path.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"报告已生成: {md_path}")
    print(f"HTML已生成: {html_path}")
    return md_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="三策略信号质量基线报表")
    parser.add_argument("--start", default=None, help="开始日期 YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="结束日期 YYYY-MM-DD，默认昨日")
    parser.add_argument("--days", type=int, default=14, help="未指定 start 时回看天数，默认 14")
    parser.add_argument("--data-root", default=str(ROOT), help="日志根目录，默认项目根目录")
    args = parser.parse_args(argv)
    run(args.start, args.end, args.days, Path(args.data_root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
