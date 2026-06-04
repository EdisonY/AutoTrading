import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.account_state import build_account_state_payload, write_account_state


ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    path = ROOT / "部署工具" / "account_snapshot_service.py"
    spec = importlib.util.spec_from_file_location("account_snapshot_service_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class AccountSnapshotServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "runtime").mkdir()
        self.old_root = self.tool.ROOT
        self.old_report_dir = self.tool.REPORT_DIR
        self.old_error_path = self.tool.ERROR_PATH
        self.old_error_log_path = self.tool.ERROR_LOG_PATH
        self.old_event_store_db = self.tool.EVENT_STORE_DB
        self.old_source = os.environ.get("ACCOUNT_SNAPSHOT_SOURCE")
        self.tool.ROOT = self.root
        self.tool.REPORT_DIR = self.root / "reports"
        self.tool.ERROR_PATH = self.root / "runtime" / "account_snapshot_error_latest.json"
        self.tool.ERROR_LOG_PATH = self.root / "logs" / "account_snapshot_errors.jsonl"
        self.tool.EVENT_STORE_DB = self.root / "runtime" / "event_store.sqlite3"

    def tearDown(self):
        self.tool.ROOT = self.old_root
        self.tool.REPORT_DIR = self.old_report_dir
        self.tool.ERROR_PATH = self.old_error_path
        self.tool.ERROR_LOG_PATH = self.old_error_log_path
        self.tool.EVENT_STORE_DB = self.old_event_store_db
        if self.old_source is None:
            os.environ.pop("ACCOUNT_SNAPSHOT_SOURCE", None)
        else:
            os.environ["ACCOUNT_SNAPSHOT_SOURCE"] = self.old_source
        self.tmp.cleanup()

    def test_central_snapshot_source_does_not_collect_signed_accounts(self):
        os.environ["ACCOUNT_SNAPSHOT_SOURCE"] = "central"
        payload = build_account_state_payload(
            [
                {
                    "account": "A",
                    "strategy": "A/v11",
                    "version": "v11",
                    "wallet_usdt": 5000,
                    "available_usdt": 4990,
                    "margin_usdt": 5000,
                    "positions": [],
                },
                {
                    "account": "B",
                    "strategy": "B/v16",
                    "version": "v16",
                    "stale": True,
                    "positions": [],
                    "snapshot_error": "waiting for user stream",
                },
                {
                    "account": "C",
                    "strategy": "C/v14",
                    "version": "v14",
                    "stale": True,
                    "positions": [],
                    "snapshot_error": "waiting for user stream",
                },
            ],
            status="partial",
            source="test",
            errors=["waiting for user stream"],
        )
        write_account_state(self.root, payload)

        with mock.patch.object(self.tool, "_collect_account") as collect_account, \
             mock.patch.object(self.tool, "insert_account_snapshot") as insert_snapshot:
            accounts = self.tool.collect_once()

        collect_account.assert_not_called()
        insert_snapshot.assert_called_once()
        self.assertEqual([account["account"] for account in accounts], ["A", "B", "C"])
        self.assertFalse(accounts[0]["stale"])
        self.assertTrue(accounts[1]["stale"])
        summary = json.loads((self.root / "runtime" / "account_snapshot_latest.json").read_text(encoding="utf-8"))["summary"]
        self.assertEqual(summary["fresh_accounts"], 1)
        self.assertEqual(summary["stale_accounts"], ["B", "C"])
        self.assertEqual(summary["wallet_usdt"], 5000.0)


if __name__ == "__main__":
    unittest.main()
