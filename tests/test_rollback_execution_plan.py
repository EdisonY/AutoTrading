import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    path = ROOT / "部署工具" / "rollback_execution_plan.py"
    spec = importlib.util.spec_from_file_location("rollback_execution_plan_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def watch_item(
    *,
    action: str = "prepare_manual_rollback",
    readiness_status: str = "operator_ready",
    priority: str = "P0",
    strategy: str = "B/v16",
) -> dict[str, object]:
    return {
        "candidate_id": "EXP-test-v16",
        "strategy": strategy,
        "priority": priority,
        "status": "rollback_required" if priority == "P0" else "rollback_watch",
        "action": action,
        "operator_readiness": {
            "status": readiness_status,
            "ready": readiness_status == "operator_ready",
            "action": action,
            "maturity": "mature",
            "gaps": [] if readiness_status == "operator_ready" else ["replay_readiness:data_gap"],
        },
        "quality_24h": {"realized_pnl_after_cost": -91.5},
        "quality_72h": {"realized_pnl_after_cost": -151.25},
        "window_24h": {"close_failed": 1},
        "decision_packet": {
            "rollback_path": ["restore previous config", "restart strategy after approval"],
        },
    }


class RollbackExecutionPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def test_operator_ready_manual_rollback_builds_disabled_apply_plan(self):
        payload = self.tool.build_payload_from_items([watch_item()])

        self.assertEqual(payload["status"], "ready_for_dry_run_review")
        self.assertFalse(payload["apply_enabled"])
        self.assertEqual(payload["summary"]["actionable_plans"], 1)
        plan = payload["plans"][0]
        self.assertEqual(plan["execution_status"], "manual_rollback_ready")
        self.assertFalse(plan["apply_enabled"])
        self.assertIn("release_manager.py rollback", plan["dry_run_commands"][1])
        self.assertNotIn("--apply", plan["dry_run_commands"][1])
        self.assertIn("--apply", plan["apply_commands_disabled"][0])

    def test_not_operator_ready_stays_blocked(self):
        payload = self.tool.build_payload_from_items(
            [watch_item(action="prepare_rollback_review", readiness_status="waiting_for_replay_readiness", priority="P1")]
        )

        self.assertEqual(payload["status"], "waiting_for_operator_ready_items")
        self.assertEqual(payload["summary"]["not_actionable"], 1)
        plan = payload["plans"][0]
        self.assertEqual(plan["execution_status"], "not_actionable")
        self.assertEqual(plan["checklist"][0]["status"], "blocked")

    def test_empty_review_has_no_items_status(self):
        payload = self.tool.build_payload_from_items([])

        self.assertEqual(payload["status"], "no_rollback_watch_items")
        self.assertEqual(payload["summary"]["plans"], 0)
        self.assertEqual(payload["summary"]["actionable_plans"], 0)

    def test_main_writes_runtime_and_report_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            reports = root / "reports"
            runtime.mkdir()
            (runtime / "rollback_watch_review_latest.json").write_text(
                json.dumps({"items": [watch_item()]}),
                encoding="utf-8",
            )

            rc = self.tool.main(["--runtime-dir", str(runtime), "--reports-dir", str(reports)])

            self.assertEqual(rc, 0)
            out = json.loads((runtime / "rollback_execution_plan_latest.json").read_text(encoding="utf-8"))
            self.assertEqual(out["summary"]["actionable_plans"], 1)
            md = (reports / "rollback_execution_plan_latest.md").read_text(encoding="utf-8")
            self.assertIn("Rollback Execution Plan", md)
            self.assertIn("Apply enabled: `False`", md)


if __name__ == "__main__":
    unittest.main()
