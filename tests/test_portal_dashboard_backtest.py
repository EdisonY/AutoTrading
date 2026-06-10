import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    path = ROOT / "部署工具" / "portal_dashboard.py"
    spec = importlib.util.spec_from_file_location("portal_dashboard_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class PortalDashboardBacktestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def test_function_status_replaces_completed_history_with_backtest_module(self):
        cards = self.tool.function_status_cards(
            {
                "attention": {"available": True, "summary": {"open": 0, "counts": {"P0": 0, "P1": 0, "P2": 0}}},
                "strategies": [{"name": "A/v11", "ok": True}, {"name": "B/v16", "ok": True}, {"name": "C/v14", "ok": True}],
                "alerts": {"available": True, "status": "ok", "alert_count": 0, "api_rate_limits": {}, "api_guard": {}, "disk": {}},
                "historical_kline_backfill": {
                    "available": True,
                    "fresh": True,
                    "status": "complete",
                    "progress": {"pending_tasks": 0, "percent": 100.0, "written_rows": 1602051},
                    "quality": {
                        "covered_symbol_count": 26,
                        "target_symbol_count": 30,
                        "covered_symbol_interval_count": 104,
                        "target_symbol_interval_count": 120,
                    },
                },
                "historical_kline_incremental": {"available": True, "fresh": True, "status": "planned", "progress": {}, "config": {}},
                "backtest_module": {
                    "available": True,
                    "fresh": True,
                    "status": "phase1_job_api_ready",
                    "capabilities": {
                        "job_submit_api": True,
                        "anti_overfit_gate": True,
                        "strategy_replay_adapter": False,
                    },
                    "historical_baseline": {
                        "covered_symbol_count": 26,
                        "target_symbol_count": 30,
                        "covered_symbol_interval_count": 104,
                        "target_symbol_interval_count": 120,
                    },
                },
            }
        )

        names = [card["name"] for card in cards]
        self.assertIn("历史回测模块", names)
        self.assertNotIn("Top30一年历史K线", names)
        backtest_card = next(card for card in cards if card["name"] == "历史回测模块")
        self.assertIn("不生成假收益", backtest_card["body"])

    def test_backtest_summary_prefers_mirror_report_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local = root / "runtime" / "backtest_module_latest.json"
            mirror = root / "server_logs_tencent" / "runtime" / "backtest_module_latest.json"
            mirror_md = root / "server_logs_tencent" / "reports" / "backtest_module_latest.md"
            for path in (local, mirror, mirror_md):
                path.parent.mkdir(parents=True, exist_ok=True)
            local.write_text(json.dumps({"generated_at": "2026-06-10T12:00:00+08:00", "status": "local_old"}), encoding="utf-8")
            mirror.write_text(json.dumps({"generated_at": "2026-06-10T13:00:00+08:00", "status": "mirror_new"}), encoding="utf-8")
            mirror_md.write_text("# mirror", encoding="utf-8")

            old_json = self.tool.MIRROR_BACKTEST_MODULE_JSON
            old_md = self.tool.MIRROR_BACKTEST_MODULE_MD
            try:
                self.tool.MIRROR_BACKTEST_MODULE_JSON = mirror
                self.tool.MIRROR_BACKTEST_MODULE_MD = mirror_md
                summary = self.tool.backtest_module_summary(local, mirror)
            finally:
                self.tool.MIRROR_BACKTEST_MODULE_JSON = old_json
                self.tool.MIRROR_BACKTEST_MODULE_MD = old_md

            self.assertEqual(summary["status"], "mirror_new")
            self.assertEqual(summary["path"], mirror_md)


if __name__ == "__main__":
    unittest.main()
