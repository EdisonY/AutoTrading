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
