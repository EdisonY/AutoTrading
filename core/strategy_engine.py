"""Unified strategy engine interfaces.

The concrete strategy algorithms stay inside each scanner module.  This layer
only standardizes how scanners call them and how their loose dict signals are
converted into shared review records.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .models import SignalRecord


AnalyzeFn = Callable[[str, str], dict[str, Any] | None]


@dataclass(slots=True)
class StrategyEngine:
    name: str
    analyze_fn: AnalyzeFn

    def analyze(self, symbol: str, timeframe: str) -> dict[str, Any] | None:
        return self.analyze_fn(symbol, timeframe)

    def to_signal_record(self, signal: dict[str, Any]) -> SignalRecord:
        return SignalRecord.from_row(self.name, signal)

