"""Reusable analytics for daily market reviews."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone, timedelta
from typing import Any

from .models import DecisionRecord, SignalRecord, categorize_decision

CST = timezone(timedelta(hours=8))


DECISION_LABELS = {
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

LAYER_LABELS = {
    "strategy": "策略层",
    "risk": "风控层",
    "execution": "执行层",
    "position": "持仓管理",
    "review": "复盘层",
    "unknown": "未知层",
}

CATEGORY_LAYERS = {
    "opened": "execution",
    "closed": "position",
    "signal_candidate": "strategy",
    "sentinel_no_signal": "strategy",
    "sentinel_strategy_rejected": "strategy",
    "sentinel_score_rejected": "strategy",
    "sentinel_pre_filter_rejected": "strategy",
    "sentinel_analysis_error": "strategy",
    "sentinel_cooldown_rejected": "risk",
    "sentinel_position_rejected": "risk",
    "sentinel_risk_rejected": "risk",
    "sentinel_scanned": "strategy",
    "signal_only": "strategy",
    "pre_filter_no_record": "strategy",
    "confirmation": "strategy",
    "score_threshold": "strategy",
    "position_limit": "risk",
    "side_limit": "risk",
    "sector_limit": "risk",
    "capital_guard": "risk",
    "cooldown": "risk",
    "market_microstructure": "risk",
    "order_failed": "execution",
    "other": "unknown",
}


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00").split(" [")[0]
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST)
        return dt.astimezone(CST)
    except Exception:
        return None


def latest(rows: list[Any], key) -> Any | None:
    if not rows:
        return None
    return sorted(rows, key=lambda row: parse_dt(key(row)) or datetime.min.replace(tzinfo=CST))[-1]


def build_decision_records(strategy_name: str, data: dict[str, list[dict]]) -> list[DecisionRecord]:
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
                score=float(row.get("score") or row.get("raw_score") or 0),
                timeframe=str(row.get("timeframe") or row.get("tf") or ""),
                time=str(row.get("time") or row.get("ts") or ""),
                reason=str(row.get("reason") or ""),
                raw=row,
            )
        )
    if records:
        return records
    for row in data.get("opens", []):
        records.append(DecisionRecord.from_event(strategy_name, row))
    for row in data.get("skips", []):
        records.append(DecisionRecord.from_event(strategy_name, row))
    for row in data.get("failed", []):
        records.append(DecisionRecord.from_event(strategy_name, row))
    for row in data.get("signals", []):
        records.append(DecisionRecord.from_signal(SignalRecord.from_row(strategy_name, row)))
    return records


def best_symbol_decision(strategy_name: str, data: dict[str, list[dict]], symbol: str) -> DecisionRecord:
    records = [r for r in build_decision_records(strategy_name, data) if r.symbol == symbol]
    if records:
        priority = {
            "opened": 0,
            "order_failed": 1,
            "position_limit": 2,
            "side_limit": 3,
            "capital_guard": 4,
            "confirmation": 5,
            "score_threshold": 6,
            "cooldown": 7,
            "signal_only": 8,
            "signal_candidate": 8,
            "sentinel_no_signal": 8,
            "sentinel_strategy_rejected": 8,
            "sentinel_score_rejected": 8,
            "sentinel_pre_filter_rejected": 8,
            "sentinel_cooldown_rejected": 8,
            "sentinel_position_rejected": 8,
            "sentinel_risk_rejected": 8,
            "sentinel_analysis_error": 9,
            "market_microstructure": 9,
            "other": 10,
        }
        records.sort(key=lambda r: (priority.get(r.category, 99), -(parse_dt(r.time) or datetime.min.replace(tzinfo=CST)).timestamp()))
        return records[0]
    return DecisionRecord(
        strategy=strategy_name,
        symbol=symbol,
        status="NO_RECORD",
        category="pre_filter_no_record",
        reason="无信号/无跳过记录，可能被成交量、ATR、扫描名单或前置分数过滤",
    )


def decision_funnel(strategy_name: str, data: dict[str, list[dict]], symbols: list[str] | None = None) -> Counter:
    records = build_decision_records(strategy_name, data)
    if symbols is not None:
        wanted = set(symbols)
        seen = {r.symbol for r in records if r.symbol in wanted}
        records = [r for r in records if r.symbol in wanted]
        for sym in wanted - seen:
            records.append(DecisionRecord(strategy_name, sym, "NO_RECORD", "pre_filter_no_record"))
    return Counter(r.category for r in records)


def decision_layer(category: str) -> str:
    return CATEGORY_LAYERS.get(category, "unknown")


def label_layer(layer: str) -> str:
    return LAYER_LABELS.get(layer, layer)


def layer_funnel(strategy_name: str, data: dict[str, list[dict]], symbols: list[str] | None = None) -> Counter:
    category_counter = decision_funnel(strategy_name, data, symbols)
    counter: Counter = Counter()
    for category, count in category_counter.items():
        counter[decision_layer(category)] += count
    return counter


def summarize_layer_counter(counter: Counter, limit: int = 6) -> str:
    if not counter:
        return "-"
    return " / ".join(f"{label_layer(k)} {v}" for k, v in counter.most_common(limit))


def label_category(category: str) -> str:
    return DECISION_LABELS.get(category, category)


def summarize_counter(counter: Counter, limit: int = 8) -> str:
    if not counter:
        return "-"
    return " / ".join(f"{label_category(k)} {v}" for k, v in counter.most_common(limit))
