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
    path = ROOT / "部署工具" / "research_kline_backfill.py"
    spec = importlib.util.spec_from_file_location("research_kline_backfill_tool", path)
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


class ResearchKlineBackfillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def test_plan_builds_missing_interval_chunks_without_network(self):
        end_dt = self.tool.parse_dt("2026-06-04T00:00:00+08:00")

        plan = self.tool.build_backfill_plan(
            [],
            symbols=["ABCUSDT"],
            intervals=["15m"],
            target_days=1,
            end_dt=end_dt,
            limit=48,
            max_symbols=10,
        )

        self.assertEqual(plan["summary"]["status"], "ready")
        self.assertEqual(plan["summary"]["requests"], 2)
        self.assertEqual(plan["items"][0]["body"]["symbol"], "ABCUSDT")
        self.assertEqual(plan["items"][0]["body"]["interval"], "15m")
        self.assertEqual(plan["items"][0]["reason"], "target_coverage_gap")

    def test_plan_skips_covered_symbol_interval(self):
        end_dt = self.tool.parse_dt("2026-06-04T00:00:00+08:00")
        existing = [
            {
                "symbol": "ABCUSDT",
                "interval": "1h",
                "open_time_ms": ms("2026-06-03T00:00:00"),
                "open_time": "2026-06-03T08:00:00+08:00",
                "date": "2026-06-03",
            }
        ]

        plan = self.tool.build_backfill_plan(
            existing,
            symbols=["ABCUSDT"],
            intervals=["1h"],
            target_days=1,
            end_dt=end_dt,
            limit=100,
            max_symbols=10,
        )

        self.assertEqual(plan["summary"]["status"], "covered_or_no_symbols")
        self.assertEqual(plan["items"], [])

    def test_main_can_seed_symbols_from_events_when_klines_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = base / "research_store"
            write_jsonl_partition(
                store,
                "events",
                "2026-06-03",
                [
                    {
                        "ts": "2026-06-03T00:00:00+08:00",
                        "strategy": "A/v11",
                        "symbol": "ABCUSDT",
                        "event_type": "SIGNAL",
                    },
                    {
                        "ts": "2026-06-03T00:01:00+08:00",
                        "strategy": "B/v16",
                        "symbol": "ABCUSDT",
                        "event_type": "OPEN_SKIPPED",
                    },
                    {
                        "ts": "2026-06-03T00:02:00+08:00",
                        "strategy": "C/v14",
                        "symbol": "XYZUSDT",
                        "event_type": "SIGNAL",
                    },
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
                    "--intervals",
                    "1h",
                    "--target-days",
                    "1",
                    "--max-symbols",
                    "1",
                    "--end",
                    "2026-06-04T00:00:00+08:00",
                    "--format",
                    "jsonl",
                ]
            )

            payload = json.loads((base / "runtime" / "research_kline_backfill_latest.json").read_text(encoding="utf-8"))
            self.assertEqual(rc, 0)
            self.assertEqual(payload["symbol_source_rows"], 3)
            self.assertEqual(payload["plan"]["symbols"], ["ABCUSDT"])
            self.assertEqual(payload["plan"]["summary"]["requests"], 1)

    def test_submit_plan_writes_queue_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = self.tool.BinanceApiQueue(Path(tmp) / "queue.sqlite3")
            plan = self.tool.build_backfill_plan(
                [],
                symbols=["ABCUSDT"],
                intervals=["1h"],
                target_days=1,
                end_dt=self.tool.parse_dt("2026-06-04T00:00:00+08:00"),
                limit=24,
                max_symbols=10,
            )

            submitted = self.tool.submit_plan(queue, plan["items"], stagger_sec=60)

            self.assertEqual(submitted["submitted"], 1)
            self.assertEqual(queue.summary()["counts"], {"queued": 1})
            leased = queue.lease_next(worker_id="test", at_ms=self.tool.now_ms() + 61_000)
            self.assertEqual(leased.path, "/fapi/v1/klines")
            self.assertEqual(leased.body["symbol"], "ABCUSDT")

    def test_ingest_done_requests_merges_queue_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            queue = self.tool.BinanceApiQueue(base / "queue.sqlite3")
            out_dir = base / "research_store"
            request = queue.submit_request(
                scope="public",
                label="research_kline_backfill",
                method="GET",
                path="/fapi/v1/klines",
                body={
                    "symbol": "ABCUSDT",
                    "interval": "1h",
                    "startTime": ms("2026-06-03T00:00:00"),
                    "endTime": ms("2026-06-03T01:00:00") - 1,
                    "limit": 2,
                },
                idempotency_key="kline_backfill:ABCUSDT:1h:1:2:2",
            )
            leased = queue.lease_next(worker_id="test", at_ms=request.earliest_ms + 1)
            queue.complete_request(
                leased.request_id,
                result_status=200,
                result_body=[
                    [ms("2026-06-03T00:00:00"), "100", "101", "99", "100", "1", ms("2026-06-03T01:00:00") - 1, "100"],
                    [ms("2026-06-03T01:00:00"), "100", "102", "99", "101", "2", ms("2026-06-03T02:00:00") - 1, "202"],
                ],
            )

            result = self.tool.ingest_done_requests(base / "queue.sqlite3", out_dir, "jsonl")

            self.assertEqual(result["done_requests"], 1)
            self.assertEqual(result["backfill_rows"], 2)
            self.assertEqual(result["merged_rows"], 2)
            rows = [
                json.loads(line)
                for line in (out_dir / "klines" / "date=2026-06-03" / "data.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([row["close"] for row in rows], [100, 101])
            conn = sqlite3.connect(str(base / "queue.sqlite3"))
            try:
                status = conn.execute("select status from api_requests").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(status, self.tool.STATUS_DONE)


if __name__ == "__main__":
    unittest.main()
