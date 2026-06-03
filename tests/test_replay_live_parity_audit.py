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

    def make_db(self, rows, scan_rows=None):
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
        con.execute(
            """
            create table sentinel_scans (
                id integer primary key,
                ts text,
                date text,
                strategy text,
                symbol text,
                event_type text,
                reason text,
                category text,
                decision_stage text,
                filter_layer text,
                change_pct real,
                velocity_pct real,
                abs_velocity_pct real,
                quote_volume real,
                scan_result text,
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
        for row in scan_rows or []:
            payload = json.dumps(row.get("payload") or {}, ensure_ascii=False)
            ts = row.get("ts") or self.tool.now_cst().isoformat()
            con.execute(
                """
                insert into sentinel_scans (
                    ts, date, strategy, symbol, event_type, reason, category,
                    decision_stage, filter_layer, scan_result, payload_json
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    ts[:10],
                    row.get("strategy") or "A/v11",
                    row.get("symbol") or "BTCUSDT",
                    row.get("event_type") or "SENTINEL_SCANNED",
                    row.get("reason") or "",
                    row.get("category") or "sentinel_score_rejected",
                    row.get("decision_stage") or "score_gate",
                    row.get("filter_layer") or "strategy",
                    row.get("scan_result") or "score_rejected",
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
        self.assertEqual(summary["acceptance_status"], "accepted")
        self.assertEqual(summary["acceptance_conclusion"], "historical_same_input_parity_accepted_for_available_rows")
        self.assertTrue(payload["acceptance"]["accepted"])
        self.assertFalse(payload["acceptance"]["fresh_run_required"])
        self.assertEqual(payload["acceptance"]["flows"]["open_flow"]["status"], "accepted")

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
        self.assertEqual(summary["acceptance_status"], "blocked")
        self.assertEqual(payload["acceptance"]["flows"]["open_flow"]["status"], "blocked_by_mismatch")
        self.assertTrue(payload["acceptance"]["fresh_run_required"])
        self.assertEqual(payload["mismatch_examples"][0]["gate"], "positive_quantity")

    def test_strategy_gate_cases_list_counts_multiple_cases_on_one_row(self):
        tmp, db_path = self.make_db(
            [
                {
                    "payload": {
                        "strategy_gate_cases": [
                            {
                                "name": "qty-positive",
                                "gate": "positive_quantity",
                                "inputs": {"quantity": 1.25},
                                "expected_allowed": True,
                                "expected_reason": "quantity_positive",
                            },
                            {
                                "name": "score-too-hot",
                                "gate": "score_max",
                                "inputs": {"score": 91, "score_max": 85},
                                "expected_allowed": False,
                                "expected_reason": "评分91超过85",
                            },
                        ]
                    }
                }
            ]
        )
        self.addCleanup(tmp.cleanup)

        payload = self.tool.build_payload(db_path, days=1, limit=100)
        summary = payload["summary"]

        self.assertEqual(summary["open_flow_rows"], 1)
        self.assertEqual(summary["rows_with_exact_cases"], 1)
        self.assertEqual(summary["missing_case_rows"], 0)
        self.assertEqual(summary["gate_cases"], 2)
        self.assertEqual(summary["passed"], 2)
        self.assertEqual(summary["mismatched"], 0)
        self.assertEqual(summary["status"], "ok")
        top_gates = {item["name"]: item["count"] for item in payload["strategies"][0]["top_gates"]}
        self.assertEqual(top_gates["positive_quantity"], 1)
        self.assertEqual(top_gates["score_max"], 1)

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
        self.assertEqual(summary["acceptance_status"], "missing_exact_cases")
        self.assertEqual(payload["acceptance"]["flows"]["open_flow"]["status"], "missing_exact_cases")
        self.assertTrue(payload["acceptance"]["fresh_run_required"])

    def test_acceptance_marks_partial_coverage_gap(self):
        tmp, db_path = self.make_db(
            [
                {
                    "payload": {
                        "strategy_gate_case": {
                            "name": "qty-positive",
                            "gate": "positive_quantity",
                            "inputs": {"quantity": 1},
                            "expected_allowed": True,
                            "expected_reason": "quantity_positive",
                        }
                    }
                },
                {"payload": {"raw": {"reason": "legacy row without exact case"}}},
            ]
        )
        self.addCleanup(tmp.cleanup)

        payload = self.tool.build_payload(db_path, days=1, limit=100)
        summary = payload["summary"]

        self.assertEqual(summary["open_flow_rows"], 2)
        self.assertEqual(summary["rows_with_exact_cases"], 1)
        self.assertEqual(summary["missing_case_rows"], 1)
        self.assertEqual(summary["exact_case_coverage_pct"], 50.0)
        self.assertEqual(summary["status"], "partial")
        self.assertEqual(summary["acceptance_status"], "partial")
        self.assertEqual(summary["acceptance_conclusion"], "historical_same_input_parity_partial_coverage")
        self.assertEqual(payload["acceptance"]["flows"]["open_flow"]["status"], "coverage_gap")
        self.assertEqual(payload["acceptance"]["blocking_flows"][0]["label"], "open_flow")
        self.assertTrue(payload["acceptance"]["fresh_run_required"])

    def test_acceptance_marks_no_historical_rows_not_measurable(self):
        tmp, db_path = self.make_db([])
        self.addCleanup(tmp.cleanup)

        payload = self.tool.build_payload(db_path, days=1, limit=100)
        summary = payload["summary"]

        self.assertEqual(summary["open_flow_rows"], 0)
        self.assertEqual(summary["acceptance_status"], "no_historical_rows")
        self.assertEqual(summary["acceptance_conclusion"], "historical_same_input_parity_not_measurable")
        self.assertEqual(payload["acceptance"]["next_action"], "run_final_staged_fresh_run_after_offline_work")

    def test_sentinel_scan_exact_cases_are_reported_separately(self):
        tmp, db_path = self.make_db(
            [],
            scan_rows=[
                {
                    "payload": {
                        "strategy_gate_case": {
                            "name": "score-too-hot",
                            "gate": "score_max",
                            "inputs": {"score": 91, "score_max": 85},
                            "expected_allowed": False,
                            "expected_reason": "评分91超过85",
                        }
                    }
                }
            ],
        )
        self.addCleanup(tmp.cleanup)

        payload = self.tool.build_payload(db_path, days=1, limit=100)
        summary = payload["summary"]

        self.assertEqual(summary["open_flow_rows"], 0)
        self.assertEqual(summary["gate_cases"], 0)
        self.assertEqual(summary["scan_gate_rows"], 1)
        self.assertEqual(summary["scan_rows_with_exact_cases"], 1)
        self.assertEqual(summary["scan_gate_cases"], 1)
        self.assertEqual(summary["scan_passed"], 1)
        self.assertEqual(summary["scan_mismatched"], 0)
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(payload["strategies"][0]["scan_top_gates"][0]["name"], "score_max")

    def test_close_flow_exact_cases_are_reported_separately(self):
        tmp, db_path = self.make_db(
            [
                {
                    "event_type": "CLOSE_FAILED",
                    "stage": "execution",
                    "layer": "execution",
                    "payload": {
                        "strategy_gate_case": {
                            "name": "close-failed",
                            "gate": "execution_result",
                            "inputs": {
                                "success": False,
                                "preflight_rejected": False,
                                "code": "close_confirmation_timeout",
                                "reason": "position still open",
                            },
                            "expected_allowed": False,
                            "expected_reason": "position still open",
                        }
                    },
                }
            ]
        )
        self.addCleanup(tmp.cleanup)

        payload = self.tool.build_payload(db_path, days=1, limit=100)
        summary = payload["summary"]

        self.assertEqual(summary["open_flow_rows"], 0)
        self.assertEqual(summary["close_flow_rows"], 1)
        self.assertEqual(summary["close_rows_with_exact_cases"], 1)
        self.assertEqual(summary["close_missing_case_rows"], 0)
        self.assertEqual(summary["close_gate_cases"], 1)
        self.assertEqual(summary["close_passed"], 1)
        self.assertEqual(summary["close_mismatched"], 0)
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(payload["strategies"][0]["close_top_gates"][0]["name"], "execution_result")

    def test_duplicate_nested_raw_cases_are_counted_once(self):
        case = {
            "name": "close-failed",
            "gate": "execution_result",
            "inputs": {
                "success": False,
                "preflight_rejected": False,
                "code": "close_confirmation_timeout",
                "reason": "position still open",
            },
            "expected_allowed": False,
            "expected_reason": "position still open",
        }
        tmp, db_path = self.make_db(
            [
                {
                    "event_type": "CLOSE_FAILED",
                    "payload": {
                        "raw": {"strategy_gate_case": case},
                        "raw_event": {"strategy_gate_case": case},
                    },
                }
            ]
        )
        self.addCleanup(tmp.cleanup)

        payload = self.tool.build_payload(db_path, days=1, limit=100)
        summary = payload["summary"]

        self.assertEqual(summary["close_rows_with_exact_cases"], 1)
        self.assertEqual(summary["close_gate_cases"], 1)
        self.assertEqual(summary["close_passed"], 1)


if __name__ == "__main__":
    unittest.main()
