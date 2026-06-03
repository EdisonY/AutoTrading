import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from core.replay_depth_cache import default_depth_cache_dirs, load_depth_snapshot


class ReplayDepthCacheTests(unittest.TestCase):
    def write_depth_partition(self, root: Path, rows: list[dict[str, object]]) -> None:
        path = root / "research_store" / "depth_snapshots" / "date=2026-06-03" / "data.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    def test_loads_research_store_depth_snapshot_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_depth_partition(
                root,
                [
                    {
                        "symbol": "ABCUSDT",
                        "snapshot_time": "2026-06-03T10:00:00+08:00",
                        "snapshot_time_ms": 1780452000000,
                        "bids_json": json.dumps([["99.5", "2"]]),
                        "asks_json": json.dumps([["100", "1"], ["101", "3"]]),
                    }
                ],
            )

            snapshot = load_depth_snapshot(
                "ABCUSDT",
                datetime.fromisoformat("2026-06-03T10:00:30+08:00"),
                side="long",
                cache_dirs=default_depth_cache_dirs(root),
                max_age_seconds=60,
            )

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.symbol, "ABCUSDT")
        self.assertEqual(snapshot.order_book["asks"][0], ["100", "1"])
        self.assertAlmostEqual(snapshot.age_seconds, 30.0)
        self.assertIn("research_store", snapshot.source)
        self.assertIn("depth_snapshots", snapshot.source)

    def test_prefers_nearest_snapshot_across_runtime_and_research_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "runtime" / "depth_cache"
            cache_dir.mkdir(parents=True)
            (cache_dir / "ABCUSDT_latest.json").write_text(
                json.dumps(
                    {
                        "symbol": "ABCUSDT",
                        "ts": "2026-06-03T09:59:00+08:00",
                        "bids": [["99", "4"]],
                        "asks": [["100.5", "4"]],
                    }
                ),
                encoding="utf-8",
            )
            self.write_depth_partition(
                root,
                [
                    {
                        "symbol": "ABCUSDT",
                        "snapshot_time": "2026-06-03T10:00:25+08:00",
                        "bids_json": json.dumps([["99.8", "2"]]),
                        "asks_json": json.dumps([["100", "1"]]),
                    }
                ],
            )

            snapshot = load_depth_snapshot(
                "ABCUSDT",
                datetime.fromisoformat("2026-06-03T10:00:30+08:00"),
                side="long",
                cache_dirs=default_depth_cache_dirs(root),
                max_age_seconds=120,
            )

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.order_book["asks"][0], ["100", "1"])
        self.assertAlmostEqual(snapshot.age_seconds, 5.0)
        self.assertIn("research_store", snapshot.source)

    def test_ignores_research_snapshot_without_fillable_side_or_with_old_age(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_depth_partition(
                root,
                [
                    {
                        "symbol": "ABCUSDT",
                        "snapshot_time": "2026-06-03T09:00:00+08:00",
                        "bids_json": json.dumps([["99.5", "2"]]),
                        "asks_json": json.dumps([["100", "1"]]),
                    },
                    {
                        "symbol": "ABCUSDT",
                        "snapshot_time": "2026-06-03T10:00:00+08:00",
                        "bids_json": json.dumps([["99.5", "2"]]),
                        "asks_json": json.dumps([]),
                    },
                ],
            )

            snapshot = load_depth_snapshot(
                "ABCUSDT",
                datetime.fromisoformat("2026-06-03T10:00:30+08:00"),
                side="long",
                cache_dirs=default_depth_cache_dirs(root),
                max_age_seconds=60,
            )

        self.assertIsNone(snapshot)


if __name__ == "__main__":
    unittest.main()
