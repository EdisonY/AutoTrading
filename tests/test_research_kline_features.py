import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    path = ROOT / "部署工具" / "research_kline_features.py"
    spec = importlib.util.spec_from_file_location("research_kline_features_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def ms(ts: str) -> int:
    return int(datetime.fromisoformat(ts).replace(tzinfo=timezone.utc).timestamp() * 1000)


class ResearchKlineFeaturesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def test_dedupe_keeps_newer_cache_row(self):
        rows = [
            {"symbol": "ABCUSDT", "interval": "1m", "open_time_ms": 1, "cache_ts": "2026-06-01T00:00:00+08:00", "close": 100},
            {"symbol": "ABCUSDT", "interval": "1m", "open_time_ms": 1, "cache_ts": "2026-06-01T00:01:00+08:00", "close": 101},
        ]

        merged = self.tool.dedupe_kline_rows(rows)

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["close"], 101)

    def test_export_merges_existing_partitions_with_new_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cache_dir = base / "cache"
            out_dir = base / "research_store"
            cache_dir.mkdir()
            old_ms = ms("2026-06-01T00:00:00")
            dup_ms = ms("2026-06-01T00:01:00")
            new_ms = ms("2026-06-01T00:02:00")
            old_partition = out_dir / "klines" / "date=2026-06-01" / "data.jsonl"
            old_partition.parent.mkdir(parents=True)
            old_rows = [
                {
                    "symbol": "ABCUSDT",
                    "interval": "1m",
                    "limit": 500,
                    "date": "2026-06-01",
                    "open_time": "2026-06-01T08:00:00+08:00",
                    "open_time_ms": old_ms,
                    "close_time_ms": old_ms + 59_999,
                    "open": 100,
                    "high": 101,
                    "low": 99,
                    "close": 100,
                    "volume": 1,
                    "quote_volume": 100,
                    "cache_ts": "2026-06-01T08:00:00+08:00",
                    "source_file": "old.json",
                },
                {
                    "symbol": "ABCUSDT",
                    "interval": "1m",
                    "limit": 500,
                    "date": "2026-06-01",
                    "open_time": "2026-06-01T08:01:00+08:00",
                    "open_time_ms": dup_ms,
                    "close_time_ms": dup_ms + 59_999,
                    "open": 100,
                    "high": 101,
                    "low": 99,
                    "close": 100,
                    "volume": 1,
                    "quote_volume": 100,
                    "cache_ts": "2026-06-01T08:00:00+08:00",
                    "source_file": "old.json",
                },
            ]
            old_partition.write_text("\n".join(json.dumps(row) for row in old_rows), encoding="utf-8")
            cache_payload = {
                "ts": datetime(2026, 6, 1, 8, 5, tzinfo=self.tool.CST).timestamp(),
                "rows": [
                    [dup_ms, "100", "102", "99", "101", "2", dup_ms + 59_999, "202"],
                    [new_ms, "101", "104", "100", "103", "3", new_ms + 59_999, "309"],
                ],
            }
            (cache_dir / "ABCUSDT_1m_500.json").write_text(json.dumps(cache_payload), encoding="utf-8")

            rc = self.tool.main([
                "--cache-dir",
                str(cache_dir),
                "--out-dir",
                str(out_dir),
                "--days",
                "365",
                "--format",
                "jsonl",
            ])

            self.assertEqual(rc, 0)
            manifest = json.loads((out_dir / "kline_features_manifest_latest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["existing_rows"], 2)
            self.assertEqual(manifest["cache_rows"], 2)
            self.assertEqual(manifest["merged_rows"], 3)
            merged_rows = [
                json.loads(line)
                for line in (out_dir / "klines" / "date=2026-06-01" / "data.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([row["open_time_ms"] for row in merged_rows], [old_ms, dup_ms, new_ms])
            self.assertEqual([row["close"] for row in merged_rows], [100, 101, 103])
            features = [
                json.loads(line)
                for line in (out_dir / "features" / "date=2026-06-01" / "data.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(features), 3)
            self.assertAlmostEqual(features[1]["return_1_pct"], 1.0)
            self.assertAlmostEqual(features[2]["return_1_pct"], 1.980198)
            self.assertEqual(features[2]["bar_index_in_series"], 2)


if __name__ == "__main__":
    unittest.main()
