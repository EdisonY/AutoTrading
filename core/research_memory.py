"""Persistent research memory helpers.

The research memory is read-only for live trading. It records review cases,
candidate hypotheses, and lessons so strategy changes can be tested in shadow
before touching scanner code.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CST = timezone(timedelta(hours=8))


@dataclass(slots=True)
class ResearchCase:
    case_id: str
    date: str
    strategy: str
    symbol: str
    case_type: str
    side: str = ""
    expected_direction: str = ""
    actual_action: str = ""
    pnl_usd: float = 0.0
    pnl_pct: float = 0.0
    move_pct: float = 0.0
    score: float = 0.0
    market_stage: str = ""
    attribution: str = ""
    decision_category: str = ""
    reason: str = ""
    lesson: str = ""
    confidence: float = 0.5
    source: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(CST).isoformat(timespec="seconds"))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CandidateHypothesis:
    candidate_id: str
    date: str
    strategy: str
    source_cases: list[str]
    problem: str
    proposal: str
    change_type: str
    params: dict[str, Any] = field(default_factory=dict)
    risk: str = ""
    expected_effect: str = ""
    promotion_gate: dict[str, Any] = field(default_factory=dict)
    status: str = "generated"
    family_id: str = ""
    parent_candidate_id: str = ""
    generation: int = 1
    governance_status: str = "active"
    created_at: str = field(default_factory=lambda: datetime.now(CST).isoformat(timespec="seconds"))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class PromotionReview:
    review_id: str
    date: str
    candidate_id: str
    experiment_id: str
    base_strategy: str
    change_type: str
    promotion_status: str
    decision: str
    reason: str = ""
    manual_review_required: bool = True
    source_cases: list[str] = field(default_factory=list)
    shadow_pnl: float = 0.0
    original_pnl: float = 0.0
    hard_stop_before: int = 0
    hard_stop_after: int = 0
    family_id: str = ""
    manual_action: str = ""
    approved_scope: str = ""
    next_step: str = ""
    risk_notes: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(CST).isoformat(timespec="seconds"))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def ensure_memory_tree(root: Path) -> None:
    for rel in (
        "cases",
        "hypotheses",
        "lessons",
        "snapshots",
        "promotions",
        "approvals",
    ):
        (root / rel).mkdir(parents=True, exist_ok=True)


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


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


def load_candidates(path: Path) -> list[CandidateHypothesis]:
    candidates: list[CandidateHypothesis] = []
    for row in read_jsonl(path):
        try:
            candidates.append(
                CandidateHypothesis(
                    candidate_id=str(row.get("candidate_id") or ""),
                    date=str(row.get("date") or ""),
                    strategy=str(row.get("strategy") or ""),
                    source_cases=[str(x) for x in row.get("source_cases") or []],
                    problem=str(row.get("problem") or ""),
                    proposal=str(row.get("proposal") or ""),
                    change_type=str(row.get("change_type") or ""),
                    params=dict(row.get("params") or {}),
                    risk=str(row.get("risk") or ""),
                    expected_effect=str(row.get("expected_effect") or ""),
                    promotion_gate=dict(row.get("promotion_gate") or {}),
                    status=str(row.get("status") or "generated"),
                    family_id=str(row.get("family_id") or ""),
                    parent_candidate_id=str(row.get("parent_candidate_id") or ""),
                    generation=int(row.get("generation") or 1),
                    governance_status=str(row.get("governance_status") or "active"),
                    created_at=str(row.get("created_at") or datetime.now(CST).isoformat(timespec="seconds")),
                )
            )
        except Exception:
            continue
    return candidates


def stable_id(*parts: Any) -> str:
    text = "-".join(str(p or "").strip().replace(" ", "_") for p in parts)
    return "".join(ch for ch in text if ch.isalnum() or ch in "-_./").replace("/", "-")
