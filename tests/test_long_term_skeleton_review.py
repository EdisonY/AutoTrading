import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    path = ROOT / "部署工具" / "long_term_skeleton_review.py"
    spec = importlib.util.spec_from_file_location("long_term_skeleton_review_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class LongTermSkeletonReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def test_missing_bone_marks_missing_skeleton(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("marker", encoding="utf-8")
            specs = [
                self.tool.module_spec(
                    item_id="P0-X",
                    priority="P0",
                    name="test module",
                    objective="test",
                    inputs=[self.tool.bone("input", "missing.txt")],
                    main=[self.tool.bone("main", "main.py", contains="marker")],
                    outputs=[],
                    portal=[],
                    sync=[],
                    tests=[],
                )
            ]

            payload = self.tool.build_payload(root, specs)

            self.assertEqual(payload["status"], "missing_skeleton")
            self.assertEqual(payload["modules"][0]["status"], "missing_skeleton")
            self.assertIn("inputs:input", payload["modules"][0]["missing_bones"])

    def test_complete_bones_with_blocker_waits_for_staged_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("marker", encoding="utf-8")
            specs = [
                self.tool.module_spec(
                    item_id="P1-X",
                    priority="P1",
                    name="test module",
                    objective="test",
                    inputs=[self.tool.bone("input", "main.py", contains="marker")],
                    main=[],
                    outputs=[],
                    portal=[],
                    sync=[],
                    tests=[],
                    validation_blockers=["fresh data needed"],
                )
            ]

            payload = self.tool.build_payload(root, specs)

            self.assertEqual(payload["status"], "blocked_by_staged_validation")
            self.assertEqual(payload["summary"]["validation_blockers"], 1)

    def test_deployed_flat_root_resolves_release_aliases_and_repo_only_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "long_term_skeleton_review.py").write_text("self", encoding="utf-8")
            (root / "部署工具").mkdir()
            (root / "main.py").write_text("marker", encoding="utf-8")
            (root / "scanner.py").write_text("strategy_gate_case", encoding="utf-8")
            (root / "binance_client.py").write_text("client", encoding="utf-8")
            (root / "systemd").mkdir()
            (root / "systemd" / "crypto-demo.service").write_text("[Service]", encoding="utf-8")
            specs = [
                self.tool.module_spec(
                    item_id="P0-X",
                    priority="P0",
                    name="deployed flat",
                    objective="test deployed aliases",
                    inputs=[self.tool.bone("tool", "部署工具/main.py", contains="marker")],
                    main=[self.tool.bone("scanner", "策略文件/scanner.py", contains="strategy_gate_case")],
                    outputs=[self.tool.bone("client", "交易客户端/binance_client.py")],
                    portal=[],
                    sync=[self.tool.bone("unit", "部署工具/systemd/crypto-demo.service")],
                    tests=[self.tool.bone("repo-only test", "tests/test_demo.py")],
                )
            ]

            payload = self.tool.build_payload(root, specs)
            module = payload["modules"][0]

            self.assertEqual(payload["status"], "skeleton_ready")
            self.assertEqual(module["ready_bones"], module["total_bones"])
            test_item = module["categories"]["tests"]["items"][0]
            self.assertTrue(test_item["ready"])
            self.assertIn("repo-only", test_item["detail"])

    def test_main_writes_runtime_and_report_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            reports = root / "reports"

            rc = self.tool.main(["--root", str(ROOT), "--runtime-dir", str(runtime), "--reports-dir", str(reports)])

            self.assertEqual(rc, 0)
            payload = json.loads((runtime / "long_term_skeleton_latest.json").read_text(encoding="utf-8"))
            self.assertIn("modules", payload["summary"])
            self.assertGreaterEqual(payload["summary"]["modules"], 1)
            md = (reports / "long_term_skeleton_latest.md").read_text(encoding="utf-8")
            self.assertIn("Long-term Skeleton Review", md)


if __name__ == "__main__":
    unittest.main()
