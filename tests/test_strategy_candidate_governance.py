import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    path = ROOT / "部署工具" / "strategy_candidate_governance.py"
    spec = importlib.util.spec_from_file_location("strategy_candidate_governance_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def evolution_payload() -> dict[str, object]:
    return {
        "generated_at": "2026-06-08T20:00:00+08:00",
        "summary": {
            "expansion_readiness": {
                "items": [
                    {
                        "strategy": "B/v16",
                        "candidate_id": "EXP-20260527-v16-atr-stop-bands",
                        "priority": "P1",
                        "status": "rollback_watch",
                        "quality": "bad",
                        "closed_samples_24h": 99,
                        "required_samples_24h": 25,
                        "missing_samples_24h": 0,
                        "pnl_after_cost_24h": -266.59,
                        "action": "pause_expansion_review_quality",
                    },
                    {
                        "strategy": "A/v11",
                        "candidate_id": "EXP-20260527-v11-trailing-pullback-1p0",
                        "priority": "P2",
                        "status": "full_live_monitoring",
                        "quality": "maturing",
                        "closed_samples_24h": 7,
                        "required_samples_24h": 25,
                        "missing_samples_24h": 18,
                        "pnl_after_cost_24h": 12.69,
                        "action": "continue_controlled_sampling",
                    },
                ]
            }
        },
        "decisions": [
            {
                "priority": "P1",
                "strategy": "B/v16",
                "candidate_id": "EXP-20260527-v16-atr-stop-bands",
                "family_id": "FAM-B-v16-atr-stop-bands",
                "status": "rollback_watch",
                "recommended_action": "investigate_live_degradation",
                "evidence_score": 0,
                "risk_score": 35,
                "blockers": ["24h 实盘窗口质量差 pnl_after_cost=-266.60; profit_factor=0.61<1.05"],
            },
            {
                "priority": "P2",
                "strategy": "A/v11",
                "candidate_id": "EXP-20260527-v11-trailing-pullback-1p0",
                "family_id": "FAM-A-v11-trailing-pullback",
                "status": "full_live_monitoring",
                "recommended_action": "keep_full_live_monitoring",
                "evidence_score": 0,
                "risk_score": 35,
                "blockers": ["24h 实盘样本未达最低数 7/25"],
            },
        ],
    }


def replay_payload() -> dict[str, object]:
    return {
        "status": "context_gap",
        "components": [
            {
                "name": "research_store",
                "status": "ok",
                "ready": True,
                "category": "ready",
                "detail": "research store coverage is sufficient",
                "metrics": {"kline_target_met": True},
            },
            {
                "name": "a_v11_rollout",
                "status": "waiting_for_samples",
                "ready": False,
                "category": "sample_gap",
                "detail": "72h paired trades 0/10",
                "metrics": {"paired_trades": 0, "completed": 0, "min_paired_trades": 10},
            },
            {
                "name": "b_v16_rollout",
                "status": "context_gap",
                "ready": False,
                "category": "context_gap",
                "detail": "72h replay context completion 0.0% below 80%",
                "metrics": {
                    "paired_trades": 46,
                    "completed": 0,
                    "completion_rate": 0.0,
                    "status_counts": {"missing_open": 2, "missing_atr": 44},
                },
            },
        ],
    }


class StrategyCandidateGovernanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def test_builds_lifecycle_rows_and_keeps_apply_disabled(self):
        payload = self.tool.build_payload(
            strategy_evolution=evolution_payload(),
            auto_upgrade={"summary": {"blockers": 8}},
            replay_readiness=replay_payload(),
            rollback_automation={"summary": {"blockers": 4}},
            a_rollout={},
            b_rollout={},
        )

        by_id = {row["candidate_id"]: row for row in payload["candidate_registry"]}

        self.assertEqual(by_id["EXP-20260527-v16-atr-stop-bands"]["lifecycle"], "rollback_watch")
        self.assertEqual(by_id["EXP-20260527-v16-atr-stop-bands"]["recommended_action"], "pause_expansion_review_quality")
        self.assertEqual(by_id["EXP-20260527-v11-trailing-pullback-1p0"]["lifecycle"], "maturing")
        self.assertFalse(payload["automatic_upgrade_allowed"])
        self.assertFalse(payload["apply_enabled"])
        self.assertEqual(payload["summary"]["upgrade_ready_candidates"], 0)

    def test_parameter_registry_contains_b_v16_controlled_knobs(self):
        keys = {row["parameter_key"] for row in self.tool.parameter_registry()}

        self.assertIn("b_v16.score_max", keys)
        self.assertIn("b_v16.atr_stop_bands", keys)

    def test_sample_contract_surfaces_b_v16_context_gaps(self):
        payload = self.tool.build_payload(
            strategy_evolution=evolution_payload(),
            auto_upgrade={},
            replay_readiness=replay_payload(),
            rollback_automation={},
            a_rollout={},
            b_rollout={},
        )

        contract = payload["sample_acceptance_contract"]
        b_row = next(row for row in contract["components"] if row["name"] == "b_v16_rollout")

        self.assertEqual(contract["status"], "not_ready")
        self.assertIn("b_v16_rollout:context_gap", contract["blockers"])
        self.assertEqual(b_row["status_counts"]["missing_atr"], 44)
        self.assertEqual(b_row["status_counts"]["missing_open"], 2)
        self.assertIn("source_timeframe", contract["required_fields"])
        self.assertIn("paper_fill", contract["required_fields"])

    def test_main_writes_json_and_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            reports = root / "reports"
            runtime.mkdir(parents=True)
            (runtime / "strategy_evolution_latest.json").write_text(json.dumps(evolution_payload()), encoding="utf-8")
            (runtime / "replay_readiness_latest.json").write_text(json.dumps(replay_payload()), encoding="utf-8")
            (runtime / "auto_upgrade_readiness_latest.json").write_text(json.dumps({"summary": {"blockers": 8}}), encoding="utf-8")
            (runtime / "rollback_automation_guard_latest.json").write_text(json.dumps({"summary": {"blockers": 4}}), encoding="utf-8")

            rc = self.tool.main(["--runtime-dir", str(runtime), "--reports-dir", str(reports)])

            self.assertEqual(rc, 0)
            out = json.loads((runtime / "strategy_candidate_governance_latest.json").read_text(encoding="utf-8"))
            self.assertFalse(out["apply_enabled"])
            md = (reports / "strategy_candidate_governance_latest.md").read_text(encoding="utf-8")
            self.assertIn("Strategy Candidate Governance", md)
            self.assertIn("pause_expansion_review_quality", md)
            self.assertIn("missing_atr=44", md)


if __name__ == "__main__":
    unittest.main()
