import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    path = ROOT / "部署工具" / "attention_api_server.py"
    spec = importlib.util.spec_from_file_location("attention_api_server_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class AttentionApiServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "runtime" / "event_store.sqlite3"
        self.attention_json = self.root / "research_memory" / "attention" / "open_items.json"
        self.db.parent.mkdir(parents=True)
        self.attention_json.parent.mkdir(parents=True)
        self.old_db = self.tool.EVENT_STORE_DB
        self.old_json = self.tool.ATTENTION_JSON
        self.tool.EVENT_STORE_DB = self.db
        self.tool.ATTENTION_JSON = self.attention_json

    def tearDown(self):
        self.tool.EVENT_STORE_DB = self.old_db
        self.tool.ATTENTION_JSON = self.old_json
        self.tmp.cleanup()

    def seed_current_schema(self):
        con = sqlite3.connect(str(self.db))
        try:
            self.tool.ensure_attention_schema(con)
            con.execute(
                """
                insert into attention_items (
                    item_id, priority, category, title, status, evidence,
                    recommended_action, first_seen, last_seen, last_confirmed_active, source
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "item-1",
                    "P1",
                    "system",
                    "Needs review",
                    "open",
                    "evidence",
                    "ack",
                    "2026-06-04T00:00:00+08:00",
                    "2026-06-04T00:01:00+08:00",
                    "2026-06-04T00:01:00+08:00",
                    "test",
                ),
            )
            con.commit()
        finally:
            con.close()

    def test_acknowledge_writes_current_durable_schema(self):
        self.seed_current_schema()

        result = self.tool.acknowledge_item("item-1", "tester")

        self.assertTrue(result["ok"])
        con = sqlite3.connect(str(self.db))
        con.row_factory = sqlite3.Row
        try:
            item = con.execute("select * from attention_items where item_id = ?", ("item-1",)).fetchone()
            ack = con.execute("select * from attention_acknowledgements where item_id = ?", ("item-1",)).fetchone()
        finally:
            con.close()

        self.assertEqual(item["status"], "acknowledged")
        self.assertTrue(item["acknowledged_at"])
        self.assertEqual(item["acknowledged_reason"], "tester:acknowledged")
        self.assertEqual(ack["status"], "acknowledged")
        self.assertEqual(ack["reason"], "tester:acknowledged")
        self.assertTrue(ack["fingerprint"])
        self.assertIn("Needs review", ack["payload_json"])

    def test_legacy_ack_table_gets_new_columns_and_resolve_works(self):
        con = sqlite3.connect(str(self.db))
        try:
            con.execute(
                """
                create table attention_items (
                    item_id text primary key,
                    priority text,
                    category text,
                    title text,
                    status text,
                    evidence text,
                    recommended_action text,
                    first_seen text,
                    last_seen text,
                    last_confirmed_active text,
                    source text
                )
                """
            )
            con.execute(
                """
                create table attention_acknowledgements (
                    item_id text,
                    ack_time text,
                    ack_user text,
                    ack_type text
                )
                """
            )
            con.execute(
                """
                insert into attention_items (
                    item_id, priority, category, title, status, evidence,
                    recommended_action, first_seen, last_seen, last_confirmed_active, source
                ) values ('item-2', 'P2', 'ops', 'Legacy item', 'open', 'e', 'r', 't0', 't1', 't1', 'src')
                """
            )
            con.commit()
        finally:
            con.close()

        result = self.tool.resolve_item("item-2", "tester")

        self.assertTrue(result["ok"])
        con = sqlite3.connect(str(self.db))
        con.row_factory = sqlite3.Row
        try:
            columns = {row[1] for row in con.execute("pragma table_info(attention_acknowledgements)").fetchall()}
            ack = con.execute("select * from attention_acknowledgements where item_id = ?", ("item-2",)).fetchone()
            item = con.execute("select * from attention_items where item_id = ?", ("item-2",)).fetchone()
        finally:
            con.close()

        self.assertIn("status", columns)
        self.assertIn("reason", columns)
        self.assertIn("acknowledged_at", columns)
        self.assertEqual(ack["status"], "resolved")
        self.assertEqual(ack["reason"], "tester:resolved")
        self.assertEqual(item["status"], "resolved")

    def test_json_fallback_marks_acknowledged_fields(self):
        payload = {
            "generated_at": "old",
            "items": [{"item_id": "json-1", "status": "open", "priority": "P2"}],
        }
        self.attention_json.write_text(json.dumps(payload), encoding="utf-8")

        result = self.tool.acknowledge_item("json-1")

        self.assertTrue(result["ok"])
        out = json.loads(self.attention_json.read_text(encoding="utf-8"))
        item = out["items"][0]
        self.assertEqual(item["status"], "acknowledged")
        self.assertTrue(item["acknowledged_at"])
        self.assertEqual(item["acknowledged_reason"], "portal:acknowledged")


if __name__ == "__main__":
    unittest.main()
