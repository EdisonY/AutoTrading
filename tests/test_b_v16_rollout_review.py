import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    path = ROOT / "部署工具" / "b_v16_rollout_review.py"
    spec = importlib.util.spec_from_file_location("b_v16_rollout_review_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class BV16RolloutReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def write_research_klines(self, root: Path, symbol: str, interval: str, rows: list[list[object]]) -> None:
        out = root / "research_store" / "klines" / "date=2026-06-03" / "data.jsonl"
        out.parent.mkdir(parents=True, exist_ok=True)
        records = []
        for row in rows:
            open_time_ms = int(row[0])
            records.append(
                {
                    "symbol": symbol,
                    "interval": interval,
                    "open_time_ms": open_time_ms,
                    "open": row[1],
                    "high": row[2],
                    "low": row[3],
                    "close": row[4],
                    "volume": row[5] if len(row) > 5 else 0,
                    "close_time_ms": open_time_ms + 60 * 60_000 - 1,
                    "quote_volume": row[7] if len(row) > 7 else 0,
                }
            )
        out.write_text("\n".join(json.dumps(item) for item in records) + "\n", encoding="utf-8")

    def write_research_depth_snapshot(self, root: Path, symbol: str, snapshot_time: str) -> None:
        out = root / "research_store" / "depth_snapshots" / "date=2026-06-03" / "data.jsonl"
        out.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "symbol": symbol,
            "snapshot_time": snapshot_time,
            "bids_json": json.dumps([["99.9", "10"]]),
            "asks_json": json.dumps([["100", "1"], ["101", "3"]]),
            "source": "test_research_depth_snapshot",
        }
        out.write_text(json.dumps(row) + "\n", encoding="utf-8")

    def replay_db_rows(self) -> list[sqlite3.Row]:
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.execute(
            """
            create table events (
                id integer primary key,
                ts text,
                strategy text,
                symbol text,
                event_type text,
                category text,
                side text,
                reason text,
                payload_json text
            )
            """
        )
        con.executemany(
            """
            insert into events (ts, strategy, symbol, event_type, category, side, reason, payload_json)
            values (?, 'B/v16', 'AAAUSDT', ?, '', 'long', ?, ?)
            """,
            [
                (
                    "2026-06-03T10:00:00+08:00",
                    "OPEN",
                    "",
                    json.dumps(
                        {
                            "symbol": "AAAUSDT",
                            "side": "long",
                            "price": 100,
                            "sl": 95,
                            "tp": 110,
                            "atr": 2,
                            "timeframe": "1h",
                            "exchange_qty": 4,
                            "leverage": 4,
                            "trade_size_usdt": 100,
                        }
                    ),
                ),
                (
                    "2026-06-03T12:00:00+08:00",
                    "CLOSE",
                    "浮动止损",
                    json.dumps(
                        {
                            "symbol": "AAAUSDT",
                            "side": "long",
                            "entry_time": "2026-06-03T10:00:00+08:00",
                            "entry_price": 100,
                            "exit_price": 104,
                            "pnl_usd": 16,
                            "reason": "浮动止损",
                            "timeframe": "1h",
                            "exchange_qty": 4,
                        }
                    ),
                ),
            ],
        )
        rows = list(con.execute("select * from events order by id"))
        con.close()
        return rows

    def replay_guard_db_rows(self) -> list[sqlite3.Row]:
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.execute(
            """
            create table events (
                id integer primary key,
                ts text,
                strategy text,
                symbol text,
                event_type text,
                category text,
                side text,
                reason text,
                payload_json text
            )
            """
        )
        con.executemany(
            """
            insert into events (ts, strategy, symbol, event_type, category, side, reason, payload_json)
            values (?, 'B/v16', ?, ?, '', ?, ?, ?)
            """,
            [
                (
                    "2026-06-03T10:00:00+08:00",
                    "HARDUSDT",
                    "OPEN",
                    "long",
                    "",
                    json.dumps(
                        {
                            "symbol": "HARDUSDT",
                            "side": "long",
                            "price": 100,
                            "sl": 90,
                            "tp": 120,
                            "atr": 2,
                            "timeframe": "1h",
                            "exchange_qty": 4,
                            "leverage": 4,
                            "trade_size_usdt": 100,
                        }
                    ),
                ),
                (
                    "2026-06-03T11:00:00+08:00",
                    "HARDUSDT",
                    "CLOSE",
                    "long",
                    "硬底10%",
                    json.dumps(
                        {
                            "symbol": "HARDUSDT",
                            "side": "long",
                            "entry_time": "2026-06-03T10:00:00+08:00",
                            "entry_price": 100,
                            "exit_price": 97,
                            "pnl_usd": -12,
                            "reason": "硬底10%",
                            "timeframe": "1h",
                            "exchange_qty": 4,
                            "leverage": 4,
                        }
                    ),
                ),
                (
                    "2026-06-03T10:00:00+08:00",
                    "PROTUSDT",
                    "OPEN",
                    "short",
                    "",
                    json.dumps(
                        {
                            "symbol": "PROTUSDT",
                            "side": "short",
                            "price": 100,
                            "sl": 115,
                            "tp": 70,
                            "atr": 20,
                            "timeframe": "1h",
                            "exchange_qty": 4,
                            "leverage": 4,
                            "trade_size_usdt": 100,
                        }
                    ),
                ),
                (
                    "2026-06-03T12:00:00+08:00",
                    "PROTUSDT",
                    "CLOSE",
                    "short",
                    "盈利回撤保护25%",
                    json.dumps(
                        {
                            "symbol": "PROTUSDT",
                            "side": "short",
                            "entry_time": "2026-06-03T10:00:00+08:00",
                            "entry_price": 100,
                            "exit_price": 93,
                            "pnl_usd": 28,
                            "reason": "盈利回撤保护25%",
                            "timeframe": "1h",
                            "exchange_qty": 4,
                            "leverage": 4,
                        }
                    ),
                ),
            ],
        )
        rows = list(con.execute("select * from events order by id"))
        con.close()
        return rows

    def db_rows(self) -> list[sqlite3.Row]:
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.execute(
            """
            create table events (
                id integer primary key,
                ts text,
                strategy text,
                symbol text,
                event_type text,
                category text,
                side text,
                reason text,
                payload_json text
            )
            """
        )
        con.executemany(
            """
            insert into events (ts, strategy, symbol, event_type, category, side, reason, payload_json)
            values (?, 'B/v16', ?, ?, '', ?, ?, ?)
            """,
            [
                (
                    "2026-06-03T10:00:00+08:00",
                    "AAAUSDT",
                    "OPEN",
                    "long",
                    "",
                    "{}",
                ),
                (
                    "2026-06-03T10:30:00+08:00",
                    "AAAUSDT",
                    "CLOSE",
                    "long",
                    "ATR stop band exit",
                    json.dumps({"pnl_usdt": 20, "reason": "ATR stop band exit", "score": 88}),
                ),
                (
                    "2026-06-03T11:00:00+08:00",
                    "BBBUSDT",
                    "FORCED_CLOSE",
                    "short",
                    "交易所硬顶30%",
                    json.dumps({"pnl_usdt": -110, "reason": "交易所硬顶30%", "score": 72}),
                ),
                (
                    "2026-06-03T11:15:00+08:00",
                    "CCCUSDT",
                    "OPEN_FAILED",
                    "long",
                    "exchange reject",
                    json.dumps({"code": -4164, "msg": "Order's notional must be no smaller than 5"}),
                ),
                (
                    "2026-06-03T11:20:00+08:00",
                    "DDDUSDT",
                    "OPEN_FAILED",
                    "short",
                    "position side mismatch",
                    json.dumps({"code": -4061, "msg": "Order's position side does not match user's setting."}),
                ),
            ],
        )
        rows = list(con.execute("select * from events order by id"))
        con.close()
        return rows

    def test_summarize_window_adds_exit_models_open_failures_pf_and_cost(self):
        metrics = self.tool.summarize_window(
            self.db_rows(),
            self.tool.parse_dt("2026-06-03T09:00:00+08:00"),
            self.tool.parse_dt("2026-06-03T12:00:00+08:00"),
        )

        exit_models = {item["model"]: item for item in metrics["exit_models"]}
        open_failed = {item["reason"]: item for item in metrics["open_failed_reasons"]}

        self.assertEqual(metrics["closed_samples"], 2)
        self.assertEqual(metrics["forced_closes"], 1)
        self.assertAlmostEqual(metrics["realized_pnl_usdt"], -90.0)
        self.assertAlmostEqual(metrics["realized_profit_usdt"], 20.0)
        self.assertAlmostEqual(metrics["realized_loss_usdt"], -110.0)
        self.assertAlmostEqual(metrics["profit_factor"], 0.1818)
        self.assertEqual(exit_models["atr_stop_band"]["count"], 1)
        self.assertAlmostEqual(exit_models["atr_stop_band"]["pnl_usdt"], 20.0)
        self.assertEqual(exit_models["forced_or_hard_stop"]["count"], 1)
        self.assertAlmostEqual(exit_models["forced_or_hard_stop"]["pnl_usdt"], -110.0)
        self.assertEqual(open_failed["min_notional"]["count"], 1)
        self.assertEqual(open_failed["position_side_mismatch"]["count"], 1)
        self.assertAlmostEqual(metrics["cost_sensitivity"][0]["estimated_cost_usdt"], 0.8)
        self.assertAlmostEqual(metrics["cost_sensitivity"][2]["pnl_after_cost_usdt"], -92.0)
        self.assertTrue(metrics["cost_sensitivity"][2]["rollback_review_loss_hit"])

    def test_decision_packet_includes_quality_attribution(self):
        windows = {
            "24h": {"closed_samples": 2, "pnl_after_cost_usdt": -10, "open_failed": 1},
            "72h": {
                "closed_samples": 60,
                "pnl_after_cost_usdt": -90,
                "forced_close_rate": 0.12,
                "open_failed_rate": 0.2,
                "profit_factor": 0.65,
                "exit_models": [{"model": "forced_or_hard_stop", "count": 8, "pnl_usdt": -160}],
                "open_failed_reasons": [{"reason": "min_notional", "count": 3}],
                "cost_sensitivity": [
                    {"cost_pct": 0.10, "pnl_after_cost_usdt": -80},
                    {"cost_pct": 0.25, "pnl_after_cost_usdt": -116},
                ],
                "top_losers": [{"symbol": "AAAUSDT", "side": "long", "pnl_usdt": -42}],
                "close_reasons": [{"reason": "交易所硬顶30%", "count": 4}],
            },
            "168h": {"closed_samples": 80},
        }

        packet = self.tool.decision_packet({}, windows, self.tool.verdict(windows))

        self.assertEqual(packet["exit_model_summary_72h"][0]["model"], "forced_or_hard_stop")
        self.assertEqual(packet["open_failed_reasons_72h"][0]["reason"], "min_notional")
        self.assertEqual(packet["cost_sensitivity_72h"][1]["cost_pct"], 0.25)
        self.assertIn("72h profit factor 0.65", packet["risk"])
        self.assertIn("72h exit models: forced_or_hard_stop(8, -160.00)", packet["risk"])
        self.assertIn("72h open failed reasons: min_notional(3)", packet["risk"])
        self.assertIn("72h after-cost pnl at 0.25% cost -116.00 USDT", packet["risk"])

    def test_render_md_includes_new_quality_sections(self):
        payload = {
            "generated_at": "2026-06-03T12:00:00+08:00",
            "approved_at": "2026-05-31T02:00:00+08:00",
            "candidate_ids": ["EXP-20260527-v16-atr-stop-bands"],
            "selected_live_parameter": {"score_max": 85},
            "decision": {"priority": "P1", "status": "manual_review_required", "recommended_actions": ["review"]},
            "decision_packet": {"risk": ["risk"], "rollback_path": ["disabled"], "automation": "disabled_report_only"},
            "windows": {
                "72h": {
                    "opens": 1,
                    "closed_samples": 2,
                    "forced_closes": 1,
                    "open_failed": 1,
                    "realized_pnl_usdt": -90,
                    "estimated_cost_usdt": 1.2,
                    "pnl_after_cost_usdt": -91.2,
                    "profit_factor": 0.18,
                    "forced_close_rate": 0.5,
                    "open_failed_rate": 0.5,
                    "exit_models": [{"model": "forced_or_hard_stop", "count": 1, "pnl_usdt": -110}],
                    "open_failed_reasons": [{"reason": "min_notional", "count": 1}],
                    "cost_sensitivity": [{"cost_pct": 0.25, "estimated_cost_usdt": 2, "pnl_after_cost_usdt": -92, "rollback_review_loss_hit": True}],
                    "top_losers": [],
                }
            },
        }

        md = self.tool.render_md(payload)

        self.assertIn("## 72h Exit Models", md)
        self.assertIn("forced_or_hard_stop", md)
        self.assertIn("## 72h Open Failure Reasons", md)
        self.assertIn("min_notional", md)
        self.assertIn("## 72h Cost Sensitivity", md)
        self.assertIn("## 72h Replay Fill Comparison", md)

    def test_replay_fill_comparison_uses_local_kline_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_root = self.tool.ROOT
            try:
                self.tool.ROOT = Path(tmp)
                cache_dir = Path(tmp) / "runtime" / "kline_cache"
                cache_dir.mkdir(parents=True)
                rows = [
                    [int(self.tool.parse_dt("2026-06-03T10:00:00+08:00").timestamp() * 1000), "100", "103", "99", "102"],
                    [int(self.tool.parse_dt("2026-06-03T11:00:00+08:00").timestamp() * 1000), "102", "105", "101", "104"],
                    [int(self.tool.parse_dt("2026-06-03T12:00:00+08:00").timestamp() * 1000), "104", "106", "102", "103"],
                ]
                (cache_dir / "AAAUSDT_1h_100.json").write_text(json.dumps({"rows": rows}), encoding="utf-8")

                comparison = self.tool.build_replay_fill_comparison(
                    self.replay_db_rows(),
                    self.tool.parse_dt("2026-06-03T09:00:00+08:00"),
                    self.tool.parse_dt("2026-06-03T13:00:00+08:00"),
                )
            finally:
                self.tool.ROOT = old_root

        self.assertEqual(comparison["status"], "ready")
        self.assertEqual(comparison["paired_trades"], 1)
        self.assertEqual(comparison["completed"], 1)
        self.assertEqual(comparison["status_counts"], {"complete": 1})
        self.assertEqual(comparison["top_deltas"][0]["replay_exit_reason"], "trailing_stop")
        self.assertIn("hard-bottom", comparison["note"])
        self.assertIn("profit-retrace", comparison["note"])

    def test_replay_fill_comparison_uses_research_store_and_depth(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_root = self.tool.ROOT
            try:
                self.tool.ROOT = Path(tmp)
                rows = [
                    [int(self.tool.parse_dt("2026-06-03T10:00:00+08:00").timestamp() * 1000), "100", "103", "99", "102"],
                    [int(self.tool.parse_dt("2026-06-03T11:00:00+08:00").timestamp() * 1000), "102", "105", "101", "104"],
                    [int(self.tool.parse_dt("2026-06-03T12:00:00+08:00").timestamp() * 1000), "104", "106", "102", "103"],
                ]
                self.write_research_klines(Path(tmp), "AAAUSDT", "1h", rows)
                self.write_research_depth_snapshot(Path(tmp), "AAAUSDT", "2026-06-03T10:00:00+08:00")

                comparison = self.tool.build_replay_fill_comparison(
                    self.replay_db_rows(),
                    self.tool.parse_dt("2026-06-03T09:00:00+08:00"),
                    self.tool.parse_dt("2026-06-03T13:00:00+08:00"),
                )
            finally:
                self.tool.ROOT = old_root

        top = comparison["top_deltas"][0]
        self.assertEqual(comparison["status"], "ready")
        self.assertEqual(comparison["order_book_fill_count"], 1)
        self.assertEqual(top["entry_fill_source"], "order_book")
        self.assertEqual(top["order_book_levels_used"], 2)
        self.assertIn("research_store", top["kline_source"])
        self.assertIn("depth_snapshots", top["depth_snapshot_source"])

    def test_replay_fill_comparison_models_b_v16_hard_bottom_and_profit_retrace(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_root = self.tool.ROOT
            try:
                self.tool.ROOT = Path(tmp)
                cache_dir = Path(tmp) / "runtime" / "kline_cache"
                cache_dir.mkdir(parents=True)
                hard_rows = [
                    [int(self.tool.parse_dt("2026-06-03T10:00:00+08:00").timestamp() * 1000), "100", "101", "96", "97"],
                    [int(self.tool.parse_dt("2026-06-03T11:00:00+08:00").timestamp() * 1000), "97", "99", "90", "91"],
                ]
                protect_rows = [
                    [int(self.tool.parse_dt("2026-06-03T10:00:00+08:00").timestamp() * 1000), "100", "100", "90", "91"],
                    [int(self.tool.parse_dt("2026-06-03T11:00:00+08:00").timestamp() * 1000), "91", "93", "90", "93"],
                ]
                (cache_dir / "HARDUSDT_1h_100.json").write_text(json.dumps({"rows": hard_rows}), encoding="utf-8")
                (cache_dir / "PROTUSDT_1h_100.json").write_text(json.dumps({"rows": protect_rows}), encoding="utf-8")

                comparison = self.tool.build_replay_fill_comparison(
                    self.replay_guard_db_rows(),
                    self.tool.parse_dt("2026-06-03T09:00:00+08:00"),
                    self.tool.parse_dt("2026-06-03T13:00:00+08:00"),
                )
            finally:
                self.tool.ROOT = old_root

        reasons = {item["reason"]: item["count"] for item in comparison["replay_exit_reasons"]}
        by_symbol = {item["symbol"]: item for item in comparison["top_deltas"]}
        self.assertEqual(comparison["status"], "ready")
        self.assertEqual(comparison["completed"], 2)
        self.assertEqual(reasons["hard_bottom"], 1)
        self.assertEqual(reasons["profit_retrace"], 1)
        self.assertEqual(by_symbol["HARDUSDT"]["replay_exit_reason"], "hard_bottom")
        self.assertEqual(by_symbol["PROTUSDT"]["replay_exit_reason"], "profit_retrace")
        self.assertEqual(by_symbol["HARDUSDT"]["hard_loss_leverage_pct"], 10.0)
        self.assertEqual(by_symbol["PROTUSDT"]["profit_protect_retrace"], 0.25)

    def test_decision_packet_includes_replay_fill_comparison(self):
        windows = {
            "24h": {"closed_samples": 1, "pnl_after_cost_usdt": 1, "forced_close_rate": 0},
            "72h": {"closed_samples": 1, "pnl_after_cost_usdt": 1, "forced_close_rate": 0},
            "168h": {"closed_samples": 1, "pnl_after_cost_usdt": 1, "forced_close_rate": 0},
        }
        replay = {"72h": {"paired_trades": 2, "completed": 1, "pnl_delta_usdt": -3.5}}

        packet = self.tool.decision_packet({}, windows, self.tool.verdict(windows), replay)

        self.assertEqual(packet["replay_fill_comparison_72h"]["completed"], 1)
        self.assertIn("72h replay/fill comparison 1/2 complete, delta -3.50 USDT", packet["risk"])


if __name__ == "__main__":
    unittest.main()
