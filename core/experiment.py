"""Offline shadow experiment helpers.

Shadow experiments are intentionally read-only.  They use synced logs to ask
"what would have happened if this filter or threshold had existed?", without
creating new live strategy processes.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CST = timezone(timedelta(hours=8))


@dataclass(slots=True)
class ExperimentSpec:
    experiment_id: str
    base_strategy: str
    hypothesis: str
    change_type: str
    params: dict[str, Any] = field(default_factory=dict)
    status: str = "candidate"
    candidate_id: str = ""
    source_cases: list[str] = field(default_factory=list)
    family_id: str = ""
    parent_experiment_id: str = ""
    generation: int = 1
    governance_status: str = "active"
    created_at: str = field(default_factory=lambda: datetime.now(CST).isoformat(timespec="seconds"))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ExperimentResult:
    experiment_id: str
    base_strategy: str
    sample_window: str
    sample_trades: int
    original_pnl: float
    shadow_pnl: float
    filtered_trades: int
    avoided_loss: float
    missed_profit: float
    hard_stop_before: int = 0
    hard_stop_after: int = 0
    promotion_status: str = "observe"
    candidate_id: str = ""
    source_cases: list[str] = field(default_factory=list)
    change_type: str = ""
    gate_passed: bool = False
    family_id: str = ""
    generation: int = 1
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def is_hard_stop(row: dict[str, Any]) -> bool:
    text = str(row.get("exit_reason") or row.get("reason") or "")
    return any(key in text for key in ("最大亏损", "硬顶", "硬底", "强平", "max loss"))


def win_rate(trades: list[dict[str, Any]]) -> float:
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if to_float(t.get("pnl_usd")) > 0)
    return wins / len(trades) * 100.0


def default_experiments() -> list[ExperimentSpec]:
    return [
        ExperimentSpec(
            experiment_id="EXP-20260523-v14-tail-guard",
            base_strategy="C/v14",
            hypothesis="v14 低分高位追涨/追跌容易演化为硬顶尾部亏损，收紧阈值与尾部过滤应减少大亏。",
            change_type="filter",
            params={
                "min_score_1h": 55,
                "confirm_min_score": 35,
                "tail_guard_min_score": 60,
            },
            family_id="FAM-C-v14-filter-tail-guard",
        ),
        ExperimentSpec(
            experiment_id="EXP-20260523-v16-confirm-soft-pass",
            base_strategy="B/v16",
            hypothesis="v16 部分 1h 中高分信号被 15m 无确认/弱确认卡住，应统计可软放行候选并进入影子观察。",
            change_type="confirmation_policy",
            params={
                "no_confirm_high_score_pass": 50,
                "weak_confirm_pass_score": 44,
                "opposite_high_score_pass": 65,
            },
            family_id="FAM-B-v16-confirmation-policy",
        ),
        ExperimentSpec(
            experiment_id="EXP-20260523-v11-replacement-quality",
            base_strategy="A/v11",
            hypothesis="v11 满仓替换应只释放明显弱仓，否则会放大噪音止损和资金占用。",
            change_type="replacement_policy",
            params={
                "strong_signal_min_score": 112,
                "min_score_gap": 25,
                "weak_position_max_pnl_pct": 2.0,
            },
            family_id="FAM-A-v11-replacement-quality",
        ),
        # Phase 3: A/v11 evidence program
        ExperimentSpec(
            experiment_id="EXP-20260527-v11-entry-threshold-15m-115",
            base_strategy="A/v11",
            hypothesis="A/v11 15m 阈值从 105 提高到 115，减少低质量短线入场和噪音止损。",
            change_type="threshold",
            params={"score_threshold_15m": 115},
            family_id="FAM-A-v11-entry-threshold",
        ),
        ExperimentSpec(
            experiment_id="EXP-20260527-v11-entry-threshold-15m-120",
            base_strategy="A/v11",
            hypothesis="A/v11 15m 阈值从 105 提高到 120，进一步收紧短线入场。",
            change_type="threshold",
            params={"score_threshold_15m": 120},
            family_id="FAM-A-v11-entry-threshold",
        ),
        ExperimentSpec(
            experiment_id="EXP-20260527-v11-trailing-pullback-0p8",
            base_strategy="A/v11",
            hypothesis="A/v11 15m 浮动止损回撤从 0.6 ATR 放宽到 0.8 ATR，减少噪音止损。",
            change_type="stop_loss",
            params={"trail_pullback_15m": 0.8},
            family_id="FAM-A-v11-trailing-pullback",
        ),
        ExperimentSpec(
            experiment_id="EXP-20260527-v11-trailing-pullback-1p0",
            base_strategy="A/v11",
            hypothesis="A/v11 15m 浮动止损回撤从 0.6 ATR 放宽到 1.0 ATR，进一步减少噪音止损。",
            change_type="stop_loss",
            params={"trail_pullback_15m": 1.0},
            family_id="FAM-A-v11-trailing-pullback",
        ),
        # Phase 4: B/v16 payoff improvement
        ExperimentSpec(
            experiment_id="EXP-20260527-v16-atr-stop-bands",
            base_strategy="B/v16",
            hypothesis="B/v16 分档止损：高波动(>3%)收紧到 1.5 ATR，低波动(<2%)放宽到 2.5 ATR。",
            change_type="stop_loss",
            params={
                "sl_mult_high_vol": 1.5,
                "sl_mult_normal": 2.0,
                "sl_mult_low_vol": 2.5,
                "high_vol_threshold": 3.0,
                "low_vol_threshold": 2.0,
            },
            family_id="FAM-B-v16-atr-stop-bands",
        ),
        ExperimentSpec(
            experiment_id="EXP-20260527-v16-overheat-cap-85",
            base_strategy="B/v16",
            hypothesis="B/v16 分数 >85 的过热信号因反转风险导致低收益，测试封顶效果。",
            change_type="threshold",
            params={"score_max": 85},
            family_id="FAM-B-v16-overheat-cap",
        ),
        # Phase 5: C/v14 rebuild/retire
        ExperimentSpec(
            experiment_id="EXP-20260527-v14-strict-candidate-long65",
            base_strategy="C/v14",
            hypothesis="C/v14 只让 1h 候选分数 >=65 的多头进入候选日志，压缩信号噪声。",
            change_type="threshold",
            params={"score_threshold_1h_long": 65, "score_threshold_1h_short": 70},
            family_id="FAM-C-v14-candidate-compression",
        ),
        ExperimentSpec(
            experiment_id="EXP-20260527-v14-strict-candidate-long70",
            base_strategy="C/v14",
            hypothesis="C/v14 只让 1h 候选分数 >=70 的多头进入候选日志，进一步压缩。",
            change_type="threshold",
            params={"score_threshold_1h_long": 70, "score_threshold_1h_short": 75},
            family_id="FAM-C-v14-candidate-compression",
        ),
        ExperimentSpec(
            experiment_id="EXP-20260527-v14-filter-ablation-sector",
            base_strategy="C/v14",
            hypothesis="C/v14 移除同赛道限制(MAX_POS_PER_SECTOR=2)，测试赛道分散是否保护PnL。",
            change_type="filter",
            params={"disable_sector_limit": True},
            family_id="FAM-C-v14-filter-ablation",
        ),
        ExperimentSpec(
            experiment_id="EXP-20260527-v14-filter-ablation-btc-trend",
            base_strategy="C/v14",
            hypothesis="C/v14 移除 BTC 大盘趋势过滤，测试逆势惩罚是否保护PnL。",
            change_type="filter",
            params={"disable_btc_trend_filter": True},
            family_id="FAM-C-v14-filter-ablation",
        ),
    ]
