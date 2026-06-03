import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    path = ROOT / "部署工具" / "research_store_query.py"
    spec = importlib.util.spec_from_file_location("research_store_query_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_kline_partition(store: Path, rows: list[dict[str, object]]) -> None:
    by_date: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        by_date.setdefault(str(row["date"]), []).append(row)
    for day, day_rows in by_date.items():
        path = store / "klines" / f"date={day}" / "data.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(row) for row in day_rows), encoding="utf-8")


def write_depth_partition(store: Path, rows: list[dict[str, object]]) -> None:
    by_date: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        by_date.setdefault(str(row["date"]), []).append(row)
    for day, day_rows in by_date.items():
        path = store / "depth_snapshots" / f"date={day}" / "data.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(row) for row in day_rows), encoding="utf-8")


def kline_rows(intervals: list[str], days: int) -> list[dict[str, object]]:
    start = datetime.now().date() - timedelta(days=days - 1)
    rows: list[dict[str, object]] = []
    for interval in intervals:
        for offset in range(days):
            day = start + timedelta(days=offset)
            open_time = f"{day.isoformat()}T08:00:00+08:00"
            rows.append(
                {
                    "symbol": "ABCUSDT",
                    "interval": interval,
                    "date": day.isoformat(),
                    "open_time": open_time,
                    "open_time_ms": int(datetime.combine(day, datetime.min.time()).timestamp() * 1000),
                    "open": 100,
                    "high": 101,
                    "low": 99,
                    "close": 100,
                    "volume": 1,
                    "quote_volume": 100,
                }
            )
    return rows


@unittest.skipUnless(importlib.util.find_spec("duckdb"), "duckdb not installed")
class ResearchStoreQueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()
        import duckdb

        cls.duckdb = duckdb

    def build_summary(self, store: Path, days: int = 45) -> dict[str, object]:
        with self.duckdb.connect(database=":memory:") as con:
            available = {
                "klines": self.tool.register_view(con, store, "klines", "jsonl"),
                "depth_snapshots": self.tool.register_view(con, store, "depth_snapshots", "jsonl"),
            }
            return self.tool.build_summary(
                con,
                available,
                days=days,
                kline_target_days=30,
                kline_key_intervals=["15m", "30m", "1h"],
            )

    def test_kline_acceptance_ok_when_key_intervals_have_30_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "research_store"
            write_kline_partition(store, kline_rows(["15m", "30m", "1h"], 30))

            summary = self.build_summary(store)

            acceptance = summary["kline_acceptance"]
            self.assertEqual(acceptance["status"], "ok")
            self.assertTrue(acceptance["target_met"])
            self.assertEqual(acceptance["met_required_interval_count"], 3)
            by_interval = {row["interval"]: row for row in summary["kline_coverage"]}
            self.assertEqual(by_interval["15m"]["coverage_days"], 30)
            self.assertTrue(by_interval["1h"]["target_met"])

    def test_kline_acceptance_reports_short_coverage_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "research_store"
            write_kline_partition(store, kline_rows(["15m", "30m", "1h"], 12))

            summary = self.build_summary(store)

            acceptance = summary["kline_acceptance"]
            self.assertEqual(acceptance["status"], "coverage_gap")
            self.assertEqual(set(acceptance["gap_intervals"]), {"15m", "30m", "1h"})
            self.assertFalse(acceptance["target_met"])

    def test_kline_acceptance_reports_missing_key_interval(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "research_store"
            write_kline_partition(store, kline_rows(["15m", "1h"], 30))

            summary = self.build_summary(store)

            acceptance = summary["kline_acceptance"]
            self.assertEqual(acceptance["status"], "coverage_gap")
            self.assertEqual(acceptance["missing_intervals"], ["30m"])
            self.assertEqual(acceptance["met_required_interval_count"], 2)

    def test_depth_snapshot_coverage_summarizes_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "research_store"
            write_depth_partition(
                store,
                [
                    {
                        "symbol": "ABCUSDT",
                        "date": "2026-06-04",
                        "snapshot_time": "2026-06-04T00:00:00+08:00",
                        "bid_levels": 2,
                        "ask_levels": 3,
                        "spread_bps": 1.5,
                    },
                    {
                        "symbol": "ABCUSDT",
                        "date": "2026-06-04",
                        "snapshot_time": "2026-06-04T00:05:00+08:00",
                        "bid_levels": 4,
                        "ask_levels": 5,
                        "spread_bps": 2.5,
                    },
                ],
            )

            summary = self.build_summary(store)

            self.assertEqual(len(summary["depth_coverage"]), 1)
            row = summary["depth_coverage"][0]
            self.assertEqual(row["symbol"], "ABCUSDT")
            self.assertEqual(row["snapshots"], 2)
            self.assertEqual(row["max_bid_levels"], 4)
            self.assertEqual(row["max_ask_levels"], 5)
            self.assertEqual(row["avg_spread_bps"], 2.0)


if __name__ == "__main__":
    unittest.main()
