import importlib.util
import json
import sqlite3
import sys
import tempfile
from contextlib import closing
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    path = ROOT / "部署工具" / "decision_attention.py"
    spec = importlib.util.spec_from_file_location("decision_attention_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class DecisionAttentionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def test_report_decision_statuses_count_as_acknowledged(self):
        item = {
            "item_id": "rollback:exp-20260527-v16-atr-stop-bands",
            "priority": "P1",
            "category": "策略回滚",
            "title": "P1 B/v16 回滚观察",
            "evidence": "pnl_after_cost=-87.04; profit_factor=0.79<1.05",
            "source": "test",
        }
        ack = {
            item["item_id"]: {
                "item_id": item["item_id"],
                "status": "narrow_b_v16_requested",
                "fingerprint": self.tool.item_fingerprint(item),
            }
        }

        self.assertTrue(self.tool.is_acknowledged(item, ack))

    def test_paper_positions_suppress_open_staleness_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            runtime.mkdir()
            db = runtime / "event_store.sqlite3"
            now = datetime.now(timezone(timedelta(hours=8)))
            with closing(sqlite3.connect(db)) as conn:
                conn.execute(
                    "create table events(id integer primary key, ts text, strategy text, source text, event_type text, category text, symbol text, side text)"
                )
                conn.execute(
                    "insert into events(ts,strategy,source,event_type,category,symbol,side) values(?,?,?,?,?,?,?)",
                    (now.isoformat(), "B/v16", "B/system", "HEARTBEAT", "", "", ""),
                )
                conn.commit()
            paper = runtime / "paper_exchange_latest.json"
            paper.write_text(
                json.dumps({
                    "mode": "paper_exchange",
                    "by_strategy": {"B/v16": {"positions": 3}},
                    "recent_fills": [],
                }),
                encoding="utf-8",
            )

            old_db = self.tool.EVENT_STORE_DB
            old_paper = self.tool.PAPER_EXCHANGE_JSON
            self.tool.EVENT_STORE_DB = db
            self.tool.PAPER_EXCHANGE_JSON = paper
            try:
                items = self.tool.detect_open_staleness_items()
            finally:
                self.tool.EVENT_STORE_DB = old_db
                self.tool.PAPER_EXCHANGE_JSON = old_paper

            self.assertFalse([item for item in items if item["item_id"] == "strategy-open-stale:b-v16"])


if __name__ == "__main__":
    unittest.main()
