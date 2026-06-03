import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    path = ROOT / "部署工具" / "research_store_retention.py"
    spec = importlib.util.spec_from_file_location("research_store_retention_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_jsonl_partition(store: Path, table: str, day: str, rows: list[dict[str, object]]) -> Path:
    path = store / table / f"date={day}" / "data.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    return path


class ResearchStoreRetentionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def test_plan_only_classifies_partitions_without_moving(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = base / "research_store"
            hot = write_jsonl_partition(store, "events", "2026-06-03", [{"id": 1}])
            warm = write_jsonl_partition(store, "events", "2026-05-30", [{"id": 2}])
            old = write_jsonl_partition(store, "events", "2026-05-20", [{"id": 3}, {"id": 4}])

            rc = self.tool.main(
                [
                    "--store",
                    str(store),
                    "--archive-dir",
                    str(base / "research_store_archive"),
                    "--runtime-dir",
                    str(base / "runtime"),
                    "--reports-dir",
                    str(base / "reports"),
                    "--tables",
                    "events",
                    "--hot-days",
                    "2",
                    "--retain-days",
                    "7",
                    "--format",
                    "jsonl",
                    "--now",
                    "2026-06-04T00:00:00+08:00",
                ]
            )

            payload = json.loads((base / "runtime" / "research_store_retention_latest.json").read_text(encoding="utf-8"))
            statuses = {row["date"]: row["status"] for row in payload["partitions"]}
            rows_by_day = {row["date"]: row for row in payload["partitions"]}
            self.assertEqual(rc, 0)
            self.assertTrue(hot.exists())
            self.assertTrue(warm.exists())
            self.assertTrue(old.exists())
            self.assertEqual(statuses["2026-06-03"], "hot")
            self.assertEqual(statuses["2026-05-30"], "warm")
            self.assertEqual(statuses["2026-05-20"], "archive_candidate")
            self.assertEqual(rows_by_day["2026-05-20"]["row_count"], 2)
            self.assertFalse(payload["apply_enabled"])
            self.assertEqual(payload["summary"]["archive_candidate_partitions"], 1)
            self.assertTrue((base / "reports" / "research_store_retention_latest.md").exists())

    def test_apply_moves_archive_candidates_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = base / "research_store"
            archive = base / "research_store_archive"
            current = write_jsonl_partition(store, "klines", "2026-06-03", [{"id": 1}])
            old = write_jsonl_partition(store, "klines", "2026-05-01", [{"id": 2}])

            rc = self.tool.main(
                [
                    "--store",
                    str(store),
                    "--archive-dir",
                    str(archive),
                    "--runtime-dir",
                    str(base / "runtime"),
                    "--reports-dir",
                    str(base / "reports"),
                    "--tables",
                    "klines",
                    "--hot-days",
                    "2",
                    "--retain-days",
                    "14",
                    "--format",
                    "jsonl",
                    "--now",
                    "2026-06-04T00:00:00+08:00",
                    "--apply",
                ]
            )

            payload = json.loads((base / "runtime" / "research_store_retention_latest.json").read_text(encoding="utf-8"))
            self.assertEqual(rc, 0)
            self.assertTrue(current.exists())
            self.assertFalse(old.exists())
            archived = archive / "klines" / "date=2026-05-01" / "data.jsonl"
            self.assertTrue(archived.exists())
            self.assertTrue(payload["apply_enabled"])
            self.assertEqual(payload["summary"]["apply_moved"], 1)
            self.assertEqual(payload["apply"]["moved_partitions"][0]["archive_path"], str(archive / "klines" / "date=2026-05-01"))

    def test_scan_summarizes_multiple_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = base / "research_store"
            write_jsonl_partition(store, "events", "2026-05-01", [{"id": 1}])
            write_jsonl_partition(store, "depth_snapshots", "2026-06-03", [{"id": 2}])

            rows = self.tool.scan_store(
                store,
                fmt="jsonl",
                tables=["events", "depth_snapshots"],
                hot_days=2,
                retain_days=14,
                now_dt=self.tool.parse_now("2026-06-04T00:00:00+08:00"),
            )
            summary = self.tool.summarize(rows)

            self.assertEqual(summary["partitions"], 2)
            self.assertEqual(summary["by_status"]["archive_candidate"], 1)
            self.assertEqual(summary["by_status"]["hot"], 1)
            by_table = {row["table"]: row for row in summary["by_table"]}
            self.assertEqual(by_table["events"]["archive_candidate"], 1)
            self.assertEqual(by_table["depth_snapshots"]["hot"], 1)


if __name__ == "__main__":
    unittest.main()
