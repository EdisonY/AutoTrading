import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from core.paper_exchange import PaperExchange


class PaperFillModelV2Tests(unittest.TestCase):
    def setUp(self):
        self.old_env = {
            key: os.environ.get(key)
            for key in (
                "PAPER_FILL_MODEL_VERSION",
                "PAPER_FILL_FALLBACK_SPREAD_BPS",
                "PAPER_FILL_FALLBACK_SLIPPAGE_BPS",
                "PAPER_FILL_MAX_DEPTH_AGE_SEC",
            )
        }
        os.environ["PAPER_FILL_MODEL_VERSION"] = "v2"
        os.environ["PAPER_FILL_FALLBACK_SPREAD_BPS"] = "5"
        os.environ["PAPER_FILL_FALLBACK_SLIPPAGE_BPS"] = "2"
        os.environ["PAPER_FILL_MAX_DEPTH_AGE_SEC"] = "600"

    def tearDown(self):
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def write_depth(self, root: Path, symbol: str, bids, asks) -> None:
        path = root / "runtime" / "depth_cache" / f"{symbol}_latest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "symbol": symbol,
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "bids": bids,
                    "asks": asks,
                }
            ),
            encoding="utf-8",
        )

    def latest_fill(self, root: Path) -> dict:
        payload = json.loads((root / "runtime" / "paper_exchange_latest.json").read_text(encoding="utf-8"))
        return payload["recent_fills"][-1]

    def test_fallback_long_open_records_side_aware_synthetic_slippage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            PaperExchange(root).open_market(
                strategy="A/v11",
                symbol="ABCUSDT",
                side="long",
                qty=2,
                price=100,
                leverage=4,
                order_id="open-1",
            )

            fill = self.latest_fill(root)

        self.assertEqual(fill["paper_fill_model_version"], "v2")
        self.assertEqual(fill["paper_fill_source"], "synthetic_fallback")
        self.assertEqual(fill["liquidity_side"], "asks")
        self.assertAlmostEqual(fill["executed_price"], 100.045)
        self.assertEqual(fill["executed_qty"], 2)
        self.assertEqual(fill["fill_ratio"], 1.0)
        self.assertEqual(fill["fallback_reason"], "fresh_depth_snapshot_unavailable")

    def test_order_book_long_open_consumes_asks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_depth(root, "ABCUSDT", bids=[["99.5", "5"]], asks=[["100", "1"], ["101", "2"]])
            PaperExchange(root).open_market(
                strategy="A/v11",
                symbol="ABCUSDT",
                side="long",
                qty=3,
                price=100,
                leverage=4,
                order_id="open-1",
            )

            fill = self.latest_fill(root)

        self.assertEqual(fill["paper_fill_source"], "order_book")
        self.assertEqual(fill["execution_side"], "long")
        self.assertEqual(fill["liquidity_side"], "asks")
        self.assertEqual(fill["order_book_levels_used"], 2)
        self.assertAlmostEqual(fill["executed_price"], 100.6666666667)
        self.assertEqual(fill["executed_qty"], 3)
        self.assertEqual(fill["fill_status"], "FILLED")
        self.assertAlmostEqual(fill["depth_slippage_usdt"], 2.0)

    def test_order_book_short_open_consumes_bids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_depth(root, "ABCUSDT", bids=[["100", "1"], ["99", "2"]], asks=[["100.5", "5"]])
            PaperExchange(root).open_market(
                strategy="B/v16",
                symbol="ABCUSDT",
                side="short",
                qty=3,
                price=100,
                leverage=4,
                order_id="open-1",
            )

            fill = self.latest_fill(root)

        self.assertEqual(fill["paper_fill_source"], "order_book")
        self.assertEqual(fill["execution_side"], "short")
        self.assertEqual(fill["liquidity_side"], "bids")
        self.assertEqual(fill["order_book_levels_used"], 2)
        self.assertAlmostEqual(fill["executed_price"], 99.3333333333)
        self.assertEqual(fill["executed_qty"], 3)

    def test_order_book_open_can_record_partial_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_depth(root, "ABCUSDT", bids=[["99", "5"]], asks=[["100", "2"]])
            PaperExchange(root).open_market(
                strategy="C/v14",
                symbol="ABCUSDT",
                side="long",
                qty=5,
                price=100,
                leverage=4,
                order_id="open-1",
            )

            fill = self.latest_fill(root)

        self.assertEqual(fill["paper_fill_source"], "order_book")
        self.assertEqual(fill["executed_qty"], 2)
        self.assertEqual(fill["unfilled_qty"], 3)
        self.assertEqual(fill["fill_ratio"], 0.4)
        self.assertTrue(fill["partial_fill"])
        self.assertEqual(fill["fill_status"], "PARTIALLY_FILLED")

    def test_close_long_uses_bids_and_realized_pnl_uses_executed_price(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            os.environ["PAPER_FILL_MODEL_VERSION"] = "v1"
            exchange = PaperExchange(root)
            exchange.open_market(
                strategy="A/v11",
                symbol="ABCUSDT",
                side="long",
                qty=2,
                price=100,
                leverage=4,
                order_id="open-1",
            )
            os.environ["PAPER_FILL_MODEL_VERSION"] = "v2"
            self.write_depth(root, "ABCUSDT", bids=[["109", "2"]], asks=[["111", "2"]])
            exchange.close_market(
                strategy="A/v11",
                symbol="ABCUSDT",
                side="long",
                qty=2,
                price=110,
                order_id="close-1",
            )

            fill = self.latest_fill(root)

        self.assertEqual(fill["action"], "CLOSE")
        self.assertEqual(fill["execution_side"], "short")
        self.assertEqual(fill["liquidity_side"], "bids")
        self.assertAlmostEqual(fill["executed_price"], 109.0)
        self.assertAlmostEqual(fill["realized_pnl"], (109.0 - 100.0) * 2 - 109.0 * 2 * 0.0004)


if __name__ == "__main__":
    unittest.main()
