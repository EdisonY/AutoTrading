import importlib.util
import gc
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    path = ROOT / "部署工具" / "paper_exchange_runner.py"
    spec = importlib.util.spec_from_file_location("paper_exchange_runner_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class PaperExchangeRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def test_bootstrap_events_are_marked_as_maintenance_not_rollout_evidence(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            old_market_rows = self.tool.market_rows
            old_supported = self.tool.okx_symbol_supported
            old_resolve_price = self.tool.resolve_price
            try:
                self.tool.market_rows = lambda _root: [{"symbol": "AAAUSDT", "change_pct": 1.0}]
                self.tool.okx_symbol_supported = lambda _symbol: True
                self.tool.resolve_price = lambda _root, _symbol: (100.0, "test_price")
                exchange = self.tool.PaperExchange(root)

                created = self.tool.open_bootstrap_positions(
                    root,
                    exchange,
                    target_per_strategy=1,
                    margin_usdt=100,
                    leverage=4,
                )
            finally:
                self.tool.market_rows = old_market_rows
                self.tool.okx_symbol_supported = old_supported
                self.tool.resolve_price = old_resolve_price

            con = sqlite3.connect(root / "runtime" / "event_store.sqlite3")
            con.row_factory = sqlite3.Row
            rows = [dict(row) for row in con.execute("select source, payload_json from events order by id")]
            con.close()
            del con
            gc.collect()

        self.assertEqual(created, 3)
        self.assertEqual(len(rows), 3)
        for row in rows:
            payload = json.loads(row["payload_json"])
            self.assertTrue(row["source"].endswith("/paper_exchange"))
            self.assertTrue(payload["paper_exchange_bootstrap"])
            self.assertEqual(payload["evidence_role"], "paper_exchange_maintenance_bootstrap")
            self.assertFalse(payload["rollout_evidence_eligible"])


if __name__ == "__main__":
    unittest.main()
