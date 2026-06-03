import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    path = ROOT / "部署工具" / "replay_readiness_review.py"
    spec = importlib.util.spec_from_file_location("replay_readiness_review_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def rollout_payload(paired: int = 12, completed: int = 11) -> dict[str, object]:
    return {
        "replay_fill_comparison": {
            "72h": {
                "paired_trades": paired,
                "completed": completed,
                "completion_rate": completed / paired if paired else 0,
                "pnl_delta_usdt": -1.5,
                "order_book_fill_count": 3,
            }
        }
    }


class ReplayReadinessReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def thresholds(self):
        return self.tool.ReadinessThresholds(
            min_rollout_paired_trades=10,
            min_rollout_completion_rate=0.80,
            min_recovery_completion_rate=0.80,
            min_depth_symbols=2,
            min_depth_snapshots=5,
        )

    def research_ok(self):
        return {
            "kline_acceptance": {"status": "ok", "target_met": True, "key_intervals": ["15m", "30m", "1h"]},
            "depth_coverage": [
                {"symbol": "AAAUSDT", "snapshots": 3},
                {"symbol": "BBBUSDT", "snapshots": 2},
            ],
        }

    def truth_ok(self):
        return {
            "summary": {"total_recovery_positions": 2},
            "recovery_bar_replay_evidence": {
                "positions": [
                    {"symbol": "AAAUSDT", "action": "bar_replay_hold_bias"},
                    {"symbol": "BBBUSDT", "action": "bar_replay_exit_manual_review"},
                ],
                "data_gap_positions": 0,
            },
        }

    def test_ready_when_all_components_pass(self):
        payload = self.tool.build_payload(
            research_store=self.research_ok(),
            a_v11_rollout=rollout_payload(),
            b_v16_rollout=rollout_payload(),
            truth=self.truth_ok(),
            thresholds=self.thresholds(),
        )

        self.assertEqual(payload["status"], "ready_for_operator_review")
        self.assertEqual(payload["priority"], "P2")
        self.assertEqual(payload["summary"]["blockers"], 0)
        self.assertEqual(payload["next_action"], "review_continue_narrow_rollback")

    def test_data_gap_when_kline_or_depth_not_ready(self):
        research = {
            "kline_acceptance": {"status": "coverage_gap", "target_met": False, "gap_intervals": ["15m"]},
            "depth_coverage": [{"symbol": "AAAUSDT", "snapshots": 1}],
        }

        payload = self.tool.build_payload(
            research_store=research,
            a_v11_rollout=rollout_payload(),
            b_v16_rollout=rollout_payload(),
            truth=self.truth_ok(),
            thresholds=self.thresholds(),
        )

        self.assertEqual(payload["status"], "data_gap")
        self.assertEqual(payload["next_action"], "run_staged_kline_depth_ingest_then_replay_review")
        self.assertEqual(payload["blockers"][0]["name"], "research_store")

    def test_waiting_for_samples_when_rollout_pairs_are_thin(self):
        payload = self.tool.build_payload(
            research_store=self.research_ok(),
            a_v11_rollout=rollout_payload(paired=3, completed=3),
            b_v16_rollout=rollout_payload(),
            truth=self.truth_ok(),
            thresholds=self.thresholds(),
        )

        self.assertEqual(payload["status"], "waiting_for_samples")
        self.assertEqual(payload["blockers"][0]["category"], "sample_gap")

    def test_report_gap_when_recovery_replay_missing_for_existing_recovery_positions(self):
        payload = self.tool.build_payload(
            research_store=self.research_ok(),
            a_v11_rollout=rollout_payload(),
            b_v16_rollout=rollout_payload(),
            truth={"summary": {"total_recovery_positions": 1}},
            thresholds=self.thresholds(),
        )

        self.assertEqual(payload["status"], "report_gap")
        self.assertEqual(payload["next_action"], "regenerate_missing_reports")
        self.assertEqual(payload["blockers"][0]["name"], "recovery_replay")

    def test_main_writes_runtime_and_report_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            reports = root / "reports"
            runtime.mkdir()
            (runtime / "research_store_summary_latest.json").write_text(json.dumps(self.research_ok()), encoding="utf-8")
            (runtime / "a_v11_rollout_review_latest.json").write_text(json.dumps(rollout_payload()), encoding="utf-8")
            (runtime / "b_v16_rollout_review_latest.json").write_text(json.dumps(rollout_payload()), encoding="utf-8")
            (runtime / "strategy_truth_latest.json").write_text(json.dumps(self.truth_ok()), encoding="utf-8")

            old_argv = sys.argv
            try:
                sys.argv = [
                    "replay_readiness_review.py",
                    "--runtime-dir",
                    str(runtime),
                    "--reports-dir",
                    str(reports),
                    "--min-depth-symbols",
                    "2",
                    "--min-depth-snapshots",
                    "5",
                ]
                rc = self.tool.main()
            finally:
                sys.argv = old_argv

            self.assertEqual(rc, 0)
            out = json.loads((runtime / "replay_readiness_latest.json").read_text(encoding="utf-8"))
            self.assertEqual(out["status"], "ready_for_operator_review")
            md = (reports / "replay_readiness_latest.md").read_text(encoding="utf-8")
            self.assertIn("Replay Readiness Review", md)


if __name__ == "__main__":
    unittest.main()
