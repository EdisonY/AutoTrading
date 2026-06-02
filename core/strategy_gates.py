"""Pure strategy gate helpers shared by live scanners and replay tools."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Collection, Mapping


@dataclass(frozen=True)
class StrategyGateDecision:
    allowed: bool
    gate: str
    reason: str = ""
    threshold: float | None = None
    adjusted_score: float | None = None
    evidence: dict[str, Any] | None = None


def evaluate_no_same_symbol_position_gate(
    *,
    has_exchange_position: bool,
    has_local_position: bool,
) -> StrategyGateDecision:
    """Evaluate the shared no-same-symbol-stacking gate."""
    if has_exchange_position or has_local_position:
        return StrategyGateDecision(
            False,
            "position_duplicate",
            "same_symbol_position_exists",
            evidence={
                "has_exchange_position": bool(has_exchange_position),
                "has_local_position": bool(has_local_position),
            },
        )
    return StrategyGateDecision(True, "position_duplicate", "no_same_symbol_position")


def evaluate_same_side_position_gate(
    *,
    has_same_side_position: bool,
) -> StrategyGateDecision:
    """Evaluate a cross-timeframe same-symbol/same-side position guard."""
    if has_same_side_position:
        return StrategyGateDecision(False, "position_gate", "same_side_position_exists")
    return StrategyGateDecision(True, "position_gate", "no_same_side_position")


def evaluate_timeframe_position_gate(
    *,
    has_timeframe_position: bool,
) -> StrategyGateDecision:
    """Evaluate whether this timeframe already holds the symbol."""
    if has_timeframe_position:
        return StrategyGateDecision(False, "position_gate", "timeframe_position_exists")
    return StrategyGateDecision(True, "position_gate", "no_timeframe_position")


def evaluate_account_state_available_gate(
    *,
    account_state_available: bool,
    read_error: bool = False,
) -> StrategyGateDecision:
    """Evaluate whether central account state is usable for entry risk checks."""
    if not account_state_available:
        reason = "account_state_read_failed" if read_error else "account_state_unavailable"
        return StrategyGateDecision(
            False,
            "risk_gate",
            reason,
            evidence={"account_state_available": False, "read_error": bool(read_error)},
        )
    return StrategyGateDecision(
        True,
        "risk_gate",
        "account_state_available",
        evidence={"account_state_available": True, "read_error": bool(read_error)},
    )


def evaluate_symbol_stop_loss_gate(
    *,
    stop_loss_count: int,
    max_stop_loss_per_symbol: int,
) -> StrategyGateDecision:
    """Evaluate per-symbol stop-loss cooldown/ban guard."""
    count = int(stop_loss_count or 0)
    limit = int(max_stop_loss_per_symbol)
    if count >= limit:
        return StrategyGateDecision(
            False,
            "cooldown",
            f"当日止损{count}次已达上限",
            evidence={"stop_loss_count": count, "max_stop_loss_per_symbol": limit},
        )
    return StrategyGateDecision(True, "cooldown", "symbol_stop_loss_allowed", evidence={"stop_loss_count": count})


def evaluate_symbol_blacklist_gate(
    *,
    symbol: str,
    blacklisted_symbols: Collection[str],
    reason: str = "symbol_blacklisted",
) -> StrategyGateDecision:
    """Evaluate a shared symbol blacklist pre-filter."""
    symbol_key = str(symbol or "").upper()
    blacklist = {str(item or "").upper() for item in blacklisted_symbols}
    if symbol_key in blacklist:
        return StrategyGateDecision(
            False,
            "pre_filter",
            str(reason),
            evidence={"symbol": symbol_key},
        )
    return StrategyGateDecision(True, "pre_filter", "symbol_allowed", evidence={"symbol": symbol_key})


def evaluate_sector_position_gate(
    *,
    sector: str,
    sector_position_count: int,
    max_positions_per_sector: int,
    exempt_sector: str = "Other",
) -> StrategyGateDecision:
    """Evaluate sector concentration limit."""
    sector_key = str(sector or "")
    count = int(sector_position_count or 0)
    limit = int(max_positions_per_sector)
    if sector_key != str(exempt_sector) and count >= limit:
        return StrategyGateDecision(
            False,
            "sector_guard",
            f"赛道[{sector_key}]已满{limit}仓",
            evidence={"sector": sector_key, "sector_position_count": count, "max_positions_per_sector": limit},
        )
    return StrategyGateDecision(True, "sector_guard", "sector_allowed", evidence={"sector": sector_key, "sector_position_count": count})


def evaluate_score_max_gate(
    *,
    score: float,
    score_max: float,
) -> StrategyGateDecision:
    """Evaluate score overheat cap."""
    adjusted_score = abs(float(score))
    threshold = float(score_max)
    if adjusted_score > threshold:
        return StrategyGateDecision(
            False,
            "score_gate",
            f"评分{score}超过{score_max}",
            threshold=threshold,
            adjusted_score=adjusted_score,
        )
    return StrategyGateDecision(True, "score_gate", "score_within_max", threshold=threshold, adjusted_score=adjusted_score)


def evaluate_watchlist_score_adjustment(
    *,
    symbol: str,
    score: float,
    watchlist_symbols: Collection[str],
    penalty: float,
) -> StrategyGateDecision:
    """Apply watchlist score penalty without rejecting the signal."""
    raw_score = float(score)
    symbol_key = str(symbol or "").upper()
    watchlist = {str(item or "").upper() for item in watchlist_symbols}
    if symbol_key in watchlist:
        adjusted = max(0.0, raw_score - float(penalty))
        return StrategyGateDecision(
            True,
            "score_adjustment",
            "watchlist_penalty_applied",
            adjusted_score=adjusted,
            evidence={"symbol": symbol_key, "raw_score": raw_score, "penalty": float(penalty)},
        )
    return StrategyGateDecision(
        True,
        "score_adjustment",
        "score_unchanged",
        adjusted_score=raw_score,
        evidence={"symbol": symbol_key, "raw_score": raw_score, "penalty": 0.0},
    )


def evaluate_consecutive_loss_cooldown_gate(
    *,
    consecutive_losses: int,
    last_loss_time: datetime | None,
    now: datetime,
    min_consecutive_losses: int,
    cooldown_minutes: float,
) -> StrategyGateDecision:
    """Evaluate a global consecutive-loss cooldown gate."""
    losses = int(consecutive_losses or 0)
    min_losses = int(min_consecutive_losses)
    cooldown = float(cooldown_minutes)
    if losses < min_losses or last_loss_time is None:
        return StrategyGateDecision(
            True,
            "cooldown",
            "consecutive_loss_cooldown_clear",
            evidence={"consecutive_losses": losses, "min_consecutive_losses": min_losses},
        )

    cooldown_end = last_loss_time + timedelta(minutes=cooldown)
    if now < cooldown_end:
        remaining_minutes = int(max(0.0, (cooldown_end - now).total_seconds()) // 60)
        return StrategyGateDecision(
            False,
            "cooldown",
            "consecutive_loss_cooldown_active",
            evidence={
                "consecutive_losses": losses,
                "min_consecutive_losses": min_losses,
                "cooldown_minutes": cooldown,
                "cooldown_end": cooldown_end,
                "remaining_minutes": remaining_minutes,
            },
        )
    return StrategyGateDecision(
        True,
        "cooldown",
        "consecutive_loss_cooldown_expired",
        evidence={
            "consecutive_losses": losses,
            "min_consecutive_losses": min_losses,
            "cooldown_minutes": cooldown,
            "cooldown_end": cooldown_end,
        },
    )


def evaluate_symbol_cooldown_gate(
    *,
    cooldown_until: datetime | None,
    now: datetime,
) -> StrategyGateDecision:
    """Evaluate a per-symbol datetime cooldown gate."""
    if cooldown_until is None or now >= cooldown_until:
        return StrategyGateDecision(True, "cooldown", "symbol_cooldown_clear")
    remaining_minutes = int(max(0.0, (cooldown_until - now).total_seconds()) // 60)
    return StrategyGateDecision(
        False,
        "cooldown",
        "symbol_cooldown_active",
        evidence={"cooldown_until": cooldown_until, "remaining_minutes": remaining_minutes},
    )


def evaluate_symbol_scan_cooldown_gate(
    *,
    cooldown_ticks: int | float | None,
) -> StrategyGateDecision:
    """Evaluate a per-symbol scan-count cooldown gate."""
    ticks = int(cooldown_ticks or 0)
    if ticks > 0:
        return StrategyGateDecision(
            False,
            "cooldown",
            "symbol_scan_cooldown_active",
            evidence={"cooldown_ticks": ticks},
        )
    return StrategyGateDecision(True, "cooldown", "symbol_scan_cooldown_clear", evidence={"cooldown_ticks": ticks})


def evaluate_active_position_limit_gate(
    *,
    open_positions: int,
    max_active_positions: int,
) -> StrategyGateDecision:
    """Evaluate active-position limit for new opens."""
    count = int(open_positions)
    limit = int(max_active_positions)
    if count >= limit:
        return StrategyGateDecision(
            False,
            "risk_gate",
            f"活跃持仓{count}>={limit}只管理不新开",
            evidence={"open_positions": count, "max_active_positions": limit},
        )
    return StrategyGateDecision(True, "risk_gate", "active_position_limit_ok", evidence={"open_positions": count})


def evaluate_positive_quantity_gate(
    *,
    quantity: float,
) -> StrategyGateDecision:
    """Evaluate whether execution quantity is positive."""
    qty = float(quantity or 0.0)
    if qty <= 0:
        return StrategyGateDecision(
            False,
            "execution",
            "qty<=0",
            evidence={"quantity": qty},
        )
    return StrategyGateDecision(True, "execution", "quantity_positive", evidence={"quantity": qty})


def evaluate_b_v16_small_live_stage_guard(
    *,
    enabled: bool,
    signal: Mapping[str, Any],
    side: str,
    score: float,
    min_score: float,
    reverse_pass_score: float,
) -> StrategyGateDecision:
    """Evaluate the B/v16 small-live stage guard."""
    if not enabled:
        return StrategyGateDecision(True, "small_live_stage_guard", "")

    side_key = str(side or "").lower()
    own_reasons = signal.get(f"reasons_{side_key}", []) or []
    opposite_side = "short" if side_key == "long" else "long"
    opposite_reasons = signal.get(f"reasons_{opposite_side}", []) or []
    score_value = float(score)
    min_score_value = float(min_score)
    reverse_pass_score_value = float(reverse_pass_score)
    reverse_stage = (
        (side_key == "long" and any("EMA空头" in str(reason) for reason in opposite_reasons))
        or (side_key == "short" and any("EMA多头" in str(reason) for reason in opposite_reasons))
    )
    if score_value < min_score_value:
        return StrategyGateDecision(
            False,
            "small_live_stage_guard",
            f"小仓阶段保护: 分数{score_value:.1f}<{min_score_value:.0f}",
            adjusted_score=score_value,
            threshold=min_score_value,
        )
    if reverse_stage and score_value < reverse_pass_score_value:
        return StrategyGateDecision(
            False,
            "small_live_stage_guard",
            f"小仓阶段保护: 逆势EMA且分数{score_value:.1f}<{reverse_pass_score_value:.0f}",
            adjusted_score=score_value,
            threshold=reverse_pass_score_value,
            evidence={"reverse_stage": True},
        )
    has_orderflow = any("CVD+OFI强势" in str(reason) for reason in own_reasons)
    has_rsi_structure = any("RSI" in str(reason) for reason in own_reasons)
    if not has_orderflow and not has_rsi_structure:
        return StrategyGateDecision(
            False,
            "small_live_stage_guard",
            "小仓阶段保护: 缺少订单流强共振或RSI结构",
            adjusted_score=score_value,
            evidence={"has_orderflow": False, "has_rsi_structure": False},
        )
    return StrategyGateDecision(
        True,
        "small_live_stage_guard",
        "small_live_stage_guard_pass",
        adjusted_score=score_value,
        evidence={"reverse_stage": reverse_stage, "has_orderflow": has_orderflow, "has_rsi_structure": has_rsi_structure},
    )


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


def evaluate_a_v11_market_microstructure_gate(
    *,
    atr: float,
    side: str,
    stop_loss: float,
    entry_price: float,
) -> StrategyGateDecision:
    """Evaluate A/v11 pre-open market-data sanity gates."""
    atr_value = float(atr or 0)
    side_key = str(side or "").lower()
    sl_value = float(stop_loss or 0)
    price_value = float(entry_price or 0)
    evidence = {"atr": atr_value, "side": side_key, "stop_loss": sl_value, "entry_price": price_value}
    if atr_value <= 0:
        return StrategyGateDecision(
            False,
            "market_microstructure",
            "ATR=0，止损止盈计算无效",
            evidence=evidence,
        )
    if side_key == "long" and sl_value >= price_value:
        return StrategyGateDecision(
            False,
            "market_microstructure",
            f"多单止损价{stop_loss}>=开仓价{entry_price}",
            evidence=evidence,
        )
    if side_key == "short" and sl_value <= price_value:
        return StrategyGateDecision(
            False,
            "market_microstructure",
            f"空单止损价{stop_loss}<=开仓价{entry_price}",
            evidence=evidence,
        )
    return StrategyGateDecision(True, "market_microstructure", "market_microstructure_ok", evidence=evidence)


def evaluate_a_v11_releasable_position(
    *,
    new_score: float,
    new_symbol: str,
    new_side: str,
    old_tf: str,
    old_symbol: str,
    old_side: str,
    old_score: float,
    pnl_pct: float,
    age_min: int,
    same_side_required: bool,
    preferred_tf: str | None,
    require_preferred_tf: bool,
    strong_signal_threshold: float,
    elite_score: float,
    min_age_minutes: int,
    elite_min_age_minutes: int,
    score_gap: float,
    soft_protect_pnl_pct: float,
    soft_protect_score_gap: float,
    hard_protect_pnl_pct: float,
) -> StrategyGateDecision:
    """Evaluate whether an existing A/v11 position can be released for a stronger signal."""
    abs_new_score = abs(float(new_score))
    if abs_new_score < float(strong_signal_threshold):
        return StrategyGateDecision(False, "position_replacement", "replacement_signal_too_weak", adjusted_score=abs_new_score)

    old_symbol_key = str(old_symbol or "")
    new_symbol_key = str(new_symbol or "")
    old_side_key = str(old_side or "").lower()
    new_side_key = str(new_side or "").lower()
    preferred_tf_key = str(preferred_tf or "")
    old_tf_key = str(old_tf or "")
    is_elite = abs_new_score >= float(elite_score)
    min_age = int(elite_min_age_minutes if is_elite else min_age_minutes)

    evidence = {
        "min_age_required": min_age,
        "gap_required": 0,
        "is_elite": is_elite,
        "tf_penalty": 0 if preferred_tf_key and old_tf_key == preferred_tf_key else 1,
        "side_penalty": 0 if old_side_key == new_side_key else 1,
    }

    if old_symbol_key == new_symbol_key:
        return StrategyGateDecision(False, "position_replacement", "same_symbol_not_releasable", adjusted_score=abs_new_score, evidence=evidence)
    if require_preferred_tf and preferred_tf_key and old_tf_key != preferred_tf_key:
        return StrategyGateDecision(False, "position_replacement", "not_preferred_timeframe", adjusted_score=abs_new_score, evidence=evidence)
    if same_side_required and old_side_key != new_side_key:
        return StrategyGateDecision(False, "position_replacement", "same_side_required", adjusted_score=abs_new_score, evidence=evidence)
    if int(age_min) < min_age:
        return StrategyGateDecision(False, "position_replacement", "position_too_young", adjusted_score=abs_new_score, evidence=evidence)
    if float(pnl_pct) >= float(hard_protect_pnl_pct):
        return StrategyGateDecision(False, "position_replacement", "hard_profit_protected", adjusted_score=abs_new_score, evidence=evidence)

    old_abs_score = abs(float(old_score or 0))
    gap_required = 0.0 if float(pnl_pct) <= 0 else float(score_gap)
    if float(pnl_pct) >= float(soft_protect_pnl_pct):
        gap_required = float(soft_protect_score_gap)
        evidence["gap_required"] = gap_required
        if old_abs_score <= 0 and not is_elite:
            return StrategyGateDecision(False, "position_replacement", "soft_profit_recovery_protected", adjusted_score=abs_new_score, evidence=evidence)
    evidence["gap_required"] = gap_required

    if old_abs_score > 0 and abs_new_score < old_abs_score + gap_required:
        return StrategyGateDecision(False, "position_replacement", "score_gap_insufficient", adjusted_score=abs_new_score, evidence=evidence)

    evidence["release_rank"] = (
        evidence["tf_penalty"],
        evidence["side_penalty"],
        float(pnl_pct),
        old_abs_score,
        -int(age_min),
    )
    return StrategyGateDecision(True, "position_replacement", "releasable_position", adjusted_score=abs_new_score, evidence=evidence)


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


def evaluate_c_v14_stale_entry_price_gate(
    *,
    recent_prices: Collection[Any],
    repeated_count: int = 3,
) -> StrategyGateDecision:
    """Evaluate the C/v14 guard for repeated identical entry prices."""
    window_size = int(repeated_count)
    prices = list(recent_prices or [])
    if window_size <= 0 or len(prices) < window_size:
        return StrategyGateDecision(True, "market_data_guard", "entry_price_fresh")
    window = prices[-window_size:]
    if len(set(window)) == 1:
        return StrategyGateDecision(
            False,
            "market_data_guard",
            "入场价连续3次相同，疑似数据冻结",
            evidence={"recent_prices": window, "repeated_count": window_size},
        )
    return StrategyGateDecision(True, "market_data_guard", "entry_price_fresh", evidence={"recent_prices": window})


def evaluate_c_v14_market_microstructure_gate(
    *,
    atr: float,
) -> StrategyGateDecision:
    """Evaluate C/v14 pre-open market-data sanity gates."""
    atr_value = float(atr or 0)
    if atr_value <= 0:
        return StrategyGateDecision(
            False,
            "market_microstructure",
            "ATR=0",
            evidence={"atr": atr_value},
        )
    return StrategyGateDecision(True, "market_microstructure", "market_microstructure_ok", evidence={"atr": atr_value})


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
