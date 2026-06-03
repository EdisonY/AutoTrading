import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    path = ROOT / "部署工具" / "system_alerts.py"
    spec = importlib.util.spec_from_file_location("system_alerts_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SystemAlertsConstructionModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.old_marker = self.tool.CONSTRUCTION_MODE_MARKER
        self.old_skeleton = self.tool.LONG_TERM_SKELETON_LATEST
        self.old_market = self.tool.MARKET_CACHE
        self.old_account = self.tool.ACCOUNT_LATEST
        self.old_attention = self.tool.ATTENTION_LATEST
        self.old_db = self.tool.EVENT_STORE_DB
        self.tool.CONSTRUCTION_MODE_MARKER = self.runtime / "construction_mode.json"
        self.tool.LONG_TERM_SKELETON_LATEST = self.runtime / "long_term_skeleton_latest.json"
        self.tool.MARKET_CACHE = self.runtime / "market_data_cache.json"
        self.tool.ACCOUNT_LATEST = self.runtime / "account_snapshot_latest.json"
        self.tool.ATTENTION_LATEST = self.root / "research_memory" / "attention" / "open_items.json"
        self.tool.EVENT_STORE_DB = self.runtime / "event_store.sqlite3"
        con = sqlite3.connect(str(self.tool.EVENT_STORE_DB))
        try:
            con.execute("create table events (id integer primary key autoincrement, ts text, source text)")
            con.execute("create table account_snapshots (id integer primary key autoincrement, ts text)")
            con.commit()
        finally:
            con.close()

    def tearDown(self):
        self.tool.CONSTRUCTION_MODE_MARKER = self.old_marker
        self.tool.LONG_TERM_SKELETON_LATEST = self.old_skeleton
        self.tool.MARKET_CACHE = self.old_market
        self.tool.ACCOUNT_LATEST = self.old_account
        self.tool.ATTENTION_LATEST = self.old_attention
        self.tool.EVENT_STORE_DB = self.old_db
        self.tmp.cleanup()

    def test_inactive_services_are_not_bad_during_construction_mode(self):
        self.tool.CONSTRUCTION_MODE_MARKER.write_text(
            json.dumps({"enabled": True, "reason": "test"}),
            encoding="utf-8",
        )

        service_states = {name: "inactive" for name in self.tool.SERVICES}
        timer_states = {name: "inactive" for name in self.tool.TIMERS}
        with mock.patch.object(self.tool, "service_states", return_value=service_states), \
             mock.patch.object(self.tool, "unit_states", return_value=timer_states), \
             mock.patch.object(self.tool, "systemctl_value", return_value="success"), \
             mock.patch.object(self.tool, "read_meminfo", return_value={}), \
             mock.patch.object(self.tool, "recent_oom_lines", return_value=[]), \
             mock.patch.object(self.tool, "recent_api_rate_limits", return_value={"total": 0, "by_service": {}, "latest": "", "latest_ts": None, "ban_until": None}), \
             mock.patch.object(self.tool, "read_binance_api_guard", return_value={}), \
             mock.patch.object(self.tool, "recent_failed_close_alerts", return_value=[]):
            payload = self.tool.collect_alerts()

        self.assertEqual(payload["status"], "warn")
        self.assertTrue(payload["construction_mode"]["enabled"])
        bad_titles = [item["title"] for item in payload["alerts"] if item["level"] == "bad"]
        self.assertFalse([title for title in bad_titles if "服务异常" in title or "定时任务未运行" in title])


if __name__ == "__main__":
    unittest.main()
