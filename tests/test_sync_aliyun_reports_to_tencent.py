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

    def test_historical_progress_prefers_tencent_mirror_over_local(self):
        local = self.tool.ALIYUN_RUNTIME / self.tool.HISTORICAL_JSON
        mirror = self.tool.TENCENT_MIRROR_RUNTIME / self.tool.HISTORICAL_JSON
        self.write_progress(local, rows=28984, percent=1.52, completed=50)
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

    def test_never_upload_historical_progress_artifacts_back_to_tencent(self):
        local_json = self.tool.ALIYUN_RUNTIME / self.tool.HISTORICAL_JSON
        local_md = self.tool.ALIYUN_REPORTS / self.tool.HISTORICAL_MD
        self.write_progress(local_json, rows=28984, percent=1.52, completed=20)
        local_md.write_text("历史数据拉取进度 28984 行", encoding="utf-8")

        self.assertTrue(self.tool.should_skip_upload(self.tool.ALIYUN_RUNTIME, self.tool.HISTORICAL_JSON, local_json))
        self.assertTrue(self.tool.should_skip_upload(self.tool.ALIYUN_REPORTS, self.tool.HISTORICAL_MD, local_md))

    def test_never_upload_historical_incremental_artifacts_back_to_tencent(self):
        local_json = self.tool.ALIYUN_RUNTIME / self.tool.HISTORICAL_INCREMENTAL_JSON
        local_md = self.tool.ALIYUN_REPORTS / self.tool.HISTORICAL_INCREMENTAL_MD
        self.write_progress(local_json, rows=1200, percent=100.0, completed=12)
        local_md.write_text("每日历史增量 1200 行", encoding="utf-8")

        self.assertTrue(self.tool.should_skip_upload(self.tool.ALIYUN_RUNTIME, self.tool.HISTORICAL_INCREMENTAL_JSON, local_json))
        self.assertTrue(self.tool.should_skip_upload(self.tool.ALIYUN_REPORTS, self.tool.HISTORICAL_INCREMENTAL_MD, local_md))

    def test_backtest_module_small_files_are_priority_synced(self):
        self.assertIn("backtest_module_latest.md", self.tool.REPORT_FILES)
        self.assertIn("backtest_module_latest.md", self.tool.PRIORITY_REPORT_FILES)
        self.assertIn("backtest_module_latest.json", self.tool.RUNTIME_FILES)
        self.assertIn("backtest_module_latest.json", self.tool.PRIORITY_RUNTIME_FILES)

    def test_never_upload_backtest_status_artifacts_back_to_tencent(self):
        local_json = self.tool.ALIYUN_RUNTIME / self.tool.BACKTEST_JSON
        local_md = self.tool.ALIYUN_REPORTS / self.tool.BACKTEST_MD
        local_json.write_text(json.dumps({"latest_job": {"job_id": "bt-20260610-202001-b1b9283a15"}}), encoding="utf-8")
        local_md.write_text("历史回测模块 旧任务", encoding="utf-8")

        self.assertTrue(self.tool.should_skip_upload(self.tool.ALIYUN_RUNTIME, self.tool.BACKTEST_JSON, local_json))
        self.assertTrue(self.tool.should_skip_upload(self.tool.ALIYUN_REPORTS, self.tool.BACKTEST_MD, local_md))


if __name__ == "__main__":
    unittest.main()
