"""Build long-term research memory from synced strategy logs.

This script does not touch live strategies. It turns daily review data into:
- cases: loss / reverse / missed big move / hard-stop samples
- hypotheses: generated candidate changes for later shadow experiments
- lessons: human-readable accumulated notes
"""

from __future__ import annotations

import argparse
import json
import sys
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

SCRIPT_DIR = Path(__file__).resolve().parent
if (SCRIPT_DIR / "core").exists():
    ROOT = SCRIPT_DIR
else:
    ROOT = SCRIPT_DIR.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from core.research_memory import (  # noqa: E402
    CandidateHypothesis,
    ResearchCase,
    ensure_memory_tree,
    read_jsonl,
    stable_id,
    write_jsonl,
)
from daily_market_review import (  # noqa: E402
    CST,
    STRATEGIES,
    classify_entry_stage,
    explain_no_open,
    fetch_market_rank,
    in_day,
    is_restored_trade,
    load_strategy_day,
    parse_dt,
    side_is_correct,
    strategy_symbol_status,
    summarize_trades,
    to_float,
    trade_reason,
)

MEMORY_DIR = ROOT / "research_memory"


def direction_for_move(change_pct: float) -> str:
    return "long" if change_pct > 0 else "short"


def hard_stop_reason(text: str) -> bool:
    return any(key in text for key in ("最大亏损", "硬顶", "硬底", "强平", "max loss"))


def case_confidence(case_type: str, pnl_usd: float, move_pct: float, stage: str) -> float:
    base = 0.55
    if case_type in {"reverse_trade", "missed_big_move"}:
        base += min(0.25, abs(move_pct) / 40)
    if pnl_usd < -10:
        base += min(0.15, abs(pnl_usd) / 100)
    if stage and stage not in {"无法分析", "顺势/非反向"}:
        base += 0.08
    return round(min(base, 0.95), 2)


