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
        expected = {"strategy_truth_ledger.py", "sentinel_quality_review.py"}

        for component in ("research", "all"):
            with self.subTest(component=component):
                remotes = {remote for _local, remote in self.tool.TENCENT_COMPONENTS[component]["files"]}

                self.assertTrue(expected.issubset(remotes))

    def test_tencent_binance_components_include_start_guard(self):
        for component in ("sentinel", "account", "account-state", "api-queue", "user-stream", "all"):
            with self.subTest(component=component):
                remotes = {remote for _local, remote in self.tool.TENCENT_COMPONENTS[component]["files"]}

                self.assertIn("binance_start_guard.py", remotes)


if __name__ == "__main__":
    unittest.main()
