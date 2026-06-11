import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    path = ROOT / "部署工具" / "j_k_l_indicator_research_report.py"
    spec = importlib.util.spec_from_file_location("j_k_l_indicator_research_report_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class IndicatorResearchReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def write_history(self, root: Path, *, symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT"), interval: str = "1h", bars: int = 220) -> None:
        progress = root / "runtime" / "historical_kline_backfill_latest.json"
        progress.parent.mkdir(parents=True, exist_ok=True)
        progress.write_text(
            json.dumps(
                {
                    "status": "complete",
                    "progress": {"pending_tasks": 0, "percent": 100.0, "written_rows": bars * len(symbols)},
                    "quality": {
                        "status": "complete_with_provider_gaps",
                        "covered_symbol_count": len(symbols),
                        "covered_symbol_interval_count": len(symbols),
                        "target_symbol_count": len(symbols),
                        "target_symbol_interval_count": len(symbols),
                    },
                    "universe": {"symbols": list(symbols)},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        end = datetime.now(self.tool.CST).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        start = end - timedelta(hours=bars - 1)
        for sidx, symbol in enumerate(symbols):
            price = 100.0 + sidx * 8.0
            rows_by_day: dict[str, list[dict[str, object]]] = {}
            for idx in range(bars):
                ts = start + timedelta(hours=idx)
                wave = 0.010 * (1 if idx % 22 < 9 else -1)
                shock = -0.035 if idx % 37 == 12 else 0.030 if idx % 41 == 18 else 0.0
                drift = 0.001 if idx % 5 else -0.0015
                move = wave + shock + drift + sidx * 0.0002
                open_price = price
                close_price = max(1.0, open_price * (1.0 + move))
                high = max(open_price, close_price) * 1.008
                low = min(open_price, close_price) * 0.992
                open_ms = int(ts.timestamp() * 1000)
                row = {
                    "symbol": symbol,
                    "interval": interval,
                    "date": ts.date().isoformat(),
                    "open_time": ts.isoformat(timespec="seconds"),
                    "open_time_ms": open_ms,
                    "close_time_ms": open_ms + 60 * 60_000 - 1,
                    "open": round(open_price, 8),
                    "high": round(high, 8),
                    "low": round(low, 8),
                    "close": round(close_price, 8),
                    "volume": 1000 + idx,
                    "quote_volume": (1000 + idx) * close_price * (1.8 if shock else 1.0),
                    "source_file": "synthetic-indicator-test",
                }
                rows_by_day.setdefault(ts.date().isoformat(), []).append(row)
                price = close_price
            for day, rows in rows_by_day.items():
                path = root / "research_store" / "historical_klines" / f"date={day}" / "data.jsonl"
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    for row in rows:
                        fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def test_generates_read_only_indicator_research_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_history(root)

            payload = self.tool.run_all(
                root,
                symbols=["BTCUSDT", "ETHUSDT"],
                intervals=["1h"],
                start=datetime.now(self.tool.CST) - timedelta(days=12),
                end=datetime.now(self.tool.CST),
                max_variants=2,
            )

            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["module"], "indicator_research")
            self.assertEqual(payload["engine_parity"], "historical_research_adapter")
            self.assertFalse(payload["safety"]["binance_requests_enabled"])
            self.assertFalse(payload["safety"]["live_config_mutation"])
            self.assertFalse(payload["safety"]["automatic_upgrade_allowed"])
            self.assertIn("j_rsi_mean_reversion", payload["strategies"])
            self.assertIn("k_bollinger_reversion", payload["strategies"])
            self.assertIn("l_supertrend_adx", payload["strategies"])
            html = (root / "reports" / "indicator_research_latest.html").read_text(encoding="utf-8")
            self.assertIn("J/K/L 指标研究报告", html)
            self.assertIn("RSI", html)
            self.assertTrue((root / "runtime" / "indicator_research_latest.json").exists())

    def test_trade_cap_is_per_symbol_not_per_interval(self):
        tool = self.tool
        bars = []
        price = 100.0
        end = datetime.now(tool.CST).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        start = end - timedelta(hours=179)
        for idx in range(180):
            ts = start + timedelta(hours=idx)
            if idx < 40:
                move = 0.002
            elif idx % 6 in (0, 1, 2):
                move = -0.05
            else:
                move = 0.055
            open_price = price
            close_price = max(1.0, open_price * (1.0 + move))
            high = max(open_price, close_price) * 1.002
            low = min(open_price, close_price) * 0.998
            open_ms = int(ts.timestamp() * 1000)
            bars.append(
                {
                    "symbol": "SYNTH",
                    "interval": "1h",
                    "open_time_ms": open_ms,
                    "ts": open_ms,
                    "open_time": ts.isoformat(timespec="seconds"),
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close_price,
                    "volume": 1000 + idx,
                    "quote_volume": (1000 + idx) * close_price,
                }
            )
            price = close_price

        variant = {
            "params": {
                "rsi_length": 2,
                "rsi_low": 45.0,
                "rsi_high": 55.0,
                "adx_max": 100.0,
                "atr_stop_multiplier": 3.0,
                "take_profit_atr": 3.0,
                "trailing_pullback_atr": 0.0,
                "trailing_activation_atr": 99.0,
                "max_hold_bars": 2,
                "trade_size_usdt": 100.0,
                "leverage": 2.0,
            }
        }
        old_cap = tool.shared.MAX_TRADES_PER_SYMBOL
        try:
            tool.shared.MAX_TRADES_PER_SYMBOL = 2
            trades = tool.run_j_interval("1h", {"AAAUSDT": bars, "BBBUSDT": bars}, variant)
        finally:
            tool.shared.MAX_TRADES_PER_SYMBOL = old_cap

        counts = {}
        for trade in trades:
            counts[trade["symbol"]] = counts.get(trade["symbol"], 0) + 1
        self.assertEqual(counts, {"AAAUSDT": 2, "BBBUSDT": 2})


if __name__ == "__main__":
    unittest.main()