def load_market_moves(date_str: str, data_root: Path, out_dir: Path, market_limit: int | None) -> list[dict[str, Any]]:
    snapshot_paths = [
        data_root / "reports" / "market_snapshot_latest.json",
        data_root / "reports" / f"market_snapshot_{date_str}.json",
        out_dir / "snapshots" / f"market_moves_{date_str}.json",
    ]
    for snapshot_path in snapshot_paths:
        if snapshot_path.exists():
            try:
                payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict) and isinstance(payload.get("moves"), list):
                    return [row for row in payload["moves"] if isinstance(row, dict)]
                if isinstance(payload, list):
                    return [row for row in payload if isinstance(row, dict)]
            except Exception:
                pass
    try:
        moves = fetch_market_rank(date_str, limit_symbols=market_limit)
    except Exception:
        moves = []
    try:
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(json.dumps(moves, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass
    return moves


def lesson_for_loss(stage: dict[str, Any], trade: dict[str, Any]) -> str:
    exit_reason = str(trade.get("exit_reason") or trade.get("reason") or "")
    score = to_float(trade.get("score") or trade.get("entry_score"))
    if hard_stop_reason(exit_reason):
        return "硬顶/最大亏损样本，优先检查入场阶段、止损尾部和方向过滤。"
    if score and score < 60:
        return "低分亏损样本，优先检查阈值、弱确认放行和噪声过滤。"
    if stage.get("stage") and stage.get("stage") != "顺势/非反向":
        return f"{stage.get('stage')}，{stage.get('weakness', '需要强化行情阶段识别')}。"
    return "亏损样本，需结合K线窗口确认是噪声、追尾还是执行离场问题。"


def build_trade_cases(date_str: str, strategy_data: dict[str, dict], moves: list[dict[str, Any]]) -> list[ResearchCase]:
    move_by_symbol = {m["symbol"]: m for m in moves}
    cases: list[ResearchCase] = []
    seq = 0
    for strategy in STRATEGIES:
        name = strategy["name"]
        data = strategy_data[strategy["key"]]
        for trade in data["trades"]:
            if "pnl_usd" not in trade or is_restored_trade(trade):
                continue
            pnl = to_float(trade.get("pnl_usd"))
            symbol = str(trade.get("symbol") or "")
            side = str(trade.get("side") or "").lower()
            move = move_by_symbol.get(symbol, {})
            day_change = to_float(move.get("change_pct"))
            exit_reason = str(trade.get("exit_reason") or trade.get("reason") or "")
            is_reverse = bool(day_change) and not side_is_correct(side, day_change)
            is_loss = pnl < 0
            is_hard = hard_stop_reason(exit_reason)
            if not (is_loss or is_reverse or is_hard):
                continue
            seq += 1
            if move:
                stage = classify_entry_stage(
                    symbol,
                    side,
                    trade.get("entry_time") or trade.get("time"),
                    trade.get("entry_price"),
                    date_str,
                    day_change,
                )
            else:
                stage = {
                    "stage": "无市场快照",
                    "weakness": "仅基于交易日志归因，未做日内K线阶段判断",
                    "detail": "缺少市场涨跌/15m K线快照",
                }
            if is_reverse:
                case_type = "reverse_trade"
            elif is_hard:
                case_type = "hard_stop_loss"
            else:
                case_type = "loss_trade"
            reason = trade_reason(data, trade)
            cases.append(
                ResearchCase(
                    case_id=stable_id("CASE", date_str, name, symbol, case_type, seq),
                    date=date_str,
                    strategy=name,
                    symbol=symbol,
                    case_type=case_type,
                    side=side,
                    expected_direction=direction_for_move(day_change) if day_change else "",
                    actual_action=f"opened_{side}",
                    pnl_usd=round(pnl, 4),
                    pnl_pct=round(to_float(trade.get("pnl_pct")), 4),
                    move_pct=round(day_change, 4),
                    score=round(to_float(trade.get("score") or trade.get("entry_score")), 4),
                    market_stage=str(stage.get("stage") or ""),
                    attribution=str(stage.get("weakness") or ""),
                    decision_category="opened",
                    reason=reason,
                    lesson=lesson_for_loss(stage, trade),
                    confidence=case_confidence(case_type, pnl, day_change, str(stage.get("stage") or "")),
                    source={
                        "entry_time": trade.get("entry_time") or trade.get("time"),
                        "exit_time": trade.get("exit_time") or trade.get("time"),
                        "entry_price": trade.get("entry_price"),
                        "exit_price": trade.get("exit_price"),
                        "exit_reason": exit_reason,
                        "stage_detail": stage.get("detail"),
                    },
                )
            )
    return cases


def build_missed_cases(
    date_str: str,
    strategy_data: dict[str, dict],
    moves: list[dict[str, Any]],
    top_n: int,
    min_abs_move: float,
) -> list[ResearchCase]:
    watched = sorted(moves, key=lambda x: abs(to_float(x.get("change_pct"))), reverse=True)[:top_n]
    cases: list[ResearchCase] = []
    seq = 0
    for move in watched:
        symbol = str(move.get("symbol") or "")
        change_pct = to_float(move.get("change_pct"))
        if abs(change_pct) < min_abs_move:
            continue
        for strategy in STRATEGIES:
            name = strategy["name"]
            data = strategy_data[strategy["key"]]
            status = strategy_symbol_status(data, symbol, change_pct)
            if status.get("opened"):
                continue
            seq += 1
            detail = str(status.get("detail") or explain_no_open(data, symbol))
            category = str(status.get("category") or "unknown")
            lesson = "大行情未开仓，需定位被策略层、风控层还是执行层挡住。"
            if category == "confirmation":
                lesson = "大行情被确认层挡住，适合进入软放行影子实验。"
            elif category in {"position_limit", "side_limit"}:
                lesson = "大行情被仓位限制挡住，需评估满仓替换和资金占用质量。"
            elif category == "score_threshold":
                lesson = "大行情分数未达标，需检查因子权重是否低估趋势启动。"
            cases.append(
                ResearchCase(
                    case_id=stable_id("CASE", date_str, name, symbol, "missed_big_move", seq),
                    date=date_str,
                    strategy=name,
                    symbol=symbol,
                    case_type="missed_big_move",
                    expected_direction=direction_for_move(change_pct),
                    actual_action="skipped",
                    move_pct=round(change_pct, 4),
                    market_stage="大涨跌榜单样本",
                    attribution=detail,
                    decision_category=category,
                    reason=detail,
                    lesson=lesson,
                    confidence=case_confidence("missed_big_move", 0, change_pct, "大涨跌榜单样本"),
                    source={
                        "open": move.get("open"),
                        "close": move.get("close"),
                        "high": move.get("high"),
                        "low": move.get("low"),
                        "amplitude_pct": move.get("amplitude_pct"),
                        "quote_volume": move.get("quote_volume"),
                    },
                )
            )
    return cases


def promotion_gate() -> dict[str, Any]:
    return {
        "min_sample_cases": 20,
        "shadow_pnl_delta_min": 0,
        "hard_stop_not_increase": True,
        "reverse_trade_not_increase": True,
        "max_single_loss_not_worse": True,
    }


def family_id_for(strategy: str, change_type: str, category: str = "") -> str:
    return stable_id("FAM", strategy, change_type, category or "general")


def build_hypotheses(date_str: str, cases: list[ResearchCase]) -> list[CandidateHypothesis]:
    by_key: dict[tuple[str, str, str], list[ResearchCase]] = defaultdict(list)
    for case in cases:
        key = (case.strategy, case.case_type, case.decision_category or case.market_stage)
        by_key[key].append(case)

    candidates: list[CandidateHypothesis] = []
    for (strategy, case_type, category), group in sorted(by_key.items()):
        if len(group) < 2:
            continue
        source_ids = [c.case_id for c in sorted(group, key=lambda c: c.confidence, reverse=True)[:8]]
        if case_type == "missed_big_move" and category == "confirmation":
            candidates.append(
                CandidateHypothesis(
                    candidate_id=stable_id("HYP", date_str, strategy, "confirmation-soft-pass"),
                    date=date_str,
                    strategy=strategy,
                    source_cases=source_ids,
                    problem="大行情样本多次被确认层过滤。",
                    proposal="高分且日内/大盘同向时，进入15m弱确认软放行影子实验，而不是直接实盘放开。",
                    change_type="confirmation_policy",
                    params={"soft_pass_score": 60, "require_direction_alignment": True, "shadow_only": True},
                    risk="可能增加趋势末端追单和短线噪声逆势单。",
                    expected_effect="减少强趋势中段错过，观察硬顶和反向开仓是否增加。",
                    promotion_gate=promotion_gate(),
                    family_id=family_id_for(strategy, "confirmation_policy", "soft-pass"),
                )
            )
        elif case_type in {"reverse_trade", "hard_stop_loss"}:
            candidates.append(
                CandidateHypothesis(
                    candidate_id=stable_id("HYP", date_str, strategy, case_type, "stage-guard"),
                    date=date_str,
                    strategy=strategy,
                    source_cases=source_ids,
                    problem="反向/硬顶样本集中，入场阶段识别不足。",
                    proposal="对强趋势左侧逆势、中段回调误判样本增加阶段保护，仅进入影子过滤实验。",
                    change_type="market_stage_filter",
                    params={"block_reverse_stage": True, "tail_guard": True, "shadow_only": True},
                    risk="可能错过短线反转机会。",
                    expected_effect="降低反向开仓和硬顶尾部亏损。",
                    promotion_gate=promotion_gate(),
                    family_id=family_id_for(strategy, "market_stage_filter", "stage-guard"),
                )
            )
        elif case_type == "missed_big_move" and category in {"position_limit", "side_limit"}:
            candidates.append(
                CandidateHypothesis(
                    candidate_id=stable_id("HYP", date_str, strategy, "replacement-quality"),
                    date=date_str,
                    strategy=strategy,
                    source_cases=source_ids,
                    problem="大行情被持仓/方向限制挡住，仓位表达受限。",
                    proposal="只允许高质量新信号替换低质量弱仓，继续走影子审计。",
                    change_type="replacement_policy",
                    params={"min_score_gap": 25, "protect_profitable_positions": True, "shadow_only": True},
                    risk="替换过频会制造噪声止损。",
                    expected_effect="在不扩大仓位上限的前提下提高大行情捕捉率。",
                    promotion_gate=promotion_gate(),
                    family_id=family_id_for(strategy, "replacement_policy", "quality"),
                )
            )
    return candidates


def write_lessons(memory_dir: Path, date_str: str, cases: list[ResearchCase], candidates: list[CandidateHypothesis]) -> None:
    counts = Counter((c.strategy, c.case_type, c.decision_category) for c in cases)
    lines = [
        f"# 研究经验快照 - {date_str}",
        "",
        f"- 案例数: {len(cases)}",
        f"- 候选假设: {len(candidates)}",
        "",
        "## 高频问题",
    ]
    for (strategy, case_type, category), count in counts.most_common(12):
        lines.append(f"- {strategy} / {case_type} / {category or '-'}: {count}")
    lines += ["", "## 候选假设"]
    for c in candidates:
        lines.append(f"- {c.candidate_id}: {c.problem} -> {c.proposal}")
    (memory_dir / "lessons" / f"daily_lessons_{date_str}.md").write_text("\n".join(lines), encoding="utf-8")

    strategy_lines = [
        f"## {date_str}",
        f"- 案例数: {len(cases)}",
        f"- 候选数: {len(candidates)}",
    ]
    for strategy in STRATEGIES:
        name = strategy["name"]
        s_cases = [c for c in cases if c.strategy == name]
        if not s_cases:
            continue
        by_type = Counter(c.case_type for c in s_cases)
        strategy_lines.append(f"- {name}: " + " / ".join(f"{k} {v}" for k, v in by_type.most_common(6)))
    strategy_path = memory_dir / "lessons" / "strategy_lessons.md"
    existing = strategy_path.read_text(encoding="utf-8") if strategy_path.exists() else "# 策略经验库\n\n"
    strategy_path.write_text(existing.rstrip() + "\n\n" + "\n".join(strategy_lines) + "\n", encoding="utf-8")

    symbol_rows = []
    symbol_bucket: dict[tuple[str, str], list[ResearchCase]] = defaultdict(list)
    factor_bucket: dict[tuple[str, str], list[ResearchCase]] = defaultdict(list)
    for case in cases:
        symbol_bucket[(case.strategy, case.symbol)].append(case)
        factor_bucket[(case.strategy, case.case_type)].append(case)
    for (strategy, symbol), group in sorted(symbol_bucket.items()):
        pnl = sum(c.pnl_usd for c in group)
        symbol_rows.append({
            "date": date_str,
            "strategy": strategy,
            "symbol": symbol,
            "cases": len(group),
            "pnl_usd": round(pnl, 4),
            "reverse_cases": sum(1 for c in group if c.case_type == "reverse_trade"),
            "missed_cases": sum(1 for c in group if c.case_type == "missed_big_move"),
            "hard_stop_cases": sum(1 for c in group if c.case_type == "hard_stop_loss"),
            "lessons": sorted({c.lesson for c in group if c.lesson})[:3],
        })
    write_jsonl(memory_dir / "lessons" / "symbol_lessons.jsonl", symbol_rows)

    factor_rows = []
    for (strategy, case_type), group in sorted(factor_bucket.items()):
        factor_rows.append({
            "date": date_str,
            "strategy": strategy,
            "case_type": case_type,
            "count": len(group),
            "avg_score": round(sum(c.score for c in group) / len(group), 4),
            "avg_move_pct": round(sum(c.move_pct for c in group) / len(group), 4),
            "avg_confidence": round(sum(c.confidence for c in group) / len(group), 4),
            "top_attribution": Counter(c.attribution or c.market_stage for c in group).most_common(1)[0][0] if group else "",
        })
    write_jsonl(memory_dir / "lessons" / "factor_lessons.jsonl", factor_rows)


def build_snapshot(
    memory_dir: Path,
    date_str: str,
    strategy_data: dict[str, dict],
    cases: list[ResearchCase],
    candidates: list[CandidateHypothesis],
) -> None:
    strategies = {}
    for strategy in STRATEGIES:
        data = strategy_data[strategy["key"]]
        all_trades = [t for t in data["trades"] if "pnl_usd" in t]
        attr_trades = [t for t in data["attributed_trades"] if "pnl_usd" in t]
        strategies[strategy["name"]] = {
            "all": summarize_trades(all_trades),
            "attributed": summarize_trades(attr_trades),
            "cases": Counter(c.case_type for c in cases if c.strategy == strategy["name"]),
        }
    snapshot = {
        "date": date_str,
        "generated_at": datetime.now(CST).isoformat(timespec="seconds"),
        "strategies": strategies,
        "case_count": len(cases),
        "candidate_count": len(candidates),
    }
    path = memory_dir / "snapshots" / f"daily_summary_{date_str}.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def split_case_rows(cases: list[ResearchCase]) -> dict[str, list[ResearchCase]]:
    return {
        "loss_cases": [c for c in cases if c.case_type in {"loss_trade", "hard_stop_loss"}],
        "missed_cases": [c for c in cases if c.case_type == "missed_big_move"],
        "reverse_cases": [c for c in cases if c.case_type == "reverse_trade"],
        "big_move_cases": [c for c in cases if c.case_type == "missed_big_move" or abs(c.move_pct) >= 15],
    }


def run(
    date_str: str,
    data_root: Path,
    out_dir: Path,
    top_n: int,
    min_abs_move: float,
    market_limit: int | None,
) -> tuple[list[ResearchCase], list[CandidateHypothesis]]:
    ensure_memory_tree(out_dir)
    moves = load_market_moves(date_str, data_root, out_dir, market_limit)
    strategy_paths = []
    for strategy in STRATEGIES:
        copied = dict(strategy)
        copied["events"] = data_root / Path(strategy["events"]).relative_to(ROOT)
        copied["trades"] = data_root / Path(strategy["trades"]).relative_to(ROOT)
        copied["signals"] = data_root / Path(strategy["signals"]).relative_to(ROOT)
        copied["decisions"] = data_root / Path(strategy["decisions"]).relative_to(ROOT)
        strategy_paths.append(copied)
    strategy_data = {s["key"]: load_strategy_day(s, date_str) for s in strategy_paths}

    cases = build_trade_cases(date_str, strategy_data, moves)
    cases.extend(build_missed_cases(date_str, strategy_data, moves, top_n, min_abs_move))
    candidates = build_hypotheses(date_str, cases)

    write_jsonl(out_dir / "cases" / f"cases_{date_str}.jsonl", [c.to_dict() for c in cases])
    for name, rows in split_case_rows(cases).items():
        write_jsonl(out_dir / "cases" / f"{name}_{date_str}.jsonl", [c.to_dict() for c in rows])
    write_jsonl(out_dir / "hypotheses" / f"candidates_{date_str}.jsonl", [c.to_dict() for c in candidates])
    write_jsonl(out_dir / "hypotheses" / "candidates_latest.jsonl", [c.to_dict() for c in candidates])
    write_lessons(out_dir, date_str, cases, candidates)
    build_snapshot(out_dir, date_str, strategy_data, cases, candidates)
    return cases, candidates


def default_date() -> str:
    return (datetime.now(CST) - timedelta(days=1)).strftime("%Y-%m-%d")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建长期研究记忆/经验库")
    parser.add_argument("--date", default=default_date(), help="复盘日期 YYYY-MM-DD，默认昨日")
    parser.add_argument("--data-root", default=str(ROOT), help="日志根目录，支持 server_logs_tencent")
    parser.add_argument("--out-dir", default=str(MEMORY_DIR), help="研究记忆输出目录")
    parser.add_argument("--top", type=int, default=30, help="大行情榜单样本数")
    parser.add_argument("--min-abs-move", type=float, default=8.0, help="大行情最小绝对涨跌幅")
    parser.add_argument("--market-limit", type=int, default=180, help="限制抓取前N个USDT永续合约；0表示全量")
    args = parser.parse_args(argv)

    market_limit = args.market_limit if args.market_limit > 0 else None
    cases, candidates = run(args.date, Path(args.data_root), Path(args.out_dir), args.top, args.min_abs_move, market_limit)
    print(f"研究记忆已生成: {args.out_dir}")
    print(f"cases={len(cases)} candidates={len(candidates)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
