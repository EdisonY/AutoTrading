import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    path = ROOT / "部署工具" / "research_depth_backfill.py"
    spec = importlib.util.spec_from_file_location("research_depth_backfill_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def ms(ts: str) -> int:
    return int(datetime.fromisoformat(ts).replace(tzinfo=timezone.utc).timestamp() * 1000)


def write_jsonl_partition(store: Path, table: str, day: str, rows: list[dict[str, object]]) -> None:
    path = store / table / f"date={day}" / "data.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")


class ResearchDepthBackfillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def test_plan_builds_missing_depth_snapshot_without_network(self):
        plan = self.tool.build_depth_plan(
            [],
            symbols=["ABCUSDT"],
            limit=50,
            max_symbols=10,
            max_age_sec=300,
            sample_bucket_sec=300,
            now_dt=self.tool.parse_dt("2026-06-04T00:00:00+08:00"),
        )

        self.assertEqual(plan["summary"]["status"], "ready")
        self.assertEqual(plan["summary"]["requests"], 1)
        self.assertEqual(plan["items"][0]["body"], {"symbol": "ABCUSDT", "limit": 50})
        self.assertEqual(plan["items"][0]["reason"], "missing_depth_snapshot")

    def test_plan_skips_fresh_existing_snapshot(self):
        plan = self.tool.build_depth_plan(
            [
                {
                    "symbol": "ABCUSDT",
                    "snapshot_time": "2026-06-04T00:04:00+08:00",
                    "snapshot_time_ms": ms("2026-06-03T16:04:00"),
                }
            ],
            symbols=["ABCUSDT"],
            limit=100,
            max_symbols=10,
            max_age_sec=300,
            sample_bucket_sec=300,
            now_dt=self.tool.parse_dt("2026-06-04T00:05:00+08:00"),
        )

        self.assertEqual(plan["summary"]["status"], "fresh_or_no_symbols")
        self.assertEqual(plan["items"], [])

    def test_main_can_seed_symbols_from_events_when_depth_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = base / "research_store"
            write_jsonl_partition(
                store,
                "events",
                "2026-06-03",
                [
                    {"ts": "2026-06-03T00:00:00+08:00", "strategy": "A/v11", "symbol": "ABCUSDT"},
                    {"ts": "2026-06-03T00:01:00+08:00", "strategy": "B/v16", "symbol": "ABCUSDT"},
                    {"ts": "2026-06-03T00:02:00+08:00", "strategy": "C/v14", "symbol": "XYZUSDT"},
                ],
            )

            rc = self.tool.main(
                [
                    "--store",
                    str(store),
                    "--queue-db",
                    str(base / "queue.sqlite3"),
                    "--runtime-dir",
                    str(base / "runtime"),
                    "--reports-dir",
                    str(base / "reports"),
                    "--limit",
                    "20",
                    "--max-symbols",
                    "1",
                    "--format",
                    "jsonl",
                ]
            )

            payload = json.loads((base / "runtime" / "research_depth_backfill_latest.json").read_text(encoding="utf-8"))
            self.assertEqual(rc, 0)
            self.assertEqual(payload["symbol_source_rows"], 3)
            self.assertEqual(payload["plan"]["symbols"], ["ABCUSDT"])
            self.assertEqual(payload["plan"]["summary"]["requests"], 1)

    def test_submit_plan_writes_queue_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = self.tool.BinanceApiQueue(Path(tmp) / "queue.sqlite3")
            plan = self.tool.build_depth_plan(
                [],
                symbols=["ABCUSDT"],
                limit=100,
                max_symbols=10,
                max_age_sec=300,
                sample_bucket_sec=300,
                now_dt=self.tool.parse_dt("2026-06-04T00:00:00+08:00"),
            )

            submitted = self.tool.submit_plan(queue, plan["items"], stagger_sec=10)

            self.assertEqual(submitted["submitted"], 1)
            leased = queue.lease_next(worker_id="test", at_ms=self.tool.now_ms() + 11_000)
            self.assertEqual(leased.path, "/fapi/v1/depth")
            self.assertEqual(leased.body["symbol"], "ABCUSDT")
            self.assertEqual(leased.body["limit"], 100)

    def test_ingest_done_requests_merges_depth_and_writes_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            queue = self.tool.BinanceApiQueue(base / "queue.sqlite3")
            out_dir = base / "research_store"
            cache_dir = base / "runtime" / "depth_cache"
            request = queue.submit_request(
                scope="public",
                label="research_depth_snapshot",
                method="GET",
                path="/fapi/v1/depth",
                body={"symbol": "ABCUSDT", "limit": 5},
                idempotency_key="depth_snapshot:ABCUSDT:5:1",
            )
            leased = queue.lease_next(worker_id="test", at_ms=request.earliest_ms + 1)
            queue.complete_request(
                leased.request_id,
                result_status=200,
                result_body={
                    "lastUpdateId": 123,
                    "bids": [["99", "2"], ["98", "3"]],
                    "asks": [["101", "1"], ["102", "4"]],
                },
            )

            result = self.tool.ingest_done_requests(base / "queue.sqlite3", out_dir, "jsonl", cache_dir=cache_dir)

            self.assertEqual(result["done_requests"], 1)
            self.assertEqual(result["backfill_rows"], 1)
            self.assertEqual(result["merged_rows"], 1)
            rows = [
                json.loads(line)
                for line in (out_dir / "depth_snapshots").glob("date=*/data.jsonl").__next__().read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(rows[0]["symbol"], "ABCUSDT")
            self.assertEqual(rows[0]["bid_levels"], 2)
            self.assertEqual(rows[0]["ask_levels"], 2)
            self.assertEqual(rows[0]["spread_bps"], 200.0)
            cache = json.loads((cache_dir / "ABCUSDT_latest.json").read_text(encoding="utf-8"))
            self.assertEqual(cache["bids"][0], ["99", "2"])
            self.assertEqual(cache["asks"][0], ["101", "1"])
            conn = sqlite3.connect(str(base / "queue.sqlite3"))
            try:
                status = conn.execute("select status from api_requests").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(status, self.tool.STATUS_DONE)


if __name__ == "__main__":
    unittest.main()
