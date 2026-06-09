import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    path = ROOT / "部署工具" / "release_manager.py"
    spec = importlib.util.spec_from_file_location("release_manager_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ReleaseManagerBundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def test_tencent_research_bundle_includes_skeleton_report_sources(self):
        expected = {
            "strategy_truth_ledger.py",
            "sentinel_quality_review.py",
            "auto_upgrade_readiness.py",
            "strategy_candidate_governance.py",
            "waiting_period_progress.py",
            "research_memory/approvals/auto_upgrade_policy.json",
        }

        for component in ("research", "all"):
            with self.subTest(component=component):
                remotes = {remote for _local, remote in self.tool.TENCENT_COMPONENTS[component]["files"]}

                self.assertTrue(expected.issubset(remotes))

    def test_aliyun_shadow_bundle_includes_upgrade_governance_reports(self):
        for component in ("shadow", "all"):
            with self.subTest(component=component):
                remotes = {remote for _local, remote in self.tool.ALIYUN_COMPONENTS[component]["files"]}

                self.assertIn("auto_upgrade_readiness.py", remotes)
                self.assertIn("strategy_candidate_governance.py", remotes)
                self.assertIn("waiting_period_progress.py", remotes)
                self.assertIn("research_memory/approvals/auto_upgrade_policy.json", remotes)

    def test_historical_kline_backfill_is_tencent_research_only(self):
        for component in ("research", "all"):
            with self.subTest(target="tencent", component=component):
                remotes = {remote for _local, remote in self.tool.TENCENT_COMPONENTS[component]["files"]}
                self.assertIn("historical_kline_backfill.py", remotes)

        for component in ("shadow", "all"):
            with self.subTest(target="aliyun", component=component):
                remotes = {remote for _local, remote in self.tool.ALIYUN_COMPONENTS[component]["files"]}
                self.assertNotIn("historical_kline_backfill.py", remotes)

        for component in ("portal", "strategy-a", "strategy-b", "strategy-c", "sentinel", "market-data"):
            with self.subTest(component=component):
                remotes = {remote for _local, remote in self.tool.TENCENT_COMPONENTS[component]["files"]}
                self.assertNotIn("historical_kline_backfill.py", remotes)

        for target_name, components in (
            ("tencent", self.tool.TENCENT_COMPONENTS),
            ("aliyun", self.tool.ALIYUN_COMPONENTS),
        ):
            for component, spec in components.items():
                with self.subTest(target=target_name, component=component):
                    posts = "\n".join(spec.get("post") or [])
                    self.assertNotIn("historical_kline_backfill.py", posts)

    def test_tencent_binance_components_include_start_guard(self):
        for component in ("sentinel", "account", "account-state", "api-queue", "user-stream", "all"):
            with self.subTest(component=component):
                remotes = {remote for _local, remote in self.tool.TENCENT_COMPONENTS[component]["files"]}

                self.assertIn("binance_start_guard.py", remotes)

    def test_obsolete_binance_simulation_files_are_not_bundled(self):
        forbidden = {
            "market_mover_sentinel.py",
            "systemd/crypto-market-mover-sentinel.service",
            "paper_sample_executor.py",
        }
        for target, components in (
            (self.tool.TENCENT_COMPONENTS, ("portal", "sentinel", "research", "all")),
            (self.tool.ALIYUN_COMPONENTS, ("shadow", "all")),
        ):
            for component in components:
                with self.subTest(component=component):
                    remotes = {remote for _local, remote in target[component]["files"]}
                    self.assertFalse(forbidden & remotes)


if __name__ == "__main__":
    unittest.main()
