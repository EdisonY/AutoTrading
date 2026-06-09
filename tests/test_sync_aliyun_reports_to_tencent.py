import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    path = ROOT / "部署工具" / "sync_aliyun_reports_to_tencent.py"
    spec = importlib.util.spec_from_file_location("sync_aliyun_reports_to_tencent_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SyncAliyunReportsToTencentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_paths = {
            "ALIYUN_REPORTS": self.tool.ALIYUN_REPORTS,
            "ALIYUN_RUNTIME": self.tool.ALIYUN_RUNTIME,
            "TENCENT_MIRROR_REPORTS": self.tool.TENCENT_MIRROR_REPORTS,
            "TENCENT_MIRROR_RUNTIME": self.tool.TENCENT_MIRROR_RUNTIME,
            "_REMOTE_HISTORICAL_REFRESHED": self.tool._REMOTE_HISTORICAL_REFRESHED,
        }
        self.tool.ALIYUN_REPORTS = self.root / "reports"
        self.tool.ALIYUN_RUNTIME = self.root / "runtime"
        self.tool.TENCENT_MIRROR_REPORTS = self.root / "server_logs_tencent" / "reports"
        self.tool.TENCENT_MIRROR_RUNTIME = self.root / "server_logs_tencent" / "runtime"
        self.tool._REMOTE_HISTORICAL_REFRESHED = True
        for path in (
            self.tool.ALIYUN_REPORTS,
            self.tool.ALIYUN_RUNTIME,
            self.tool.TENCENT_MIRROR_REPORTS,
            self.tool.TENCENT_MIRROR_RUNTIME,
        ):
            path.mkdir(parents=True)

    def tearDown(self):
        for name, value in self.old_paths.items():
            setattr(self.tool, name, value)
        self.tmp.cleanup()

    def write_progress(self, path: Path, rows: int, percent: float, completed: int) -> None:
        path.write_text(
            json.dumps(
                {
                    "generated_at": f"2026-06-09T15:{rows % 60:02d}:00+08:00",
                    "status": "planned",
                    "mode": "plan_only",
                    "progress": {
                        "written_rows": rows,
                        "percent": percent,
                        "completed_requests": completed,
                        "skipped_existing": 0,
                        "failed_requests": 0,
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def test_historical_progress_prefers_rows_over_completed_requests(self):
        local = self.tool.ALIYUN_RUNTIME / self.tool.HISTORICAL_JSON
        mirror = self.tool.TENCENT_MIRROR_RUNTIME / self.tool.HISTORICAL_JSON
        self.write_progress(local, rows=2199, percent=0.12, completed=50)
        self.write_progress(mirror, rows=19983, percent=1.05, completed=0)

        payload, source = self.tool.best_historical_payload()

        self.assertEqual(source, mirror)
        self.assertEqual(payload["progress"]["written_rows"], 19983)
        self.assertTrue(self.tool.use_tencent_historical_progress())

    def test_skip_stale_embedded_report_when_latest_rows_missing(self):
        mirror = self.tool.TENCENT_MIRROR_RUNTIME / self.tool.HISTORICAL_JSON
        self.write_progress(mirror, rows=19983, percent=1.05, completed=0)
        stale_index = self.tool.ALIYUN_REPORTS / "index.html"
        stale_index.write_text("历史数据拉取进度 2199 行", encoding="utf-8")
        fresh_index = self.tool.ALIYUN_REPORTS / "decision_portal_latest.html"
        fresh_index.write_text("历史数据拉取进度 19983 行", encoding="utf-8")

        self.assertTrue(self.tool.should_skip_upload(self.tool.ALIYUN_REPORTS, "index.html", stale_index))
        self.assertFalse(
            self.tool.should_skip_upload(
                self.tool.ALIYUN_REPORTS,
                "decision_portal_latest.html",
                fresh_index,
            )
        )


if __name__ == "__main__":
    unittest.main()
