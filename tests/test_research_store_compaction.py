import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    path = ROOT / "部署工具" / "research_store_compaction.py"
    spec = importlib.util.spec_from_file_location("research_store_compaction_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_jsonl_partition(store: Path, table: str, day: str, rows: list[dict[str, object]]) -> Path:
    path = store / table / f"date={day}" / "data.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False, separators=(", ", ": ")) for row in rows), encoding="utf-8")
    return path


class ResearchStoreCompactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def test_plan_only_marks_large_jsonl_without_rewriting(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = base / "research_store"
            data_file = write_jsonl_partition(store, "events", "2026-06-03", [{"id": 1}, {"id": 2}])
            original = data_file.read_text(encoding="utf-8")

            rc = self.tool.main(
                [
                    "--store",
                    str(store),
                    "--backup-dir",
                    str(base / "research_store_compaction_backup"),
                    "--runtime-dir",
                    str(base / "runtime"),
                    "--reports-dir",
                    str(base / "reports"),
                    "--tables",
                    "events",
                    "--format",
                    "jsonl",
                    "--min-bytes",
                    "1",
                    "--min-rows",
                    "100",
                ]
            )

            payload = json.loads((base / "runtime" / "research_store_compaction_latest.json").read_text(encoding="utf-8"))
            self.assertEqual(rc, 0)
            self.assertEqual(payload["summary"]["compact_candidates"], 1)
            self.assertEqual(payload["partitions"][0]["status"], "compact_candidate")
            self.assertIn("large_file", payload["partitions"][0]["reasons"])
            self.assertEqual(data_file.read_text(encoding="utf-8"), original)
            self.assertFalse((base / "research_store_compaction_backup").exists())
            self.assertTrue((base / "reports" / "research_store_compaction_latest.md").exists())

    def test_apply_backs_up_and_rewrites_jsonl_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = base / "research_store"
            backup = base / "research_store_compaction_backup"
            data_file = write_jsonl_partition(store, "klines", "2026-06-03", [{"id": 1, "value": "x"}])
            original = data_file.read_text(encoding="utf-8")

            rc = self.tool.main(
                [
                    "--store",
                    str(store),
                    "--backup-dir",
                    str(backup),
                    "--runtime-dir",
                    str(base / "runtime"),
                    "--reports-dir",
                    str(base / "reports"),
                    "--tables",
                    "klines",
                    "--format",
                    "jsonl",
                    "--min-bytes",
                    "1",
                    "--min-rows",
                    "100",
                    "--apply",
                ]
            )

            payload = json.loads((base / "runtime" / "research_store_compaction_latest.json").read_text(encoding="utf-8"))
            self.assertEqual(rc, 0)
            self.assertTrue(payload["apply_enabled"])
            self.assertEqual(payload["summary"]["apply_compacted"], 1)
            self.assertEqual(payload["apply"]["compacted_partitions"][0]["rewritten_rows"], 1)
            self.assertIn('"id":1', data_file.read_text(encoding="utf-8"))
            backup_files = list(backup.glob("*/klines/date=2026-06-03/data.jsonl"))
            self.assertEqual(len(backup_files), 1)
            self.assertEqual(backup_files[0].read_text(encoding="utf-8"), original)

    def test_ok_partition_is_not_compacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = base / "research_store"
            data_file = write_jsonl_partition(store, "depth_snapshots", "2026-06-03", [{"id": 1}])

            rows = self.tool.scan_store(
                store,
                fmt="jsonl",
                target_format="same",
                tables=["depth_snapshots"],
                min_bytes=data_file.stat().st_size + 1,
                min_rows=10,
                max_row_groups=32,
            )
            summary = self.tool.summarize(rows)

            self.assertEqual(rows[0]["status"], "ok")
            self.assertEqual(summary["compact_candidates"], 0)


if __name__ == "__main__":
    unittest.main()
