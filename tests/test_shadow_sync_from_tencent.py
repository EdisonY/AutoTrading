import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    path = ROOT / "部署工具" / "shadow_sync_from_tencent.py"
    spec = importlib.util.spec_from_file_location("shadow_sync_from_tencent_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ShadowSyncFromTencentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def test_stale_tmp_cleanup_is_scoped_and_age_limited(self):
        cmd = self.tool.remote_shadow_sync_stale_cleanup_command()

        self.assertIn("find /tmp -xdev -maxdepth 1", cmd)
        self.assertIn("-name 'autotrading_shadow_sync_*'", cmd)
        self.assertIn("-name 'autotrading_shadow_sync_*.tgz'", cmd)
        self.assertIn("-mmin +15", cmd)
        self.assertIn("-exec rm -rf -- {} +", cmd)

    def test_exit_trap_removes_only_current_tmp_dir(self):
        cmd = self.tool.remote_shadow_sync_exit_trap("/tmp/autotrading_shadow_sync_123")

        self.assertEqual(cmd, "trap \"rm -rf '/tmp/autotrading_shadow_sync_123'\" EXIT")

    def test_backtest_module_small_files_are_mirrored(self):
        self.assertIn("reports/backtest_module_latest.md", self.tool.REPORT_FILES)
        self.assertIn("runtime/backtest_module_latest.json", self.tool.REPORT_FILES)


if __name__ == "__main__":
    unittest.main()
