import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

from core.binance_api_queue import BinanceApiQueue


ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    path = ROOT / "部署工具" / "binance_start_guard.py"
    spec = importlib.util.spec_from_file_location("binance_start_guard_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class BinanceStartGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def make_db(self) -> tuple[tempfile.TemporaryDirectory, Path]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return tmp, Path(tmp.name) / "queue.sqlite3"

    def test_missing_db_allows_start(self):
        _tmp, db = self.make_db()

        payload = self.tool.guard_status(db, scope="public", account="", at_ms=1_000)

        self.assertTrue(payload["allowed"])
        self.assertEqual(payload["active_cooldowns"], [])

    def test_global_cooldown_blocks_any_scope(self):
        _tmp, db = self.make_db()
        queue = BinanceApiQueue(db)
        queue.set_cooldown(scope="global", until_ms=60_000, reason="HTTP 418 global")

        payload = self.tool.guard_status(db, scope="public", account="", at_ms=1_000)

        self.assertFalse(payload["allowed"])
        self.assertEqual(payload["active_cooldowns"][0]["scope"], "global")

    def test_account_alias_cooldown_blocks_matching_strategy(self):
        _tmp, db = self.make_db()
        queue = BinanceApiQueue(db)
        queue.set_cooldown(scope="signed", account="B", until_ms=60_000, reason="HTTP 418")

        blocked = self.tool.guard_status(db, scope="signed", account="B/v16", at_ms=1_000)
        other = self.tool.guard_status(db, scope="signed", account="C/v14", at_ms=1_000)

        self.assertFalse(blocked["allowed"])
        self.assertTrue(other["allowed"])

    def test_expired_cooldown_allows_start(self):
        _tmp, db = self.make_db()
        queue = BinanceApiQueue(db)
        queue.set_cooldown(scope="public", until_ms=1_000, reason="old")

        payload = self.tool.guard_status(db, scope="public", account="", at_ms=60_000)

        self.assertTrue(payload["allowed"])

    def test_broken_db_fails_closed(self):
        _tmp, db = self.make_db()
        db.write_text("not sqlite", encoding="utf-8")

        payload = self.tool.guard_status(db, scope="any", account="", at_ms=1_000)

        self.assertFalse(payload["allowed"])
        self.assertTrue(payload["fail_closed"])


if __name__ == "__main__":
    unittest.main()
