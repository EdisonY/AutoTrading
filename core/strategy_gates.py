"""Pure strategy gate helpers shared by live scanners and replay tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Collection, Mapping


@dataclass(frozen=True)
class StrategyGateDecision:
    allowed: bool
    gate: str
    reason: str = ""
    threshold: float | None = None
    adjusted_score: float | None = None
    evidence: dict[str, Any] | None = None


def evaluate_a_v11_entry_threshold(
    *,
    timeframe: str,
    side: str,
    score: float,
    score_thresholds: Mapping[str, float],
    score_threshold: float,
    short_entry_penalty: float,
) -> StrategyGateDecision:
    """Evaluate the A/v11 entry-threshold gate without touching runtime state."""
    threshold = float(score_thresholds.get(timeframe, score_threshold))
    side_key = str(side or "").lower()
    if side_key == "short":
        threshold += float(short_entry_penalty)
    adjusted_score = abs(float(score))
    return StrategyGateDecision(
        allowed=adjusted_score >= threshold,
        gate="threshold",
        reason="threshold_pass" if adjusted_score >= threshold else "threshold_fail",
        threshold=threshold,
        adjusted_score=adjusted_score,
        evidence={"timeframe": timeframe, "side": side_key},
    )


def effective_a_v11_signal_score(
    *,
    score: float,
    side: str,
    resonance: bool,
    resonance_bonus: float,
) -> float:
    """Apply the A/v11 resonance bonus to a raw signal score."""
    adjusted_score = float(score or 0)
    side_key = str(side or "").lower()
    if resonance:
        if side_key == "long" and adjusted_score > 0:
            return round(adjusted_score + float(resonance_bonus), 1)
        if side_key == "short" and adjusted_score < 0:
            return round(adjusted_score - float(resonance_bonus), 1)
    return adjusted_score


def evaluate_a_v11_replacement_signal(
    *,
    effective_score: float,
    strong_signal_threshold: float,
) -> StrategyGateDecision:
    """Evaluate whether an A/v11 signal is strong enough to try full-position replacement."""
    adjusted_score = abs(float(effective_score))
    threshold = float(strong_signal_threshold)
    return StrategyGateDecision(
        allowed=adjusted_score >= threshold,
        gate="position_replacement",
        reason="replacement_signal_pass" if adjusted_score >= threshold else "replacement_signal_fail",
        threshold=threshold,
        adjusted_score=adjusted_score,
    )


def evaluate_b_v16_entry_threshold(
    *,
    timeframe: str,
    side: str,
    score: float,
    symbol: str | None,
    open_positions: int | None,
    confirm_reason: str,
    score_thresholds: Mapping[str, float],
    score_min: float,
    short_entry_penalty: float,
    major_symbols: Collection[str],
    low_position_threshold_discount: float,
    no_confirm_threshold_penalty: float,
    weak_opposite_confirm_penalty: float,
    confirm_bonus: float,
    confirm_strong_bonus: float,
) -> StrategyGateDecision:
    """Evaluate the B/v16 entry-threshold gate without touching runtime state."""
    threshold = float(score_thresholds.get(timeframe, score_min))
    adjusted_score = float(score)
    symbol_key = str(symbol or "").upper()
    side_key = str(side or "").lower()
    reason = str(confirm_reason or "")

    if side_key == "short" and symbol_key not in set(major_symbols):
        threshold += float(short_entry_penalty)
    if open_positions is not None and int(open_positions) <= 3:
        threshold -= float(low_position_threshold_discount)
    if "15m无信号" in reason:
        threshold += float(no_confirm_threshold_penalty)
    elif "15m轻微相反" in reason:
        threshold += float(weak_opposite_confirm_penalty)
    elif "+8" in reason or "+5" in reason:
        adjusted_score += float(confirm_strong_bonus if "+8" in reason else confirm_bonus)

    return StrategyGateDecision(
        allowed=adjusted_score >= threshold,
        gate="threshold",
        reason="threshold_pass" if adjusted_score >= threshold else "threshold_fail",
        threshold=threshold,
        adjusted_score=adjusted_score,
        evidence={
            "timeframe": timeframe,
            "side": side_key,
            "symbol": symbol_key,
            "open_positions": open_positions,
            "confirm_reason": reason,
        },
    )


def evaluate_c_v14_entry_threshold(
    *,
    timeframe: str,
    side: str,
    trend_dir: str = "neutral",
    trend_strength: float = 0.0,
    score_thresholds: Mapping[str, float],
    score_min: float,
    long_penalty: float,
    short_entry_penalty: float,
    trend_penalty_threshold: float = 50.0,
    trend_penalty_value: float = 15.0,
) -> StrategyGateDecision:
    """Evaluate the C/v14 entry threshold and trend penalty."""
    side_key = str(side or "").lower()
    trend_key = str(trend_dir or "neutral").lower()
    strength = float(trend_strength or 0)
    trend_penalty = 0.0
    if trend_key == "bull" and strength >= float(trend_penalty_threshold) and side_key == "short":
        trend_penalty = float(trend_penalty_value)
    elif trend_key == "bear" and strength >= float(trend_penalty_threshold) and side_key == "long":
        trend_penalty = float(trend_penalty_value)

    threshold = (
        float(score_thresholds.get(timeframe, score_min))
        + (float(long_penalty) if side_key == "long" else 0.0)
        + (float(short_entry_penalty) if side_key == "short" else 0.0)
        + trend_penalty
    )
    return StrategyGateDecision(
        allowed=True,
        gate="threshold",
        reason="threshold_computed",
        threshold=threshold,
        adjusted_score=None,
        evidence={
            "timeframe": timeframe,
            "side": side_key,
            "trend_dir": trend_key,
            "trend_strength": strength,
            "trend_penalty": trend_penalty,
        },
    )


def evaluate_c_v14_confirmation_gate(
    *,
    side: str,
    entry_score: float,
    confirm_signal: Mapping[str, Any] | None,
    confirm_timeframe: str,
    no_confirm_high_score_pass: float,
    weak_confirm_min_score: float,
    confirm_min_score: float,
) -> StrategyGateDecision:
    """Evaluate the C/v14 confirmation gate from a supplied confirmation signal."""
    side_key = str(side or "").lower()
    score = float(entry_score)
    if not confirm_signal:
        if score >= float(no_confirm_high_score_pass):
            return StrategyGateDecision(True, "confirmation", f"扩样放行:{confirm_timeframe}无信号但1h高分{score:.0f}")
        return StrategyGateDecision(False, "confirmation", "15m无有效确认")

    confirm_score = abs(float(confirm_signal.get("net_score") or 0))
    confirm_side = str(confirm_signal.get("trade_side") or "").lower()
    if confirm_side != side_key:
        if confirm_score < float(weak_confirm_min_score) and score >= float(no_confirm_high_score_pass):
            return StrategyGateDecision(True, "confirmation", f"扩样放行:15m弱反向{confirm_score:.0f}+1h高分{score:.0f}")
        return StrategyGateDecision(False, "confirmation", f"15m方向相反:{confirm_signal.get('trade_side')}")

    if not confirm_signal.get("can_trade"):
        if score >= float(no_confirm_high_score_pass):
            return StrategyGateDecision(True, "confirmation", f"扩样放行:15m弱同向{confirm_score:.0f}+1h高分{score:.0f}")
        return StrategyGateDecision(False, "confirmation", "15m无有效确认")

    if confirm_score < float(confirm_min_score):
        if confirm_score >= float(weak_confirm_min_score) and score >= float(no_confirm_high_score_pass):
            return StrategyGateDecision(True, "confirmation", f"扩样放行:15m弱确认{confirm_score:.0f}+1h高分{score:.0f}")
        return StrategyGateDecision(False, "confirmation", f"15m确认分不足:{confirm_score:.0f}")

    return StrategyGateDecision(True, "confirmation", f"15m确认{confirm_score:.0f}")


def evaluate_c_v14_tail_guard(
    *,
    signal: Mapping[str, Any],
    side: str,
    tail_guard_min_score: float,
    tail_guard_long_bb_pos: float,
    tail_guard_short_bb_pos: float,
    tail_guard_min_vol_ratio: float,
    tail_guard_max_atr_pct: float,
) -> StrategyGateDecision:
    """Evaluate C/v14 low-score tail-chasing guard."""
    abs_score = abs(float(signal.get("net_score") or 0))
    if abs_score >= float(tail_guard_min_score):
        return StrategyGateDecision(True, "tail_guard", "tail_guard_pass_high_score", adjusted_score=abs_score)

    bb_pos = float(signal.get("bb_pos") or 50.0)
    rsi = float(signal.get("rsi") or 50.0)
    vol_ratio = float(signal.get("vol_ratio") or 1.0)
    atr_pct = float(signal.get("atr_pct") or 0.0)
    st_flipped = bool(signal.get("st_flipped"))
    side_key = str(side or "").lower()

    if atr_pct >= float(tail_guard_max_atr_pct):
        return StrategyGateDecision(False, "tail_guard", f"硬顶尾部过滤:波动过高 atr_pct={atr_pct:.3f} score={abs_score:.0f}", adjusted_score=abs_score)
    if st_flipped and vol_ratio < float(tail_guard_min_vol_ratio):
        return StrategyGateDecision(False, "tail_guard", f"硬顶尾部过滤:ST翻转但放量不足 vol={vol_ratio:.1f} score={abs_score:.0f}", adjusted_score=abs_score)
    if side_key == "long" and bb_pos >= float(tail_guard_long_bb_pos) and rsi >= 55:
        return StrategyGateDecision(False, "tail_guard", f"硬顶尾部过滤:多头高位追涨 bb_pos={bb_pos:.0f} rsi={rsi:.0f}", adjusted_score=abs_score)
    if side_key == "short" and bb_pos <= float(tail_guard_short_bb_pos) and rsi <= 45:
        return StrategyGateDecision(False, "tail_guard", f"硬顶尾部过滤:空头低位追跌 bb_pos={bb_pos:.0f} rsi={rsi:.0f}", adjusted_score=abs_score)
    return StrategyGateDecision(True, "tail_guard", "tail_guard_pass", adjusted_score=abs_score)


def evaluate_b_v16_confirmation_gate(
    *,
    side: str,
    raw_score: float,
    confirm_signal: Mapping[str, Any] | None,
    open_positions: int,
    max_active_new_positions: int,
    no_confirm_high_score_pass: float,
    confirm_opposite_reject_score: float,
    opposite_high_score_pass: float,
    weak_confirm_pass_score: float,
    confirm_min_score: float,
    confirm_bonus: float,
    confirm_strong_bonus: float,
) -> StrategyGateDecision:
    """Evaluate the B/v16 15m confirmation gate from a supplied signal row."""
    side_key = str(side or "").lower()
    score = float(raw_score)
    if not confirm_signal:
        if score >= float(no_confirm_high_score_pass):
            return StrategyGateDecision(True, "confirmation", "15m无信号但高分放行")
        return StrategyGateDecision(False, "confirmation", "15m无确认信号")

    confirm_side = str(confirm_signal.get("trade_side") or "").lower()
    confirm_score_value = confirm_signal.get("net_score") or 0
    confirm_score = abs(float(confirm_score_value))
    confirm_score_text = str(abs(confirm_score_value)) if isinstance(confirm_score_value, (int, float)) else str(confirm_score)
    if confirm_side != side_key:
        if confirm_score >= float(confirm_opposite_reject_score):
            return StrategyGateDecision(False, "confirmation", f"15m方向强烈相反:{confirm_signal.get('trade_side')} {confirm_score_text}")
        if score >= float(opposite_high_score_pass) and int(open_positions) < int(max_active_new_positions):
            return StrategyGateDecision(True, "confirmation", f"15m轻微相反但高分放行:{confirm_score_text}")
        return StrategyGateDecision(False, "confirmation", f"15m方向相反:{confirm_signal.get('trade_side')}")

    if confirm_score < float(confirm_min_score):
        if score >= float(weak_confirm_pass_score):
            return StrategyGateDecision(True, "confirmation", f"15m弱确认放行:{confirm_score_text}")
        return StrategyGateDecision(False, "confirmation", f"15m确认分不足:{confirm_score_text}")

    bonus = float(confirm_strong_bonus if confirm_score >= 35 else confirm_bonus)
    bonus_text = str(int(bonus)) if bonus.is_integer() else str(bonus)
    return StrategyGateDecision(True, "confirmation", f"15m确认{confirm_score_text}+{bonus_text}")
