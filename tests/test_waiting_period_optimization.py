import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    path = ROOT / "部署工具" / "waiting_period_optimization.py"
    spec = importlib.util.spec_from_file_location("waiting_period_optimization_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class WaitingPeriodOptimizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def create_event_db(self, path: Path) -> None:
        now = datetime.now(timezone(timedelta(hours=8)))
        with closing(sqlite3.connect(path)) as conn:
            conn.execute(
                """
                create table events(
                    id integer primary key,
                    ts text,
                    strategy text,
                    symbol text,
                    event_type text,
                    reason text,
                    stage text,
                    layer text,
                    payload_json text
                )
                """
            )
            conn.execute(
                "insert into events(ts,strategy,symbol,event_type,reason,stage,layer,payload_json) values(?,?,?,?,?,?,?,?)",
                ((now - timedelta(minutes=5)).isoformat(), "A/v11", "BTCUSDT", "OPEN_SKIPPED", "duplicate_position", "", "", "{}"),
            )
            conn.execute(
                "insert into events(ts,strategy,symbol,event_type,reason,stage,layer,payload_json) values(?,?,?,?,?,?,?,?)",
                ((now - timedelta(minutes=4)).isoformat(), "B/v16", "ETHUSDT", "OPEN_FAILED", "exchange_error", "", "", "{}"),
            )
            conn.execute(
                "insert into events(ts,strategy,symbol,event_type,reason,stage,layer,payload_json) values(?,?,?,?,?,?,?,?)",
                ((now - timedelta(minutes=4)).isoformat(), "B/v16", "ATOMUSDT", "OPEN_FAILED", "open_submitted_unconfirmed", "", "", "{}"),
            )
            conn.execute(
                "insert into events(ts,strategy,symbol,event_type,reason,stage,layer,payload_json) values(?,?,?,?,?,?,?,?)",
                (
                    (now - timedelta(minutes=3)).isoformat(),
                    "B/v16",
                    "",
                    "SCAN_STATS",
                    "",
                    "",
                    "",
                    json.dumps({"score_low": 2, "confirm_fail": 1, "opened": 0}),
                ),
            )
            conn.execute(
                """
                create table sentinel_scans(
                    id integer primary key,
                    ts text,
                    strategy text,
                    symbol text
                )
                """
            )
            conn.execute(
                "insert into sentinel_scans(ts,strategy,symbol) values(?,?,?)",
                ((now - timedelta(minutes=2)).isoformat(), "C/v14", "S2USDT"),
            )
            conn.commit()

    def create_queue_db(self, path: Path) -> None:
        with closing(sqlite3.connect(path)) as conn:
            conn.execute(
                """
                create table api_requests(
                    label text,
                    scope text,
                    account text,
                    path text,
                    status text,
                    result_status integer,
                    error text
                )
                """
            )
            conn.execute("create table cooldowns(scope text, account text, until_ms integer)")
            conn.execute(
                "insert into api_requests(label,scope,account,path,status,result_status,error) values(?,?,?,?,?,?,?)",
                ("market-data-cache", "public", "", "/fapi/v1/ticker/24hr", "done", 200, ""),
            )
            conn.commit()

    def test_build_payload_from_local_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            reports = root / "reports"
            mirror_runtime = root / "server_logs_tencent" / "runtime"
            runtime.mkdir(parents=True)
            reports.mkdir()
            mirror_runtime.mkdir(parents=True)
            self.create_queue_db(runtime / "binance_api_queue.sqlite3")
            self.create_event_db(mirror_runtime / "event_store.sqlite3")
            (runtime / "market_data_cache.json").write_text(
                json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(), "available_symbols": ["BTCUSDT", "ETHUSDT"] + [f"S{i}USDT" for i in range(118)]}),
                encoding="utf-8",
            )
            (runtime / "research_store_summary_latest.json").write_text(
                json.dumps({"kline_acceptance": {"status": "coverage_gap", "target_met": False, "gap_intervals": ["15m"]}}),
                encoding="utf-8",
            )
            (runtime / "research_kline_backfill_latest.json").write_text(
                json.dumps({"plan": {"summary": {"requests": 7}}}),
                encoding="utf-8",
            )
            (runtime / "research_depth_backfill_latest.json").write_text(
                json.dumps({"plan": {"summary": {"requests": 3}}}),
                encoding="utf-8",
            )

            payload = self.tool.build_payload(root=root, hours=24)
            self.assertEqual(payload["safety"], self.tool.SAFETY)
            self.assertEqual(payload["status"], "safe_to_optimize_offline")
            self.assertEqual(payload["summary"]["open_skipped"], 1)
            self.assertEqual(payload["summary"]["open_failed"], 2)
            self.assertEqual(payload["summary"]["planned_kline_requests"], 7)
            self.assertEqual(payload["top100"]["coverage_hint"], "ok")
            self.assertGreaterEqual(payload["summary"]["top100_scanned"], 3)
            self.assertIn("已有同币种/同方向仓位", payload["open_skipped"]["plain_reasons"][0]["reason"])
            self.assertTrue(any("订单已提交但未确认成仓" in row["reason"] for row in payload["open_skipped"]["open_failed_plain_reasons"]))
            self.assertFalse(payload["readiness"]["can_raise_frequency"])

            self.tool.write_outputs(runtime, reports, payload)
            self.assertTrue((runtime / "waiting_period_optimization_latest.json").exists())
            md = (reports / "waiting_period_optimization_latest.md").read_text(encoding="utf-8")
            self.assertIn("OPEN_SKIPPED", md)
            self.assertIn("Top100 实扫", md)


if __name__ == "__main__":
    unittest.main()
