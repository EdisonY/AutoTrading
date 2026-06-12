import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    path = ROOT / "部署工具" / "local_research_pipeline.py"
    spec = importlib.util.spec_from_file_location("local_research_pipeline_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class LocalResearchPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def test_download_complete_requires_done_queue(self):
        self.assertTrue(
            self.tool.download_complete(
                {"status": "complete", "progress": {"pending_tasks": 0, "failed_requests": 0}, "quality": {}}
            )
        )
        self.assertTrue(
            self.tool.download_complete(
                {"status": "complete_with_provider_gaps", "progress": {"pending_tasks": 5, "failed_requests": 0}, "quality": {"task_queue_complete": True}}
            )
        )
        self.assertFalse(
            self.tool.download_complete(
                {"status": "paused_time_budget", "progress": {"pending_tasks": 1, "failed_requests": 0}, "quality": {"task_queue_complete": False}}
            )
        )
        self.assertFalse(
            self.tool.download_complete(
                {"status": "complete", "progress": {"pending_tasks": 0, "failed_requests": 1}, "quality": {"task_queue_complete": False}}
            )
        )

    def test_commands_are_local_safe(self):
        args = SimpleNamespace(
            top_n=30,
            days=730,
            intervals="15m,30m,1h,4h",
            providers="bybit",
            max_rps=0.2,
            batch_requests=240,
            batch_runtime_sec=1200,
            request_timeout=8.0,
            flush_requests=10,
            download_prefix="historical_kline_backfill_2y_local",
            backtest_stage="full-2y-v1",
            all_combos=True,
            max_combos=120,
        )
        download = self.tool.make_download_cmd(args)
        backtest = self.tool.make_backtest_cmd(args)

        self.assertIn("historical_kline_backfill.py", " ".join(download))
        self.assertIn("--providers", download)
        self.assertIn("bybit", download)
        self.assertIn("--request-timeout", download)
        self.assertIn("8.0", download)
        self.assertNotIn("binance", " ".join(download).lower())
        self.assertIn("--all-combos", backtest)
        self.assertIn("indicator_factory.py", " ".join(backtest))

    def test_save_state_writes_json_and_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_json = self.tool.PIPELINE_JSON
            old_md = self.tool.PIPELINE_MD
            try:
                self.tool.PIPELINE_JSON = root / "runtime" / "local_research_pipeline_latest.json"
                self.tool.PIPELINE_MD = root / "reports" / "local_research_pipeline_latest.md"
                payload = self.tool.save_state(
                    "download",
                    "paused_time_budget",
                    root / "runtime" / "pipeline.log",
                    {"download_progress": {"percent": 12.5, "pending_tasks": 10, "failed_requests": 0}},
                )
                self.assertEqual(payload["phase"], "download")
                self.assertTrue(self.tool.PIPELINE_JSON.exists())
                self.assertTrue(self.tool.PIPELINE_MD.exists())
                self.assertIn("Local Research Pipeline", self.tool.PIPELINE_MD.read_text(encoding="utf-8"))
            finally:
                self.tool.PIPELINE_JSON = old_json
                self.tool.PIPELINE_MD = old_md


if __name__ == "__main__":
    unittest.main()
