import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    path = ROOT / "部署工具" / "historical_kline_backfill.py"
    spec = importlib.util.spec_from_file_location("historical_kline_backfill_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def ms(ts: str) -> int:
    return int(datetime.fromisoformat(ts).replace(tzinfo=timezone.utc).timestamp() * 1000)


class HistoricalKlineBackfillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def test_choose_top_symbols_filters_stable_and_uses_coingecko_top_symbols(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            (runtime / "market_data_cache.json").write_text(
                json.dumps(
                    {
                        "coingecko_top_symbols": ["BTCUSDT", "USDTUSDT", "ETHUSDT", "USDCUSDT", "SOLUSDT"],
                        "top_symbols": ["SHOULDNOTUSEUSDT"],
                        "available_symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            symbols, meta = self.tool.choose_top_symbols(runtime, [], 3)

            self.assertEqual(symbols, ["BTCUSDT", "ETHUSDT", "SOLUSDT"])
            self.assertEqual(meta["source"], "market_data_cache.coingecko_top_symbols")
            self.assertIn("USDTUSDT", meta["rejected_preview"])
            self.assertIn("USDCUSDT", meta["rejected_preview"])

    def test_choose_top_symbols_falls_back_to_tencent_mirror_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = base / "runtime"
            mirror = base / "server_logs_tencent" / "runtime"
            runtime.mkdir()
            mirror.mkdir(parents=True)
            (mirror / "market_data_cache.json").write_text(
                json.dumps(
                    {
                        "coingecko_top_symbols": ["BTCUSDT", "ETHUSDT"],
                        "available_symbols": ["BTCUSDT", "ETHUSDT"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            symbols, meta = self.tool.choose_top_symbols(runtime, [], 2)

            self.assertEqual(symbols, ["BTCUSDT", "ETHUSDT"])
            self.assertIn("server_logs_tencent", meta["cache_path"])

    def test_plan_only_writes_progress_without_provider_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = base / "runtime"
            reports = base / "reports"
            store = base / "research_store"
            runtime.mkdir()
            (runtime / "market_data_cache.json").write_text(
                json.dumps({"coingecko_top_symbols": ["BTCUSDT"], "available_symbols": ["BTCUSDT"]}),
                encoding="utf-8",
            )

            with mock.patch.object(self.tool, "bybit_public_get") as bybit_get, mock.patch.object(self.tool, "okx_public_get") as okx_get:
                rc = self.tool.main(
                    [
                        "--runtime-dir",
                        str(runtime),
                        "--reports-dir",
                        str(reports),
                        "--research-store",
                        str(store),
                        "--top-n",
                        "1",
                        "--days",
                        "1",
                        "--intervals",
                        "1h",
                        "--end",
                        "2026-06-09T00:00:00+08:00",
                        "--format",
                        "jsonl",
                    ]
                )

            payload = json.loads((runtime / "historical_kline_backfill_latest.json").read_text(encoding="utf-8"))
            self.assertEqual(rc, 0)
            bybit_get.assert_not_called()
            okx_get.assert_not_called()
            self.assertEqual(payload["status"], "planned")
            self.assertEqual(payload["mode"], "plan_only")
            self.assertFalse(payload["apply_enabled"])
            self.assertFalse(payload["binance_requests_enabled"])
            self.assertFalse(payload["strategy_frequency_change"])
            self.assertEqual(payload["live_scanner_impact"], "none")
            self.assertTrue((reports / "historical_kline_backfill_latest.md").exists())

    def test_apply_with_mocked_bybit_writes_historical_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = base / "runtime"
            reports = base / "reports"
            store = base / "research_store"
            runtime.mkdir()
            (runtime / "market_data_cache.json").write_text(
                json.dumps({"coingecko_top_symbols": ["BTCUSDT"], "available_symbols": ["BTCUSDT"]}),
                encoding="utf-8",
            )
            open_ms = ms("2026-06-08T15:00:00")
            response = {
                "result": {
                    "list": [
                        [open_ms, "100", "102", "99", "101", "1.5", "151.5"],
                    ]
                }
            }

            with mock.patch.object(self.tool, "bybit_public_get", return_value=response) as bybit_get, mock.patch.object(self.tool, "okx_public_get") as okx_get:
                rc = self.tool.main(
                    [
                        "--runtime-dir",
                        str(runtime),
                        "--reports-dir",
                        str(reports),
                        "--research-store",
                        str(store),
                        "--symbols",
                        "BTCUSDT",
                        "--top-n",
                        "1",
                        "--days",
                        "1",
                        "--intervals",
                        "1h",
                        "--end",
                        "2026-06-09T00:00:00+08:00",
                        "--format",
                        "jsonl",
                        "--limit",
                        "12",
                        "--max-requests",
                        "1",
                        "--max-rps",
                        "1000",
                        "--apply",
                    ]
                )

            payload = json.loads((runtime / "historical_kline_backfill_latest.json").read_text(encoding="utf-8"))
            rows = [
                json.loads(line)
                for line in (store / "historical_klines" / "date=2026-06-08" / "data.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(rc, 0)
            bybit_get.assert_called_once()
            okx_get.assert_not_called()
            self.assertEqual(payload["status"], "paused_request_budget")
            self.assertEqual(payload["mode"], "apply")
            self.assertEqual(payload["progress"]["completed_requests"], 1)
            self.assertEqual(payload["progress"]["written_rows"], 1)
            self.assertFalse(payload["binance_requests_enabled"])
            self.assertFalse(payload["strategy_frequency_change"])
            self.assertEqual(payload["live_scanner_impact"], "none")
            self.assertEqual(rows[0]["symbol"], "BTCUSDT")
            self.assertEqual(rows[0]["interval"], "1h")
            self.assertEqual(rows[0]["close"], 101)
            self.assertEqual(rows[0]["source_file"], "bybit")

    def test_task_planning_count_for_one_symbol_one_interval_one_day(self):
        start_ms = ms("2026-06-08T16:00:00")
        end_ms = ms("2026-06-09T16:00:00") - 1

        tasks = self.tool.chunk_tasks(["BTCUSDT"], ["1h"], start_ms, end_ms, limit=24)

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["expected_bars"], 24)

    def test_task_windows_align_to_kline_open_times(self):
        start_ms = ms("2026-06-08T16:07:13")
        end_ms = ms("2026-06-08T17:14:59")

        tasks = self.tool.chunk_tasks(["BTCUSDT"], ["15m"], start_ms, end_ms, limit=10)

        self.assertEqual(len(tasks), 1)
        self.assertEqual(self.tool.ms_to_iso(tasks[0]["start_ms"]), "2026-06-09T00:15:00+08:00")
        self.assertEqual(self.tool.ms_to_iso(tasks[0]["end_ms"]), "2026-06-09T01:00:00+08:00")
        self.assertEqual(tasks[0]["expected_bars"], 4)

    def test_aligned_existing_rows_cover_task(self):
        start_ms = ms("2026-06-08T16:07:13")
        end_ms = ms("2026-06-08T17:14:59")
        task = self.tool.chunk_tasks(["BTCUSDT"], ["15m"], start_ms, end_ms, limit=10)[0]
        existing = {
            ("BTCUSDT", "15m", ms("2026-06-08T16:15:00")),
            ("BTCUSDT", "15m", ms("2026-06-08T16:30:00")),
            ("BTCUSDT", "15m", ms("2026-06-08T16:45:00")),
            ("BTCUSDT", "15m", ms("2026-06-08T17:00:00")),
        }

        self.assertTrue(self.tool.task_covered(task, existing))

    def test_terminal_unavailable_task_is_marked_and_skipped_on_next_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = base / "runtime"
            reports = base / "reports"
            store = base / "research_store"
            runtime.mkdir()
            (runtime / "market_data_cache.json").write_text(
                json.dumps({"coingecko_top_symbols": ["LEOUSDT"], "available_symbols": ["LEOUSDT"]}),
                encoding="utf-8",
            )

            with mock.patch.object(self.tool, "bybit_kline_window", side_effect=RuntimeError("Symbol Is Invalid")), mock.patch.object(
                self.tool, "okx_kline_window", side_effect=RuntimeError("Instrument ID doesn't exist")
            ):
                rc = self.tool.main(
                    [
                        "--runtime-dir",
                        str(runtime),
                        "--reports-dir",
                        str(reports),
                        "--research-store",
                        str(store),
                        "--symbols",
                        "LEOUSDT",
                        "--days",
                        "1",
                        "--intervals",
                        "1h",
                        "--end",
                        "2026-06-09T00:00:00+08:00",
                        "--format",
                        "jsonl",
                        "--limit",
                        "24",
                        "--max-requests",
                        "1",
                        "--max-rps",
                        "1000",
                        "--apply",
                    ]
                )

            payload = json.loads((runtime / "historical_kline_backfill_latest.json").read_text(encoding="utf-8"))
            self.assertEqual(rc, 0)
            self.assertEqual(payload["progress"]["failed_requests"], 0)
            self.assertEqual(payload["progress"]["skipped_unavailable"], 1)
            self.assertTrue((store / "historical_kline_unavailable" / "data.jsonl").exists())

            rc = self.tool.main(
                [
                    "--runtime-dir",
                    str(runtime),
                    "--reports-dir",
                    str(reports),
                    "--research-store",
                    str(store),
                    "--symbols",
                    "LEOUSDT",
                    "--days",
                    "1",
                    "--intervals",
                    "1h",
                    "--end",
                    "2026-06-09T00:00:00+08:00",
                    "--format",
                    "jsonl",
                ]
            )

            payload = json.loads((runtime / "historical_kline_backfill_latest.json").read_text(encoding="utf-8"))
            self.assertEqual(rc, 0)
            self.assertEqual(payload["progress"]["pending_tasks"], 0)
            self.assertEqual(payload["progress"]["skipped_unavailable"], 1)

    def test_partial_available_task_is_marked_and_skipped_on_next_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = base / "runtime"
            reports = base / "reports"
            store = base / "research_store"
            runtime.mkdir()
            (runtime / "market_data_cache.json").write_text(
                json.dumps({"coingecko_top_symbols": ["BTCUSDT"], "available_symbols": ["BTCUSDT"]}),
                encoding="utf-8",
            )
            open_ms = ms("2026-06-08T23:00:00")
            response = {
                "result": {
                    "list": [
                        [open_ms, "100", "102", "99", "101", "1.5", "151.5"],
                    ]
                }
            }

            with mock.patch.object(self.tool, "bybit_public_get", return_value=response):
                rc = self.tool.main(
                    [
                        "--runtime-dir",
                        str(runtime),
                        "--reports-dir",
                        str(reports),
                        "--research-store",
                        str(store),
                        "--symbols",
                        "BTCUSDT",
                        "--days",
                        "1",
                        "--intervals",
                        "1h",
                        "--end",
                        "2026-06-09T00:00:00+08:00",
                        "--format",
                        "jsonl",
                        "--limit",
                        "24",
                        "--max-requests",
                        "1",
                        "--max-rps",
                        "1000",
                        "--apply",
                    ]
                )

            payload = json.loads((runtime / "historical_kline_backfill_latest.json").read_text(encoding="utf-8"))
            status_rows = [
                json.loads(line)
                for line in (store / "historical_kline_task_status" / "data.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(rc, 0)
            self.assertEqual(payload["progress"]["completed_requests"], 1)
            self.assertEqual(status_rows[-1]["status"], "partial_available")

            rc = self.tool.main(
                [
                    "--runtime-dir",
                    str(runtime),
                    "--reports-dir",
                    str(reports),
                    "--research-store",
                    str(store),
                    "--symbols",
                    "BTCUSDT",
                    "--days",
                    "1",
                    "--intervals",
                    "1h",
                    "--end",
                    "2026-06-09T00:00:00+08:00",
                    "--format",
                    "jsonl",
                ]
            )

            payload = json.loads((runtime / "historical_kline_backfill_latest.json").read_text(encoding="utf-8"))
            self.assertEqual(rc, 0)
            self.assertEqual(payload["progress"]["pending_tasks"], 0)
            self.assertEqual(payload["progress"]["skipped_partial"], 1)


if __name__ == "__main__":
    unittest.main()
