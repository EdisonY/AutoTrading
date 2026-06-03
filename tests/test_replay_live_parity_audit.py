import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    path = ROOT / "部署工具/replay_live_parity_audit.py"
    spec = importlib.util.spec_from_file_location("replay_live_parity_audit_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ReplayLiveParityAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def make_db(self, rows):
        tmp = tempfile.TemporaryDirectory()
        db_path = Path(tmp.name) / "events.sqlite3"
        con = sqlite3.connect(db_path)
        con.execute(
            """
            create table events (
                id integer primary key,
                ts text,
                strategy text,
                symbol text,
                event_type text,
                category text,
                side text,
                score real,
                stage text,
                layer text,
                reason text,
                source text,
                payload_json text
            )
            """
        )
        for row in rows:
            payload = json.dumps(row.get("payload") or {}, ensure_ascii=False)
            con.execute(
                """
                insert into events (
                    ts, strategy, symbol, event_type, category, side, score,
                    stage, layer, reason, source, payload_json
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.get("ts") or self.tool.now_cst().isoformat(),
                    row.get("strategy") or "A/v11",
                    row.get("symbol") or "BTCUSDT",
                    row.get("event_type") or "OPEN_SKIPPED",
                    row.get("category") or "decision",
                    row.get("side") or "long",
                    row.get("score"),
                    row.get("stage") or "",
                    row.get("layer") or "",
                    row.get("reason") or "",
                    row.get("source") or "test/events",
                    payload,
                ),
            )
        con.commit()
        con.close()
        return tmp, db_path

    def test_exact_case_pass_counts_as_ok(self):
        tmp, db_path = self.make_db(
            [
                {
                    "payload": {
                        "strategy_gate_case": {
                            "name": "qty-zero",
                            "gate": "positive_quantity",
                            "inputs": {"quantity": 0},
                            "expected_allowed": False,
                            "expected_reason": "qty<=0",
                        }
                    }
                }
            ]
        )
        self.addCleanup(tmp.cleanup)

        payload = self.tool.build_payload(db_path, days=1, limit=100)
        summary = payload["summary"]

        self.assertEqual(summary["open_flow_rows"], 1)
        self.assertEqual(summary["rows_with_exact_cases"], 1)
        self.assertEqual(summary["gate_cases"], 1)
        self.assertEqual(summary["passed"], 1)
        self.assertEqual(summary["mismatched"], 0)
        self.assertEqual(summary["errors"], 0)
        self.assertEqual(summary["status"], "ok")

    def test_mismatch_is_reported_with_example(self):
        tmp, db_path = self.make_db(
            [
                {
                    "payload": {
                        "strategy_gate_case": {
                            "name": "qty-zero-mismatch",
                            "gate": "positive_quantity",
                            "inputs": {"quantity": 0},
                            "expected_allowed": True,
                        }
                    }
                }
            ]
        )
        self.addCleanup(tmp.cleanup)

        payload = self.tool.build_payload(db_path, days=1, limit=100)
        summary = payload["summary"]

        self.assertEqual(summary["passed"], 0)
        self.assertEqual(summary["mismatched"], 1)
        self.assertEqual(summary["status"], "bad")
        self.assertEqual(payload["mismatch_examples"][0]["gate"], "positive_quantity")

    def test_missing_case_rows_are_visible_gaps(self):
        tmp, db_path = self.make_db([{"payload": {"raw": {"reason": "no exact case"}}}])
        self.addCleanup(tmp.cleanup)

        payload = self.tool.build_payload(db_path, days=1, limit=100)
        summary = payload["summary"]

        self.assertEqual(summary["open_flow_rows"], 1)
        self.assertEqual(summary["rows_with_exact_cases"], 0)
        self.assertEqual(summary["missing_case_rows"], 1)
        self.assertEqual(summary["gate_cases"], 0)
        self.assertEqual(summary["status"], "missing_exact_cases")


if __name__ == "__main__":
    unittest.main()
