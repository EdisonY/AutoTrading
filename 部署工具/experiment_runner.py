"""Run offline shadow experiments from synced strategy logs."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
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

from core.experiment import (  # noqa: E402
    ExperimentResult,
    ExperimentSpec,
    append_jsonl,
    default_experiments,
    is_hard_stop,
    read_jsonl,
    to_float,
    win_rate,
    write_jsonl,
)
from core.research_memory import (  # noqa: E402
    PromotionReview,
    load_candidates,
    stable_id,
)

CST = timezone(timedelta(hours=8))
EXPERIMENT_DIR = ROOT / "experiments"
MEMORY_DIR = ROOT / "research_memory"


def parse_dt(value: Any):
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


def date_range(end: str | None, days: int) -> list[str]:
    if end:
        end_dt = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=CST)
    else:
        end_dt = (datetime.now(CST) - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return [(end_dt - timedelta(days=i)).strftime("%Y-%m-%d") for i in reversed(range(days))]


def in_dates(row: dict[str, Any], days: set[str], fields: tuple[str, ...] = ("time", "ts")) -> bool:
    for field in fields:
        dt = parse_dt(row.get(field))
        if dt and dt.strftime("%Y-%m-%d") in days:
            return True
    return False


def strategy_files(data_root: Path) -> dict[str, dict[str, Path]]:
    return {
        "A/v11": {
            "trades": data_root / "scanner_data" / "trades.jsonl",
            "events": data_root / "scanner_data" / "events.jsonl",
            "decisions": data_root / "logs" / "decisions.jsonl",
        },
        "B/v16": {
            "trades": data_root / "scanner_data_v16" / "trades.jsonl",
            "events": data_root / "scanner_data_v16" / "events.jsonl",
            "decisions": data_root / "logs_v16" / "decisions.jsonl",
        },
        "C/v14": {
            "trades": data_root / "scanner_data_v14" / "trades.jsonl",
            "events": data_root / "scanner_data_v14" / "events.jsonl",
            "decisions": data_root / "logs_v14" / "decisions.jsonl",
        },
    }


def candidate_files(memory_dir: Path) -> Path:
    return memory_dir / "hypotheses" / "candidates_latest.jsonl"


def trade_score(row: dict[str, Any]) -> float:
    return abs(to_float(row.get("score") or row.get("entry_score") or row.get("raw_score")))


def strategy_file_for(base_strategy: str, data_root: Path) -> dict[str, Path]:
    return strategy_files(data_root).get(base_strategy, strategy_files(data_root)["A/v11"])


def candidate_to_spec(candidate: Any) -> ExperimentSpec:
    family_id = candidate.family_id or stable_id("FAM", candidate.strategy, candidate.change_type, "generated")
    return ExperimentSpec(
        experiment_id=candidate.candidate_id,
        base_strategy=candidate.strategy,
        hypothesis=candidate.problem or candidate.proposal,
        change_type=candidate.change_type,
        params=dict(candidate.params or {}),
        status=candidate.status or "generated",
        candidate_id=candidate.candidate_id,
        source_cases=list(candidate.source_cases or []),
        family_id=family_id,
        parent_experiment_id=getattr(candidate, "parent_candidate_id", "") or "",
        generation=int(getattr(candidate, "generation", 1) or 1),
        governance_status=getattr(candidate, "governance_status", "active") or "active",
        created_at=candidate.created_at,
    )


def enrich_result(result: ExperimentResult, spec: ExperimentSpec) -> ExperimentResult:
    result.family_id = spec.family_id or stable_id("FAM", spec.base_strategy, spec.change_type, "manual")
    result.generation = spec.generation or 1
    return result


def evaluate_promotion(result: ExperimentResult, min_trades: int = 30) -> ExperimentResult:
    if result.sample_trades < min_trades:
        result.promotion_status = "observe"
        result.gate_passed = False
        result.notes.append(f"样本不足 {result.sample_trades}/{min_trades}，只观察不晋级")
        return result

    if result.shadow_pnl < result.original_pnl * 0.95 and result.original_pnl > 0:
        result.promotion_status = "reject"
        result.gate_passed = False
        result.notes.append("影子PnL较原版恶化超过5%")
        return result

    if result.hard_stop_after < result.hard_stop_before and result.avoided_loss > result.missed_profit:
        result.promotion_status = "approved_candidate"
        result.gate_passed = True
        result.notes.append("硬顶减少且避免亏损大于错过盈利，可进入人工审批")
    else:
        result.promotion_status = "observe"
        result.gate_passed = False
        result.notes.append("未达到自动晋级条件，继续观察")
    return result


def run_v14_tail_guard(spec: ExperimentSpec, data_root: Path, days: list[str]) -> ExperimentResult:
    files = strategy_files(data_root)["C/v14"]
    trades = [t for t in read_jsonl(files["trades"]) if in_dates(t, set(days), ("exit_time", "time")) and "pnl_usd" in t]
    original_pnl = sum(to_float(t.get("pnl_usd")) for t in trades)

    min_score = float(spec.params.get("min_score_1h", 55))
    tail_min = float(spec.params.get("tail_guard_min_score", 60))

    def rejected(t: dict[str, Any]) -> bool:
        score = trade_score(t)
        reason = str(t.get("reason") or t.get("entry_reason") or "")
        exit_reason = str(t.get("exit_reason") or "")
        if score < min_score:
            return True
        if score < tail_min and ("ST" in reason or "EMA" in reason) and is_hard_stop(t):
            return True
        if score < tail_min and "最大亏损" in exit_reason:
            return True
        return False

    kept = [t for t in trades if not rejected(t)]
    filtered = [t for t in trades if rejected(t)]
    avoided_loss = abs(sum(to_float(t.get("pnl_usd")) for t in filtered if to_float(t.get("pnl_usd")) < 0))
    missed_profit = sum(to_float(t.get("pnl_usd")) for t in filtered if to_float(t.get("pnl_usd")) > 0)
    result = ExperimentResult(
        experiment_id=spec.experiment_id,
        base_strategy=spec.base_strategy,
        sample_window=f"{days[0]}~{days[-1]}",
        sample_trades=len(trades),
        original_pnl=round(original_pnl, 4),
        shadow_pnl=round(sum(to_float(t.get("pnl_usd")) for t in kept), 4),
        filtered_trades=len(filtered),
        avoided_loss=round(avoided_loss, 4),
        missed_profit=round(missed_profit, 4),
        hard_stop_before=sum(1 for t in trades if is_hard_stop(t)),
        hard_stop_after=sum(1 for t in kept if is_hard_stop(t)),
        candidate_id=spec.candidate_id,
        source_cases=list(spec.source_cases),
        change_type=spec.change_type,
        notes=[
            f"原胜率 {win_rate(trades):.1f}%，影子胜率 {win_rate(kept):.1f}%",
            f"过滤分数阈值 min_score={min_score}, tail_min={tail_min}",
        ],
    )
    return evaluate_promotion(result)


def run_v16_confirm_soft_pass(spec: ExperimentSpec, data_root: Path, days: list[str]) -> ExperimentResult:
    files = strategy_files(data_root)["B/v16"]
    events = [e for e in read_jsonl(files["events"]) if in_dates(e, set(days), ("time", "ts"))]
    decisions = [d for d in read_jsonl(files["decisions"]) if in_dates(d, set(days), ("time", "ts"))]
    rows = events + decisions
    skips = [
        r for r in rows
        if str(r.get("event") or r.get("status") or "").upper() in {"OPEN_SKIPPED", "SKIPPED"}
        or str(r.get("category") or "") == "confirmation"
    ]
    no_confirm = [r for r in skips if "15m" in str(r.get("skip_reason") or r.get("reason") or "")]
    score_counter = Counter()
    pass_candidates = []
    no_confirm_pass = float(spec.params.get("no_confirm_high_score_pass", 50))
    weak_pass = float(spec.params.get("weak_confirm_pass_score", 44))
    for row in no_confirm:
        score = abs(to_float(row.get("score") or row.get("raw_score")))
        reason = str(row.get("skip_reason") or row.get("reason") or "")
        if "无确认" in reason and score >= no_confirm_pass:
            pass_candidates.append(row)
            score_counter["no_confirm_high_score"] += 1
        elif "确认分不足" in reason and score >= weak_pass:
            pass_candidates.append(row)
            score_counter["weak_confirm_high_score"] += 1
    result = ExperimentResult(
        experiment_id=spec.experiment_id,
        base_strategy=spec.base_strategy,
        sample_window=f"{days[0]}~{days[-1]}",
        sample_trades=len(no_confirm),
        original_pnl=0.0,
        shadow_pnl=0.0,
        filtered_trades=-len(pass_candidates),
        avoided_loss=0.0,
        missed_profit=0.0,
        candidate_id=spec.candidate_id,
        source_cases=list(spec.source_cases),
        change_type=spec.change_type,
        promotion_status="observe",
        notes=[
            f"确认层跳过 {len(no_confirm)} 条，软放行候选 {len(pass_candidates)} 条",
            "该实验没有真实成交PnL，需要进入影子纸面撮合或小样本人工审批",
            f"候选分布: {dict(score_counter)}",
        ],
    )
    return result


def run_v11_replacement_quality(spec: ExperimentSpec, data_root: Path, days: list[str]) -> ExperimentResult:
    files = strategy_files(data_root)["A/v11"]
    trades = [t for t in read_jsonl(files["trades"]) if in_dates(t, set(days), ("exit_time", "time")) and "pnl_usd" in t]
    original_pnl = sum(to_float(t.get("pnl_usd")) for t in trades)
    weak_noise = [
        t for t in trades
        if trade_score(t) < 80 and to_float(t.get("pnl_usd")) < 0 and "浮动止损" in str(t.get("exit_reason") or "")
    ]
    result = ExperimentResult(
        experiment_id=spec.experiment_id,
        base_strategy=spec.base_strategy,
        sample_window=f"{days[0]}~{days[-1]}",
        sample_trades=len(trades),
        original_pnl=round(original_pnl, 4),
        shadow_pnl=round(original_pnl - sum(to_float(t.get("pnl_usd")) for t in weak_noise), 4),
        filtered_trades=len(weak_noise),
        avoided_loss=round(abs(sum(to_float(t.get("pnl_usd")) for t in weak_noise)), 4),
        missed_profit=0.0,
        hard_stop_before=sum(1 for t in trades if is_hard_stop(t)),
        hard_stop_after=sum(1 for t in trades if is_hard_stop(t)),
        candidate_id=spec.candidate_id,
        source_cases=list(spec.source_cases),
        change_type=spec.change_type,
        promotion_status="observe",
        notes=[
            f"低分浮动止损噪音样本 {len(weak_noise)} 条",
            "该实验是替换质量审计，不直接晋级，需结合持仓释放事件判断",
        ],
    )
    return result


def run_generic_stage_guard(spec: ExperimentSpec, data_root: Path, days: list[str]) -> ExperimentResult:
    files = strategy_file_for(spec.base_strategy, data_root)
    trades = [t for t in read_jsonl(files["trades"]) if in_dates(t, set(days), ("exit_time", "time")) and "pnl_usd" in t]
    original_pnl = sum(to_float(t.get("pnl_usd")) for t in trades)
    min_score = float(spec.params.get("min_score", spec.params.get("tail_guard_min_score", 55)))
    keep_profitable = bool(spec.params.get("protect_profitable_positions", True))
    block_reverse_stage = bool(spec.params.get("block_reverse_stage", True))
    tail_guard = bool(spec.params.get("tail_guard", True))

    def rejected(t: dict[str, Any]) -> bool:
        score = trade_score(t)
        reason = str(t.get("reason") or t.get("entry_reason") or "")
        exit_reason = str(t.get("exit_reason") or "")
        if score < min_score:
            return True
        if keep_profitable and to_float(t.get("pnl_usd")) > 0:
            return False
        if tail_guard and is_hard_stop(t):
            return True
        if block_reverse_stage and any(k in reason for k in ("逆势", "反向")) and is_hard_stop(t):
            return True
        if tail_guard and "最大亏损" in exit_reason:
            return True
        return False

    kept = [t for t in trades if not rejected(t)]
    filtered = [t for t in trades if rejected(t)]
    result = ExperimentResult(
        experiment_id=spec.experiment_id,
        base_strategy=spec.base_strategy,
        sample_window=f"{days[0]}~{days[-1]}",
        sample_trades=len(trades),
        original_pnl=round(original_pnl, 4),
        shadow_pnl=round(sum(to_float(t.get("pnl_usd")) for t in kept), 4),
        filtered_trades=len(filtered),
        avoided_loss=round(abs(sum(to_float(t.get("pnl_usd")) for t in filtered if to_float(t.get("pnl_usd")) < 0)), 4),
        missed_profit=round(sum(to_float(t.get("pnl_usd")) for t in filtered if to_float(t.get("pnl_usd")) > 0), 4),
        hard_stop_before=sum(1 for t in trades if is_hard_stop(t)),
        hard_stop_after=sum(1 for t in kept if is_hard_stop(t)),
        candidate_id=spec.candidate_id,
        source_cases=list(spec.source_cases),
        change_type=spec.change_type,
        promotion_status="observe",
        notes=[
            f"阶段过滤样本 {len(filtered)} / {len(trades)}",
            f"阈值 min_score={min_score}, tail_guard={tail_guard}, reverse_guard={block_reverse_stage}",
        ],
    )
    return evaluate_promotion(result)


def run_generic_replacement_quality(spec: ExperimentSpec, data_root: Path, days: list[str]) -> ExperimentResult:
    files = strategy_file_for(spec.base_strategy, data_root)
    trades = [t for t in read_jsonl(files["trades"]) if in_dates(t, set(days), ("exit_time", "time")) and "pnl_usd" in t]
    original_pnl = sum(to_float(t.get("pnl_usd")) for t in trades)
    weak_noise = [
        t for t in trades
        if trade_score(t) < float(spec.params.get("strong_signal_min_score", 112))
        and to_float(t.get("pnl_usd")) < 0
        and "浮动止损" in str(t.get("exit_reason") or "")
    ]
    result = ExperimentResult(
        experiment_id=spec.experiment_id,
        base_strategy=spec.base_strategy,
        sample_window=f"{days[0]}~{days[-1]}",
        sample_trades=len(trades),
        original_pnl=round(original_pnl, 4),
        shadow_pnl=round(original_pnl - sum(to_float(t.get("pnl_usd")) for t in weak_noise), 4),
        filtered_trades=len(weak_noise),
        avoided_loss=round(abs(sum(to_float(t.get("pnl_usd")) for t in weak_noise)), 4),
        missed_profit=0.0,
        hard_stop_before=sum(1 for t in trades if is_hard_stop(t)),
        hard_stop_after=sum(1 for t in trades if is_hard_stop(t)),
        candidate_id=spec.candidate_id,
        source_cases=list(spec.source_cases),
        change_type=spec.change_type,
        promotion_status="observe",
        notes=[
            f"替换审计样本 {len(weak_noise)} 条",
            "低分浮动止损噪音审计，不直接晋级，需结合持仓释放事件判断",
        ],
    )
    return result


def load_shadow_experiments(memory_dir: Path) -> list[ExperimentSpec]:
    candidates_path = candidate_files(memory_dir)
    if not candidates_path.exists():
        return []
    return [candidate_to_spec(c) for c in load_candidates(candidates_path)]


def load_all_specs(memory_dir: Path) -> list[ExperimentSpec]:
    return default_experiments() + load_shadow_experiments(memory_dir)


def evaluate_spec(spec: ExperimentSpec, data_root: Path, days: list[str]) -> ExperimentResult:
    if spec.experiment_id.endswith("v14-tail-guard"):
        return enrich_result(run_v14_tail_guard(spec, data_root, days), spec)
    if spec.experiment_id.endswith("v16-confirm-soft-pass"):
        return enrich_result(run_v16_confirm_soft_pass(spec, data_root, days), spec)
    if spec.experiment_id.endswith("v11-replacement-quality"):
        return enrich_result(run_v11_replacement_quality(spec, data_root, days), spec)
    if spec.change_type == "confirmation_policy":
        return enrich_result(run_v16_confirm_soft_pass(spec, data_root, days), spec)
    if spec.change_type == "market_stage_filter":
        return enrich_result(run_generic_stage_guard(spec, data_root, days), spec)
    if spec.change_type == "replacement_policy":
        return enrich_result(run_generic_replacement_quality(spec, data_root, days), spec)
    return enrich_result(run_generic_stage_guard(spec, data_root, days), spec)


def evaluate_specs(specs: list[ExperimentSpec], data_root: Path, days: list[str]) -> list[ExperimentResult]:
    return [evaluate_spec(spec, data_root, days) for spec in specs]


def family_decision(results: list[ExperimentResult]) -> str:
    if any(r.promotion_status == "approved_for_small_live" for r in results):
        return "small_live_observation"
    if any(r.promotion_status == "approved_candidate" for r in results):
        return "needs_manual_approval"
    if results and all(r.promotion_status == "reject" for r in results):
        return "archive_or_rework"
    return "observe"


def load_manual_actions(memory_dir: Path) -> dict[str, dict[str, Any]]:
    rows = read_jsonl(memory_dir / "approvals" / "manual_actions.jsonl")
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        for key in (row.get("candidate_id"), row.get("experiment_id"), row.get("family_id")):
            if key:
                out[str(key)] = row
    return out


def apply_manual_actions(results: list[ExperimentResult], memory_dir: Path) -> None:
    actions = load_manual_actions(memory_dir)
    for result in results:
        record = actions.get(result.candidate_id) or actions.get(result.experiment_id) or actions.get(result.family_id)
        if not record:
            continue
        action = str(record.get("manual_action") or "")
        if action == "approve_shadow":
            result.promotion_status = "approved_for_small_live"
            result.gate_passed = True
            result.notes.append(f"人工审批: 小仓观察 scope={record.get('approved_scope') or ''}")
        elif action == "reject":
            result.promotion_status = "manual_reject"
            result.gate_passed = False
            result.notes.append("人工审批: 拒绝候选")
        elif action == "observe":
            result.promotion_status = "observe"
            result.notes.append("人工审批: 继续观察")


def write_experiment_families(out_dir: Path, memory_dir: Path, specs: list[ExperimentSpec], results: list[ExperimentResult]) -> Path:
    spec_by_id = {s.experiment_id: s for s in specs}
    grouped: dict[str, list[ExperimentResult]] = {}
    for result in results:
        family_id = result.family_id or stable_id("FAM", result.base_strategy, result.change_type, "manual")
        grouped.setdefault(family_id, []).append(result)

    rows: list[dict[str, Any]] = []
    for family_id, family_results in sorted(grouped.items()):
        family_specs = [spec_by_id.get(r.experiment_id) for r in family_results if spec_by_id.get(r.experiment_id)]
        latest = family_results[-1]
        best = max(family_results, key=lambda r: (r.shadow_pnl - r.original_pnl, -r.hard_stop_after))
        rows.append({
            "family_id": family_id,
            "base_strategy": latest.base_strategy,
            "change_type": latest.change_type,
            "governance_status": family_specs[-1].governance_status if family_specs else "active",
            "generation": max((s.generation for s in family_specs), default=latest.generation or 1),
            "experiment_count": len(family_results),
            "candidate_count": sum(1 for r in family_results if r.candidate_id),
            "latest_experiment_id": latest.experiment_id,
            "latest_status": latest.promotion_status,
            "recommended_action": family_decision(family_results),
            "best_experiment_id": best.experiment_id,
            "best_pnl_delta": round(best.shadow_pnl - best.original_pnl, 4),
            "hard_stop_delta": latest.hard_stop_after - latest.hard_stop_before,
            "source_cases": sorted({case for r in family_results for case in r.source_cases}),
            "experiments": [r.to_dict() for r in family_results],
        })

    path = out_dir / "families_latest.json"
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    memory_path = memory_dir / "promotions" / "families_latest.json"
    memory_path.parent.mkdir(parents=True, exist_ok=True)
    memory_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def write_promotion_reviews(memory_dir: Path, results: list[ExperimentResult]) -> Path:
    from core.research_memory import write_jsonl as memory_write_jsonl

    reviews: list[PromotionReview] = []
    actions = load_manual_actions(memory_dir)
    date_str = datetime.now(CST).strftime("%Y-%m-%d")
    for result in results:
        if not result.candidate_id:
            continue
        action_record = actions.get(result.candidate_id) or actions.get(result.experiment_id) or actions.get(result.family_id) or {}
        decision = "manual_review"
        manual_review_required = True
        if result.promotion_status == "approved_for_small_live":
            decision = "manual_approved_small_live"
            manual_review_required = False
        elif result.promotion_status == "approved_candidate":
            decision = "approve_for_manual_review"
        elif result.promotion_status == "reject":
            decision = "reject"
        reviews.append(
            PromotionReview(
                review_id=stable_id("REVIEW", date_str, result.candidate_id),
                date=date_str,
                candidate_id=result.candidate_id,
                experiment_id=result.experiment_id,
                base_strategy=result.base_strategy,
                change_type=result.change_type,
                promotion_status=result.promotion_status,
                decision=decision,
                reason="; ".join(result.notes[:3]),
                manual_review_required=manual_review_required,
                source_cases=list(result.source_cases),
                shadow_pnl=result.shadow_pnl,
                original_pnl=result.original_pnl,
                hard_stop_before=result.hard_stop_before,
                hard_stop_after=result.hard_stop_after,
                family_id=result.family_id,
                manual_action=str(action_record.get("manual_action") or ""),
                approved_scope=str(action_record.get("approved_scope") or ""),
                next_step=str(action_record.get("next_step") or decision),
                risk_notes=str(action_record.get("risk_notes") or ""),
            )
        )
    path = memory_dir / "promotions" / f"reviews_{date_str}.jsonl"
    memory_write_jsonl(path, [r.to_dict() for r in reviews])
    memory_write_jsonl(memory_dir / "promotions" / "reviews_latest.jsonl", [r.to_dict() for r in reviews])
    return path


def run_all(data_root: Path, days: list[str], out_dir: Path, memory_dir: Path) -> list[ExperimentResult]:
    specs = load_all_specs(memory_dir)
    write_jsonl(out_dir / "registry.jsonl", [s.to_dict() for s in specs])
    results = evaluate_specs(specs, data_root, days)
    for result in results:
        append_jsonl(out_dir / "results" / "latest.jsonl", result.to_dict())
    apply_manual_actions(results, memory_dir)
    write_jsonl(out_dir / "results" / "latest.jsonl", [r.to_dict() for r in results])
    write_promotion_reviews(memory_dir, results)
    write_experiment_families(out_dir, memory_dir, specs, results)
    return results


def parse_windows(text: str) -> list[int]:
    windows: list[int] = []
    for part in str(text or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except Exception:
            continue
        if value > 0 and value not in windows:
            windows.append(value)
    return windows


def run_windowed(data_root: Path, out_dir: Path, memory_dir: Path, windows: list[int], end: str | None) -> list[dict[str, Any]]:
    specs = load_all_specs(memory_dir)
    rows: list[dict[str, Any]] = []
    for window in windows:
        days = date_range(end, window)
        results = evaluate_specs(specs, data_root, days)
        apply_manual_actions(results, memory_dir)
        for result in results:
            row = result.to_dict()
            row["window_days"] = window
            row["window_label"] = f"{window}d"
            rows.append(row)
    write_jsonl(out_dir / "results" / "windowed_latest.jsonl", rows)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run shadow experiments from synced logs")
    parser.add_argument("--data-root", default=str(ROOT / "server_logs_tencent"))
    parser.add_argument("--out-dir", default=str(EXPERIMENT_DIR))
    parser.add_argument("--memory-dir", default=str(MEMORY_DIR))
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--end", default=None, help="End date YYYY-MM-DD, default yesterday")
    parser.add_argument("--windows", default="", help="Optional comma-separated validation windows, e.g. 3,7,14,30")
    args = parser.parse_args(argv)

    days = date_range(args.end, args.days)
    out_dir = Path(args.out_dir)
    (out_dir / "results").mkdir(parents=True, exist_ok=True)
    latest = out_dir / "results" / "latest.jsonl"
    if latest.exists():
        latest.unlink()
    windows = parse_windows(args.windows)
    if windows:
        windowed = run_windowed(Path(args.data_root), out_dir, Path(args.memory_dir), windows, args.end)
        print(f"Windowed results: {out_dir / 'results' / 'windowed_latest.jsonl'} rows={len(windowed)} windows={windows}")
    results = run_all(Path(args.data_root), days, out_dir, Path(args.memory_dir))
    for result in results:
        print(
            f"{result.experiment_id}: {result.promotion_status} "
            f"trades={result.sample_trades} pnl={result.original_pnl:+.2f}->{result.shadow_pnl:+.2f}"
        )
    print(f"Results: {latest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
