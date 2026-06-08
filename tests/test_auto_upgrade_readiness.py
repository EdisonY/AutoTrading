import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    path = ROOT / "部署工具" / "auto_upgrade_readiness.py"
    spec = importlib.util.spec_from_file_location("auto_upgrade_readiness_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def policy(enabled: bool = True) -> dict[str, object]:
    return {
        "approved": enabled,
        "automatic_upgrade_enabled": enabled,
        "approved_by": "operator",
        "approved_at": "2026-06-08T12:00:00+08:00",
        "scope": "strategy_upgrade_candidates",
        "procedure_version": "v1",
    }


def evolution(*, ready: bool = True, rollback: bool = False) -> dict[str, object]:
    decisions = []
    if ready:
        decisions.append(
            {
                "candidate_id": "EXP-ready",
                "strategy": "A/v11",
                "priority": "P1",
                "status": "verified_upgrade_ready",
                "evidence_score": 91,
                "risk_score": 4,
            }
        )
    if rollback:
        decisions.append(
            {
                "candidate_id": "EXP-bad",
                "strategy": "B/v16",
                "priority": "P1",
                "status": "rollback_watch",
            }
        )
    return {
        "generated_at": "2026-06-08T12:00:00+08:00",
        "summary": {
            "expansion_readiness": {"ready_count": 1 if ready else 0},
        },
        "decisions": decisions,
    }


def replay(status: str = "ready_for_operator_review") -> dict[str, object]:
    if status == "ready_for_operator_review":
        return {
            "status": status,
            "summary": {"blockers": 0},
            "components": [{"name": "research_store", "status": "ok", "ready": True, "category": "ready"}],
        }
    return {
        "status": status,
        "summary": {"blockers": 1},
        "components": [
            {
                "name": "a_v11_rollout",
                "status": "waiting_for_samples",
                "ready": False,
                "category": "sample_gap",
                "detail": "72h paired trades 3/10",
            }
        ],
    }


def calibration() -> dict[str, object]:
    return {
        "status": "approved",
        "approved": True,
        "pairs": 12,
        "min_pairs": 10,
        "max_abs_slippage_bps": 8.0,
        "allowed_slippage_bps": 20.0,
    }


class AutoUpgradeReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def test_missing_policy_and_invalid_strategy_json_are_non_sample_blockers(self):
        payload = self.tool.build_payload(
            strategy_evolution_result={"payload": None, "status": "parse_error", "path": "bad.json", "error": "broken"},
            replay_readiness=replay(),
            rollback_watch={"summary": {"p0": 0, "p1": 0}},
            a_rollout={},
            b_rollout={},
            policy=None,
            policy_path=Path("research_memory/approvals/auto_upgrade_policy.json"),
            paper_real_calibration=calibration(),
            calibration_path=Path("calibration.json"),
            max_age_hours=99999,
        )

        self.assertEqual(payload["status"], "blocked_non_sample_gaps")
        self.assertFalse(payload["automatic_upgrade_allowed"])
        self.assertFalse(payload["apply_enabled"])
        self.assertIn("explicit_auto_upgrade_policy_missing", payload["non_sample_blockers"])
        self.assertIn("strategy_evolution_json:parse_error", payload["non_sample_blockers"])

    def test_waiting_samples_only_when_other_preconditions_clear(self):
        payload = self.tool.build_payload(
            strategy_evolution_result={"payload": evolution(), "status": "ok", "path": "strategy.json", "error": ""},
            replay_readiness=replay("context_gap"),
            rollback_watch={"summary": {"p0": 0, "p1": 0}},
            a_rollout={"replay_fill_comparison": {"72h": {"paired_trades": 3, "completed": 1}}},
            b_rollout={},
            policy=policy(),
            policy_path=Path("policy.json"),
            paper_real_calibration=calibration(),
            calibration_path=Path("calibration.json"),
            max_age_hours=99999,
        )

        self.assertEqual(payload["status"], "waiting_for_samples_report_only")
        self.assertTrue(payload["waiting_samples_only"])
        self.assertEqual(payload["summary"]["non_sample_blockers"], 0)
        self.assertGreater(payload["summary"]["sample_blockers"], 0)
        self.assertFalse(payload["automatic_upgrade_allowed"])

    def test_all_preconditions_still_report_only(self):
        payload = self.tool.build_payload(
            strategy_evolution_result={"payload": evolution(), "status": "ok", "path": "strategy.json", "error": ""},
            replay_readiness=replay(),
            rollback_watch={"summary": {"p0": 0, "p1": 0}},
            a_rollout={},
            b_rollout={},
            policy=policy(),
            policy_path=Path("policy.json"),
            paper_real_calibration=calibration(),
            calibration_path=Path("calibration.json"),
            max_age_hours=99999,
        )

        self.assertEqual(payload["status"], "preconditions_met_report_only")
        self.assertTrue(payload["preconditions_met"])
        self.assertFalse(payload["automatic_upgrade_allowed"])
        self.assertFalse(payload["apply_enabled"])
        self.assertEqual(payload["candidates"][0]["automation_status"], "preconditions_met_report_only")

    def test_rollback_pressure_blocks_upgrade_readiness(self):
        payload = self.tool.build_payload(
            strategy_evolution_result={"payload": evolution(rollback=True), "status": "ok", "path": "strategy.json", "error": ""},
            replay_readiness=replay(),
            rollback_watch={"summary": {"p0": 0, "p1": 1, "worst_candidate": "EXP-bad"}},
            a_rollout={},
            b_rollout={},
            policy=policy(),
            policy_path=Path("policy.json"),
            paper_real_calibration=calibration(),
            calibration_path=Path("calibration.json"),
            max_age_hours=99999,
        )

        self.assertEqual(payload["status"], "blocked_non_sample_gaps")
        self.assertIn("active_p1_rollback_pressure:1", payload["non_sample_blockers"])

    def test_main_writes_runtime_and_report_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            reports = root / "reports"
            approvals = root / "research_memory" / "approvals"
            runtime.mkdir(parents=True)
            approvals.mkdir(parents=True)
            (runtime / "strategy_evolution_latest.json").write_text(json.dumps(evolution()), encoding="utf-8")
            (runtime / "replay_readiness_latest.json").write_text(json.dumps(replay("context_gap")), encoding="utf-8")
            (runtime / "rollback_watch_review_latest.json").write_text(json.dumps({"summary": {"p0": 0, "p1": 0}}), encoding="utf-8")
            (runtime / "paper_real_calibration_latest.json").write_text(json.dumps(calibration()), encoding="utf-8")
            policy_path = approvals / "auto_upgrade_policy.json"
            policy_path.write_text(json.dumps(policy()), encoding="utf-8")

            rc = self.tool.main([
                "--runtime-dir", str(runtime),
                "--reports-dir", str(reports),
                "--approval-json", str(policy_path),
                "--max-age-hours", "99999",
            ])

            self.assertEqual(rc, 0)
            out = json.loads((runtime / "auto_upgrade_readiness_latest.json").read_text(encoding="utf-8"))
            self.assertEqual(out["status"], "waiting_for_samples_report_only")
            self.assertFalse(out["automatic_upgrade_allowed"])
            md = (reports / "auto_upgrade_readiness_latest.md").read_text(encoding="utf-8")
            self.assertIn("Automatic Upgrade Readiness", md)
            self.assertIn("Automatic upgrade allowed: `False`", md)


if __name__ == "__main__":
    unittest.main()
