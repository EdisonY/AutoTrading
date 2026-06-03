import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_counterfactual():
    path = ROOT / "部署工具/counterfactual_open_skips.py"
    spec = importlib.util.spec_from_file_location("counterfactual_open_skips_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class CounterfactualReplayFillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_counterfactual()

    def test_evaluate_uses_shared_replay_fill_kernel(self):
        event_ts = datetime(2026, 6, 1, 0, 0, 30, tzinfo=timezone.utc)
        entry_ts = event_ts.replace(second=0, microsecond=0) + timedelta(minutes=1)
        entry_ms = int(entry_ts.timestamp() * 1000)
        event = self.tool.SkipEvent(
            event_id=1,
            ts=event_ts,
            strategy="A/v11",
            symbol="ABCUSDT",
            side="long",
            timeframe="1m",
            score=88.0,
            stage="risk",
            layer="position",
            reason="test skip",
            sentinel=False,
            replay_decision="reject",
            replay_gate="position",
            payload={},
        )
        bars = {
            "ABCUSDT": {
                entry_ms: [entry_ms, "100", "101.5", "99.5", "100.5"],
                entry_ms + 60_000: [entry_ms + 60_000, "100.5", "101", "100", "100.8"],
            }
        }

        result = self.tool.evaluate(
            event,
            horizon=2,
            bars_by_symbol=bars,
            now=entry_ts + timedelta(minutes=3),
            margin_usdt=100,
            leverage=4,
            tp_pct=1,
            sl_pct=1,
        )

        self.assertEqual(result.status, "complete")
        self.assertEqual(result.barrier_outcome, "take_profit")
        self.assertEqual(result.end_price, 101.0)
        self.assertAlmostEqual(result.sim_pnl_usdt, 3.598, places=3)
        self.assertEqual(result.replay_fill["exit_reason"], "take_profit")

    def test_evaluate_can_apply_partial_fill_cap(self):
        event_ts = datetime(2026, 6, 1, 0, 0, 30, tzinfo=timezone.utc)
        entry_ts = event_ts.replace(second=0, microsecond=0) + timedelta(minutes=1)
        entry_ms = int(entry_ts.timestamp() * 1000)
        event = self.tool.SkipEvent(
            event_id=11,
            ts=event_ts,
            strategy="A/v11",
            symbol="PARTUSDT",
            side="long",
            timeframe="1m",
            score=88.0,
            stage="risk",
            layer="position",
            reason="test partial",
            sentinel=False,
            replay_decision="reject",
            replay_gate="position",
            payload={},
        )
        bars = {
            "PARTUSDT": {
                entry_ms: [entry_ms, "100", "101.5", "99.5", "100.5"],
                entry_ms + 60_000: [entry_ms + 60_000, "100.5", "101", "100", "100.8"],
            }
        }

        result = self.tool.evaluate(
            event,
            horizon=2,
            bars_by_symbol=bars,
            now=entry_ts + timedelta(minutes=3),
            margin_usdt=100,
            leverage=4,
            tp_pct=1,
            sl_pct=1,
            max_fill_quantity=2,
        )

        self.assertEqual(result.status, "complete")
        self.assertEqual(result.barrier_outcome, "take_profit")
        self.assertAlmostEqual(result.sim_pnl_usdt, 1.799)
        self.assertEqual(result.replay_fill["quantity"], 2)
        self.assertEqual(result.replay_fill["requested_quantity"], 4)
        self.assertEqual(result.replay_fill["unfilled_quantity"], 2)
        self.assertEqual(result.replay_fill["fill_ratio"], 0.5)
        self.assertTrue(result.replay_fill["partial_fill"])

    def test_evaluate_reports_fill_error_when_partial_rejected(self):
        event_ts = datetime(2026, 6, 1, 0, 0, 30, tzinfo=timezone.utc)
        entry_ts = event_ts.replace(second=0, microsecond=0) + timedelta(minutes=1)
        entry_ms = int(entry_ts.timestamp() * 1000)
        event = self.tool.SkipEvent(
            event_id=12,
            ts=event_ts,
            strategy="A/v11",
            symbol="STRICTUSDT",
            side="long",
            timeframe="1m",
            score=88.0,
            stage="risk",
            layer="position",
            reason="test strict partial",
            sentinel=False,
            replay_decision="reject",
            replay_gate="position",
            payload={},
        )
        bars = {
            "STRICTUSDT": {
                entry_ms: [entry_ms, "100", "101.5", "99.5", "100.5"],
                entry_ms + 60_000: [entry_ms + 60_000, "100.5", "101", "100", "100.8"],
            }
        }

        result = self.tool.evaluate(
            event,
            horizon=2,
            bars_by_symbol=bars,
            now=entry_ts + timedelta(minutes=3),
            margin_usdt=100,
            leverage=4,
            tp_pct=1,
            sl_pct=1,
            max_fill_quantity=2,
            allow_partial_fill=False,
        )

        self.assertTrue(result.status.startswith("fill_error:partial fill required"))
        self.assertIsNone(result.sim_pnl_usdt)
        self.assertIsNone(result.replay_fill)

    def test_evaluate_uses_local_depth_cache_when_available(self):
        event_ts = datetime(2026, 6, 1, 0, 0, 30, tzinfo=timezone.utc)
        entry_ts = event_ts.replace(second=0, microsecond=0) + timedelta(minutes=1)
        entry_ms = int(entry_ts.timestamp() * 1000)
        event = self.tool.SkipEvent(
            event_id=13,
            ts=event_ts,
            strategy="A/v11",
            symbol="DEPTHUSDT",
            side="long",
            timeframe="1m",
            score=88.0,
            stage="risk",
            layer="position",
            reason="test depth",
            sentinel=False,
            replay_decision="reject",
            replay_gate="position",
            payload={},
        )
        bars = {
            "DEPTHUSDT": {
                entry_ms: [entry_ms, "100", "101.5", "99.5", "100.5"],
                entry_ms + 60_000: [entry_ms + 60_000, "100.5", "101.3", "100", "101.0"],
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "depth_cache"
            cache_dir.mkdir()
            (cache_dir / "DEPTHUSDT_latest.json").write_text(
                json.dumps(
                    {
                        "symbol": "DEPTHUSDT",
                        "ts": entry_ts.isoformat(),
                        "bids": [["99.9", "10"]],
                        "asks": [["100", "1"], ["101", "3"]],
                    }
                ),
                encoding="utf-8",
            )

            result = self.tool.evaluate(
                event,
                horizon=2,
                bars_by_symbol=bars,
                now=entry_ts + timedelta(minutes=3),
                margin_usdt=100,
                leverage=4,
                tp_pct=1,
                sl_pct=1,
                depth_cache_dirs=[cache_dir],
                depth_max_age_sec=60,
            )

        self.assertEqual(result.status, "complete")
        self.assertEqual(result.replay_fill["entry_fill_source"], "order_book")
        self.assertEqual(result.replay_fill["order_book_levels_used"], 2)
        self.assertEqual(result.replay_fill["depth_snapshot_age_seconds"], 0.0)
        self.assertEqual(result.replay_fill["quantity"], 4.0)
        self.assertAlmostEqual(result.replay_fill["entry_price"], 100.75)
        self.assertAlmostEqual(result.replay_fill["depth_slippage_usdt"], 3.0)
        self.assertAlmostEqual(result.sim_pnl_usdt, 0.5965, places=4)

    def test_a_v11_uses_atr_trailing_exit_when_payload_has_atr(self):
        event_ts = datetime(2026, 6, 1, 0, 0, 30, tzinfo=timezone.utc)
        entry_ts = event_ts.replace(second=0, microsecond=0) + timedelta(minutes=1)
        entry_ms = int(entry_ts.timestamp() * 1000)
        event = self.tool.SkipEvent(
            event_id=2,
            ts=event_ts,
            strategy="A/v11",
            symbol="ATRUSDT",
            side="long",
            timeframe="15m",
            score=90.0,
            stage="threshold",
            layer="strategy",
            reason="test atr trailing",
            sentinel=False,
            replay_decision="reject",
            replay_gate="threshold",
            payload={"raw_event": {"atr": 0.5}},
        )
        bars = {
            "ATRUSDT": {
                entry_ms: [entry_ms, "100", "100.7", "100.4", "100.6"],
                entry_ms + 60_000: [entry_ms + 60_000, "100.6", "100.8", "100.1", "100.2"],
            }
        }

        result = self.tool.evaluate(
            event,
            horizon=2,
            bars_by_symbol=bars,
            now=entry_ts + timedelta(minutes=3),
            margin_usdt=100,
            leverage=4,
            tp_pct=1,
            sl_pct=1,
        )

        self.assertEqual(result.status, "complete")
        self.assertEqual(result.barrier_outcome, "trailing_stop")
        self.assertEqual(result.replay_fill["exit_model"], "a_v11_atr_trailing")
        self.assertEqual(result.replay_fill["trailing_timeframe"], "15m")
        self.assertEqual(result.replay_fill["trailing_activation_atr"], 1.0)
        self.assertEqual(result.replay_fill["trailing_stop_atr"], 1.0)
        self.assertAlmostEqual(result.end_price, 100.3)

    def test_non_a_v11_keeps_fixed_pct_barrier_even_with_atr_payload(self):
        event_ts = datetime(2026, 6, 1, 0, 0, 30, tzinfo=timezone.utc)
        entry_ts = event_ts.replace(second=0, microsecond=0) + timedelta(minutes=1)
        entry_ms = int(entry_ts.timestamp() * 1000)
        event = self.tool.SkipEvent(
            event_id=3,
            ts=event_ts,
            strategy="B/v16",
            symbol="BETAUSDT",
            side="long",
            timeframe="15m",
            score=70.0,
            stage="confirmation",
            layer="strategy",
            reason="test fixed barrier",
            sentinel=False,
            replay_decision="reject",
            replay_gate="confirmation",
            payload={"atr": 0.5},
        )
        bars = {
            "BETAUSDT": {
                entry_ms: [entry_ms, "100", "100.7", "100.4", "100.6"],
                entry_ms + 60_000: [entry_ms + 60_000, "100.6", "100.8", "100.1", "100.2"],
            }
        }

        result = self.tool.evaluate(
            event,
            horizon=2,
            bars_by_symbol=bars,
            now=entry_ts + timedelta(minutes=3),
            margin_usdt=100,
            leverage=4,
            tp_pct=1,
            sl_pct=1,
        )

        self.assertEqual(result.status, "complete")
        self.assertEqual(result.barrier_outcome, "end_of_window")
        self.assertEqual(result.replay_fill["exit_model"], "fixed_pct_barrier")
        self.assertNotIn("trailing_stop_atr", result.replay_fill)

    def test_aggregate_exposes_replay_fill_summary(self):
        event_ts = datetime(2026, 6, 1, 0, 0, 30, tzinfo=timezone.utc)
        event = self.tool.SkipEvent(
            event_id=4,
            ts=event_ts,
            strategy="A/v11",
            symbol="FILLUSDT",
            side="long",
            timeframe="15m",
            score=95.0,
            stage="threshold",
            layer="strategy",
            reason="test fill summary",
            sentinel=False,
            replay_decision="reject",
            replay_gate="threshold",
            payload={},
        )
        rows = [
            self.tool.Result(
                event=event,
                horizon=60,
                entry_ts=event_ts,
                entry_price=100.0,
                end_price=101.0,
                return_pct=0.9,
                sim_pnl_usdt=3.6,
                mfe_pct=1.2,
                mae_pct=0.2,
                barrier_outcome="take_profit",
                status="complete",
                replay_fill={
                    "exit_model": "a_v11_atr_trailing",
                    "exit_reason": "trailing_stop",
                    "gross_pnl_usdt": 4.0,
                    "fee_usdt": 0.4,
                    "slippage_usdt": 0.1,
                    "depth_slippage_usdt": 0.1,
                    "market_impact_usdt": 0.2,
                    "net_pnl_usdt": 3.6,
                    "requested_quantity": 4.0,
                    "quantity": 2.0,
                    "unfilled_quantity": 2.0,
                    "fill_ratio": 0.5,
                    "partial_fill": True,
                    "entry_fill_source": "order_book",
                    "order_book_levels_used": 2,
                    "order_book_available_quantity": 2.0,
                    "order_book_fill_ratio": 0.5,
                    "order_book_queue_ahead_quantity": 1.0,
                    "depth_snapshot_source": "runtime/depth_cache/FILLUSDT_latest.json",
                    "depth_snapshot_age_seconds": 12.0,
                    "bars_held": 7,
                },
            ),
            self.tool.Result(
                event=event,
                horizon=60,
                entry_ts=event_ts,
                entry_price=100.0,
                end_price=99.0,
                return_pct=-0.7,
                sim_pnl_usdt=-2.8,
                mfe_pct=0.4,
                mae_pct=1.1,
                barrier_outcome="stop_loss",
                status="complete",
                replay_fill={
                    "exit_model": "fixed_pct_barrier",
                    "exit_reason": "stop_loss",
                    "gross_pnl_usdt": -2.4,
                    "fee_usdt": 0.4,
                    "slippage_usdt": 0.0,
                    "depth_slippage_usdt": 0.0,
                    "market_impact_usdt": 0.0,
                    "net_pnl_usdt": -2.8,
                    "requested_quantity": 4.0,
                    "quantity": 4.0,
                    "unfilled_quantity": 0.0,
                    "fill_ratio": 1.0,
                    "partial_fill": False,
                    "entry_fill_source": "synthetic",
                    "order_book_levels_used": 0,
                    "order_book_available_quantity": 0.0,
                    "order_book_fill_ratio": 0.0,
                    "bars_held": 3,
                },
            ),
        ]

        summary = self.tool.aggregate(rows)["replay_fill"]

        self.assertEqual(summary["samples"], 2)
        self.assertAlmostEqual(summary["gross_pnl_usdt"], 1.6)
        self.assertAlmostEqual(summary["fee_usdt"], 0.8)
        self.assertAlmostEqual(summary["slippage_usdt"], 0.1)
        self.assertAlmostEqual(summary["depth_slippage_usdt"], 0.1)
        self.assertAlmostEqual(summary["market_impact_usdt"], 0.2)
        self.assertEqual(summary["order_book_fill_count"], 1)
        self.assertAlmostEqual(summary["avg_order_book_available_quantity"], 2.0)
        self.assertAlmostEqual(summary["avg_order_book_fill_ratio"], 0.5)
        self.assertAlmostEqual(summary["avg_order_book_queue_ahead_quantity"], 1.0)
        self.assertEqual(summary["depth_snapshot_count"], 1)
        self.assertAlmostEqual(summary["avg_depth_snapshot_age_seconds"], 12.0)
        self.assertAlmostEqual(summary["net_pnl_usdt"], 0.8)
        self.assertAlmostEqual(summary["requested_quantity"], 8.0)
        self.assertAlmostEqual(summary["filled_quantity"], 6.0)
        self.assertAlmostEqual(summary["unfilled_quantity"], 2.0)
        self.assertEqual(summary["partial_fill_count"], 1)
        self.assertAlmostEqual(summary["avg_fill_ratio"], 0.75)
        self.assertEqual(summary["exit_model_counts"][0], {"name": "a_v11_atr_trailing", "count": 1})
        self.assertIn({"name": "stop_loss", "count": 1}, summary["exit_reason_counts"])
        by_model = {row["exit_model"]: row for row in summary["by_exit_model"]}
        self.assertAlmostEqual(by_model["a_v11_atr_trailing"]["net_pnl_usdt"], 3.6)
        self.assertEqual(by_model["a_v11_atr_trailing"]["partial_fill_count"], 1)
        self.assertEqual(by_model["a_v11_atr_trailing"]["order_book_fill_count"], 1)
        self.assertAlmostEqual(by_model["a_v11_atr_trailing"]["avg_order_book_available_quantity"], 2.0)
        self.assertAlmostEqual(by_model["a_v11_atr_trailing"]["avg_order_book_fill_ratio"], 0.5)
        self.assertAlmostEqual(by_model["a_v11_atr_trailing"]["market_impact_usdt"], 0.2)
        self.assertAlmostEqual(by_model["a_v11_atr_trailing"]["avg_order_book_queue_ahead_quantity"], 1.0)
        self.assertAlmostEqual(by_model["a_v11_atr_trailing"]["avg_fill_ratio"], 0.5)
        self.assertAlmostEqual(by_model["fixed_pct_barrier"]["net_pnl_usdt"], -2.8)


if __name__ == "__main__":
    unittest.main()
