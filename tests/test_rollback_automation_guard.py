import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    path = ROOT / "部署工具" / "rollback_automation_guard.py"
    spec = importlib.util.spec_from_file_location("rollback_automation_guard_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def replay(status: str = "ready_for_operator_review") -> dict[str, object]:
    return {"status": status, "summary": {"blockers": 0 if status == "ready_for_operator_review" else 1}}


def policy(enabled: bool = True) -> dict[str, object]:
    return {
        "approved": enabled,
        "automatic_rollback_enabled": enabled,
        "approved_by": "operator",
        "approved_at": "2026-06-04T05:30:00+08:00",
        "scope": "testnet_rollback_watch_items",
        "procedure_version": "v1",
    }


def execution_plan(*, release_id_ready: bool = True, actionable: int = 1) -> dict[str, object]:
    plans = []
    if actionable:
        release_id = "20260601-000000-strategy-b-abc1234" if release_id_ready else ""
        dry_run = (
            "python 部署工具\\release_manager.py rollback --target tencent "
            f"--release-id {release_id or '<release-id-for-exp-test>'}"
        )
        plans.append(
            {
                "plan_id": "rollback-b-v16-exp-test",
                "candidate_id": "EXP-test",
                "strategy": "B/v16",
                "priority": "P1",
                "execution_status": "rollback_review_ready",
                "component": "strategy-b",
                "reviewed_release_id": release_id,
                "dry_run_commands": [dry_run],
                "apply_commands_disabled": [],
            }
        )
    return {
        "status": "ready_for_dry_run_review" if actionable else "waiting_for_operator_ready_items",
        "summary": {"actionable_plans": actionable, "plans": actionable},
        "plans": plans,
    }


class RollbackAutomationGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def test_missing_policy_blocks_and_keeps_automation_disabled(self):
        payload = self.tool.build_payload(
            rollback_watch={"summary": {"operator_ready": 1}},
            rollback_execution=execution_plan(),
            replay_readiness=replay(),
            policy=None,
            policy_path=Path("research_memory/approvals/rollback_automation_policy.json"),
        )

        self.assertEqual(payload["status"], "blocked")
        self.assertFalse(payload["automatic_rollback_allowed"])
        self.assertFalse(payload["apply_enabled"])
        self.assertIn("explicit_rollback_automation_policy_missing", payload["blockers"])

    def test_replay_gap_blocks_even_with_policy_and_plan(self):
        payload = self.tool.build_payload(
            rollback_watch={"summary": {"operator_ready": 1}},
            rollback_execution=execution_plan(),
            replay_readiness=replay("data_gap"),
            policy=policy(),
            policy_path=Path("policy.json"),
        )

        self.assertEqual(payload["status"], "blocked")
        self.assertIn("replay_readiness:data_gap", payload["blockers"])
        self.assertFalse(payload["automatic_rollback_allowed"])

    def test_no_actionable_execution_plan_blocks(self):
        payload = self.tool.build_payload(
            rollback_watch={"summary": {"operator_ready": 0}},
            rollback_execution=execution_plan(actionable=0),
            replay_readiness=replay(),
            policy=policy(),
            policy_path=Path("policy.json"),
        )

        self.assertEqual(payload["status"], "blocked")
        self.assertIn("no_actionable_dry_run_plan", payload["blockers"])

    def test_placeholder_release_id_blocks(self):
        payload = self.tool.build_payload(
            rollback_watch={"summary": {"operator_ready": 1}},
            rollback_execution=execution_plan(release_id_ready=False),
            replay_readiness=replay(),
            policy=policy(),
            policy_path=Path("policy.json"),
        )

        self.assertEqual(payload["status"], "blocked")
        self.assertIn("release_id_unresolved:EXP-test", payload["blockers"])

    def test_all_preconditions_still_report_only_and_apply_disabled(self):
        payload = self.tool.build_payload(
            rollback_watch={"summary": {"operator_ready": 1}},
            rollback_execution=execution_plan(release_id_ready=True),
            replay_readiness=replay(),
            policy=policy(),
            policy_path=Path("policy.json"),
        )

        self.assertEqual(payload["status"], "preconditions_met_report_only")
        self.assertTrue(payload["preconditions_met"])
        self.assertFalse(payload["automatic_rollback_allowed"])
        self.assertFalse(payload["apply_enabled"])
        self.assertEqual(payload["candidates"][0]["automation_status"], "preconditions_met_report_only")

    def test_main_writes_runtime_and_report_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            reports = root / "reports"
            approvals = root / "research_memory" / "approvals"
            runtime.mkdir(parents=True)
            approvals.mkdir(parents=True)
            (runtime / "rollback_watch_review_latest.json").write_text(
                json.dumps({"summary": {"operator_ready": 1}}),
                encoding="utf-8",
            )
            (runtime / "rollback_execution_plan_latest.json").write_text(
                json.dumps(execution_plan(release_id_ready=False)),
                encoding="utf-8",
            )
            (runtime / "replay_readiness_latest.json").write_text(json.dumps(replay()), encoding="utf-8")
            policy_path = approvals / "rollback_automation_policy.json"
            policy_path.write_text(json.dumps(policy()), encoding="utf-8")

            rc = self.tool.main(["--runtime-dir", str(runtime), "--reports-dir", str(reports), "--approval-json", str(policy_path)])

            self.assertEqual(rc, 0)
            out = json.loads((runtime / "rollback_automation_guard_latest.json").read_text(encoding="utf-8"))
            self.assertEqual(out["status"], "blocked")
            self.assertFalse(out["automatic_rollback_allowed"])
            md = (reports / "rollback_automation_guard_latest.md").read_text(encoding="utf-8")
            self.assertIn("Rollback Automation Guard", md)
            self.assertIn("Automatic rollback allowed: `False`", md)


if __name__ == "__main__":
    unittest.main()
