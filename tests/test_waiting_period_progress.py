import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    path = ROOT / "部署工具" / "waiting_period_progress.py"
    spec = importlib.util.spec_from_file_location("waiting_period_progress_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def governance() -> dict[str, object]:
    return {
        "generated_at": "2026-06-08T22:00:00+08:00",
        "automatic_upgrade_allowed": False,
        "apply_enabled": False,
        "sample_acceptance_contract": {
            "status": "not_ready",
            "required_fields": [
                "candidate_id",
                "parameter_version",
                "source_timeframe",
                "atr",
                "entry_time",
                "paper_fill",
                "close_reason",
                "replay_pair_key",
            ],
            "blockers": ["a_v11_rollout:sample_gap", "b_v16_rollout:context_gap"],
            "components": [
                {"name": "research_store", "ready": True, "category": "ready", "detail": "ok"},
                {
                    "name": "a_v11_rollout",
                    "ready": False,
                    "category": "sample_gap",
                    "detail": "72h paired trades 0/10",
                    "paired_trades": 0,
                    "completed": 0,
                },
                {
                    "name": "b_v16_rollout",
                    "ready": False,
                    "category": "context_gap",
                    "detail": "72h replay context completion 0.0%",
                    "paired_trades": 47,
                    "completed": 0,
                    "status_counts": {"missing_open": 2, "missing_atr": 45},
                },
            ],
        },
    }


def b_rollout() -> dict[str, object]:
    return {
        "replay_fill_comparison": {
            "72h": {
                "paired_trades": 47,
                "completed": 0,
                "completion_rate": 0.0,
                "status_counts": {"missing_open": 2, "missing_atr": 45},
                "incomplete_examples": [
                    {"status": "missing_open", "symbol": "AMATUSDT", "side": "short", "timeframe": "1h"},
                    {"status": "missing_atr", "symbol": "ETHUSDT", "side": "long", "timeframe": "1h"},
                ],
            }
        }
    }


def auto_upgrade() -> dict[str, object]:
    return {
        "automatic_upgrade_allowed": False,
        "apply_enabled": False,
        "summary": {"sample_blockers": 4, "non_sample_blockers": 4},
    }


def rollback_execution() -> dict[str, object]:
    return {
        "apply_enabled": False,
        "summary": {"plans": 2, "actionable_plans": 0},
    }


def alerts() -> dict[str, object]:
    return {
        "disk": {"used_pct": 59.7, "free_gb": 17.66, "used_gb": 29.32},
        "api_rate_limits": {"total": 0},
        "api_guard": {"in_cooldown": False, "rolling_count_60s": 1, "public_rolling_count_60s": 5},
    }


def policy() -> dict[str, object]:
    return {
        "approved": False,
        "automatic_upgrade_enabled": False,
        "scope": "strategy_upgrade_candidates",
        "procedure_version": "v1-disabled-template",
    }


class WaitingPeriodProgressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def test_builds_report_only_backlog_with_disabled_apply(self):
        payload, calibration = self.tool.build_payload(
            governance=governance(),
            auto_upgrade=auto_upgrade(),
            rollback_execution=rollback_execution(),
            b_rollout=b_rollout(),
            a_rollout={},
            alerts=alerts(),
            market={"unix_ts": 1780929720, "sources": ["okx", "bybit"], "available_symbols": ["BTCUSDT"], "top_symbols": ["BTCUSDT"]},
            micro={"unix_ts": 1780929720, "coverage_symbols": 118, "fresh_symbols_240s": 100, "retention_days": 14},
            policy=policy(),
            policy_path=Path("research_memory/approvals/auto_upgrade_policy.json"),
        )

        self.assertFalse(payload["automatic_upgrade_allowed"])
        self.assertFalse(payload["automatic_rollback_allowed"])
        self.assertFalse(payload["automatic_tuning_allowed"])
        self.assertFalse(payload["apply_enabled"])
        self.assertFalse(payload["binance_requests_enabled"])
        self.assertEqual(payload["policy"]["status"], "installed_disabled")
        self.assertEqual(payload["b_v16_context_gap"]["missing_atr"], 45)
        self.assertEqual(payload["b_v16_context_gap"]["missing_open"], 2)
        self.assertFalse(calibration["approved"])
        self.assertFalse(calibration["apply_enabled"])

    def test_safety_flags_bad_if_any_apply_is_enabled(self):
        payload, _calibration = self.tool.build_payload(
            governance=governance(),
            auto_upgrade={"automatic_upgrade_allowed": True, "apply_enabled": True, "summary": {}},
            rollback_execution={"apply_enabled": True, "summary": {"plans": 1}},
            b_rollout=b_rollout(),
            a_rollout={},
            alerts=alerts(),
            market={},
            micro={},
            policy={"approved": True, "automatic_upgrade_enabled": True, "procedure_version": "bad"},
            policy_path=Path("policy.json"),
        )

        self.assertEqual(payload["status"], "safety_violation_report_only")
        self.assertFalse(payload["apply_enabled"])
        self.assertIn("automatic_upgrade", payload["safety"]["forbidden_actions"])

    def test_main_writes_progress_and_calibration_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            reports = root / "reports"
            approvals = root / "research_memory" / "approvals"
            runtime.mkdir(parents=True)
            approvals.mkdir(parents=True)
            (runtime / "strategy_candidate_governance_latest.json").write_text(json.dumps(governance()), encoding="utf-8")
            (runtime / "auto_upgrade_readiness_latest.json").write_text(json.dumps(auto_upgrade()), encoding="utf-8")
            (runtime / "rollback_execution_plan_latest.json").write_text(json.dumps(rollback_execution()), encoding="utf-8")
            (runtime / "b_v16_rollout_review_latest.json").write_text(json.dumps(b_rollout()), encoding="utf-8")
            (runtime / "alerts_latest.json").write_text(json.dumps(alerts()), encoding="utf-8")
            policy_path = approvals / "auto_upgrade_policy.json"
            policy_path.write_text(json.dumps(policy()), encoding="utf-8")

            rc = self.tool.main(["--runtime-dir", str(runtime), "--reports-dir", str(reports), "--policy-json", str(policy_path)])

            self.assertEqual(rc, 0)
            out = json.loads((runtime / "waiting_period_progress_latest.json").read_text(encoding="utf-8"))
            self.assertFalse(out["apply_enabled"])
            self.assertIn("sample_quality_contract", {row["id"] for row in out["tasks"]})
            plan = json.loads((runtime / "paper_real_calibration_plan_latest.json").read_text(encoding="utf-8"))
            self.assertFalse(plan["approved"])
            md = (reports / "waiting_period_progress_latest.md").read_text(encoding="utf-8")
            self.assertIn("Waiting Period Progress", md)
            self.assertIn("missing_atr", md)


if __name__ == "__main__":
    unittest.main()
