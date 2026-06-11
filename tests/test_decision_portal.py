import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    path = ROOT / "部署工具" / "decision_portal.py"
    spec = importlib.util.spec_from_file_location("decision_portal_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class DecisionPortalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.attention_json = self.root / "research_memory" / "attention" / "open_items.json"
        self.attention_json.parent.mkdir(parents=True)
        self.old_attention_json = self.tool.ATTENTION_JSON
        self.old_live_attention_json = self.tool.LIVE_ATTENTION_JSON
        self.old_mirror_attention_json = self.tool.MIRROR_ATTENTION_JSON
        self.tool.ATTENTION_JSON = self.attention_json
        self.tool.LIVE_ATTENTION_JSON = self.root / "runtime" / "live_attention_latest.json"
        self.tool.MIRROR_ATTENTION_JSON = self.root / "server_logs_tencent" / "runtime" / "live_attention_latest.json"

    def tearDown(self):
        self.tool.ATTENTION_JSON = self.old_attention_json
        self.tool.LIVE_ATTENTION_JSON = self.old_live_attention_json
        self.tool.MIRROR_ATTENTION_JSON = self.old_mirror_attention_json
        self.tmp.cleanup()

    def test_confirm_section_only_shows_current_p0_p1_in_plain_chinese(self):
        payload = {
            "summary": {"open": 2, "counts": {"P0": 0, "P1": 1, "P2": 1}},
            "items": [
                {
                    "item_id": "evolution:exp-20260523-v11-replacement-quality",
                    "priority": "P2",
                    "category": "策略进化",
                    "title": "P2 A/v11 EXP-20260523-v11-replacement-quality",
                    "status": "open",
                    "evidence": "状态 small_live_monitoring",
                    "recommended_action": "keep_small_live_monitoring",
                },
                {
                    "item_id": "rollback:exp-old",
                    "priority": "P1",
                    "category": "策略回滚",
                    "title": "P1 A/v11 回滚观察 EXP-old",
                    "status": "cleared_pending_review",
                    "evidence": "状态 rollback_watch",
                    "recommended_action": "prepare_rollback_review",
                },
                {
                    "item_id": "rollback:exp-current",
                    "priority": "P1",
                    "category": "策略回滚",
                    "title": "P1 A/v11 回滚观察 EXP-current",
                    "status": "open",
                    "evidence": "状态 rollback_watch",
                    "recommended_action": "prepare_rollback_review",
                },
            ],
        }
        self.attention_json.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        _summary, items = self.tool.attention_items()
        html = self.tool.render_attention(items)

        self.assertEqual([item["item_id"] for item in items], ["rollback:exp-current"])
        self.assertIn("A/v11 策略改动上线后表现需要你复核", html)
        self.assertIn("为什么出现", html)
        self.assertIn("你现在要做什么", html)
        self.assertIn("继续收样", html)
        self.assertNotIn("EXP-current", html)
        self.assertNotIn("EXP-old", html)
        self.assertNotIn("replacement-quality", html)

    def test_confirm_section_explains_b_v16_rollback_items(self):
        item = {
            "item_id": "rollback:exp-20260527-v16-atr-stop-bands",
            "priority": "P1",
            "category": "策略回滚",
            "title": "P1 B/v16 回滚观察 EXP-20260527-v16-atr-stop-bands",
            "status": "open",
            "evidence": "状态 rollback_watch；阻塞 24h 实盘窗口质量差 pnl_after_cost=-85.98; profit_factor=0.68<1.05",
            "recommended_action": "investigate_live_degradation",
        }

        html = self.tool.render_attention([item])

        self.assertIn("B/v16 ATR止损带改动上线后表现需要你复核", html)
        self.assertIn("扣费后盈亏约 -85.98 USDT", html)
        self.assertIn("收益因子 PF=0.68", html)
        self.assertIn("PF 警戒线", html)
        self.assertIn("继续收样", html)
        self.assertIn("收窄 B/v16", html)
        self.assertIn("准备回滚", html)
        self.assertIn("/api/attention/decision", self.tool.render_html())
        self.assertNotIn("为什么出现：", html)

    def test_attention_items_uses_newer_live_attention_pull(self):
        self.attention_json.write_text(
            json.dumps({"summary": {"counts": {"P0": 0, "P1": 0}}, "items": []}, ensure_ascii=False),
            encoding="utf-8",
        )
        live_payload = {
            "summary": {"counts": {"P0": 0, "P1": 1}},
            "items": [
                {
                    "item_id": "rollback:exp-20260527-v16-overheat-cap-85",
                    "priority": "P1",
                    "category": "策略回滚",
                    "title": "P1 B/v16 回滚观察 EXP-20260527-v16-overheat-cap-85",
                    "status": "open",
                    "evidence": "pnl_after_cost=-85.98; profit_factor=0.68<1.05",
                    "recommended_action": "investigate_live_degradation",
                }
            ],
        }
        self.tool.LIVE_ATTENTION_JSON.parent.mkdir(parents=True)
        self.tool.LIVE_ATTENTION_JSON.write_text(json.dumps(live_payload, ensure_ascii=False), encoding="utf-8")
        newer = time.time() + 10
        os.utime(self.tool.LIVE_ATTENTION_JSON, (newer, newer))

        _summary, items = self.tool.attention_items()

        self.assertEqual([item["item_id"] for item in items], ["rollback:exp-20260527-v16-overheat-cap-85"])

    def test_strategy_table_separates_execution_failures_from_skips(self):
        rows = [
            {
                "level": "good",
                "name": "B/v16",
                "service": "运行中",
                "age": "1分钟前",
                "opens": "0",
                "closes": "0",
                "open_failed": "1",
                "close_failed": "0",
                "open_skipped": "12",
                "note": "有候选，但15分钟确认没有跟上，所以策略按规则没开仓。",
                "raw_note": "15m无确认信号",
            }
        ]

        html = self.tool.render_strategy_table(rows)

        self.assertIn("开仓执行失败", html)
        self.assertIn("平仓/强平失败", html)
        self.assertIn("候选被挡住", html)
        self.assertIn("有候选，但15分钟确认没有跟上，所以策略按规则没开仓。", html)
        self.assertIn("原始原因：15m无确认信号", html)
        self.assertNotIn("<th>失败</th>", html)

    def test_plain_strategy_reason_explains_raw_gate_reason(self):
        text = self.tool.plain_strategy_reason("15m无确认信号", "skip")
        self.assertEqual(text, "有候选，但15分钟确认没有跟上，所以策略按规则没开仓。")

    def test_fmt_plain_preserves_tiny_nonzero_values(self):
        self.assertEqual(self.tool.fmt_plain(9.5e-09), "9.5e-09")
        self.assertEqual(self.tool.fmt_plain(4.3655745685100555e-11), "4.36557e-11")
        self.assertNotEqual(self.tool.fmt_plain(9.5e-09), "0")

    def test_plain_strategy_reason_explains_non_tradable_external_candidate(self):
        text = self.tool.plain_strategy_reason("合约不存在", "skip")
        self.assertEqual(
            text,
            "候选来自外部行情，但不在当前策略可交易/可模拟合约清单里；系统没有让它进入自建模拟账本，不是账本下单失败。",
        )

    def test_alerts_prefer_tencent_live_mirror_over_newer_shadow_local(self):
        old_runtime = self.tool.RUNTIME_DIR
        old_mirror = self.tool.MIRROR_RUNTIME_DIR
        try:
            runtime = self.root / "runtime"
            mirror = self.root / "server_logs_tencent" / "runtime"
            runtime.mkdir(parents=True)
            mirror.mkdir(parents=True)
            self.tool.RUNTIME_DIR = runtime
            self.tool.MIRROR_RUNTIME_DIR = mirror
            local = runtime / "alerts_latest.json"
            mirrored = mirror / "alerts_latest.json"
            local.write_text(
                json.dumps({"alerts": [{"title": "施工暂停：crypto-data-maintenance.timer", "body": "old shadow wording"}]}, ensure_ascii=False),
                encoding="utf-8",
            )
            mirrored.write_text(
                json.dumps({"alerts": [{"title": "施工暂停：crypto-data-maintenance.timer", "body": "new Tencent live wording"}]}, ensure_ascii=False),
                encoding="utf-8",
            )
            os.utime(local, (time.time() + 10, time.time() + 10))

            payload = self.tool.read_alerts_json()

            self.assertEqual(payload["alerts"][0]["body"], "new Tencent live wording")
        finally:
            self.tool.RUNTIME_DIR = old_runtime
            self.tool.MIRROR_RUNTIME_DIR = old_mirror

    def test_live_runtime_prefers_tencent_mirror_for_paper_ledger(self):
        old_runtime = self.tool.RUNTIME_DIR
        old_mirror = self.tool.MIRROR_RUNTIME_DIR
        try:
            runtime = self.root / "runtime"
            mirror = self.root / "server_logs_tencent" / "runtime"
            runtime.mkdir(parents=True)
            mirror.mkdir(parents=True)
            self.tool.RUNTIME_DIR = runtime
            self.tool.MIRROR_RUNTIME_DIR = mirror
            local = runtime / "paper_exchange_latest.json"
            mirrored = mirror / "paper_exchange_latest.json"
            local.write_text(
                json.dumps({"total_unrealized_pnl": 19.5351}, ensure_ascii=False),
                encoding="utf-8",
            )
            mirrored.write_text(
                json.dumps({"total_unrealized_pnl": 34.266341}, ensure_ascii=False),
                encoding="utf-8",
            )
            os.utime(local, (time.time() + 10, time.time() + 10))

            payload = self.tool.read_live_runtime_json("paper_exchange_latest.json")

            self.assertEqual(payload["total_unrealized_pnl"], 34.266341)
        finally:
            self.tool.RUNTIME_DIR = old_runtime
            self.tool.MIRROR_RUNTIME_DIR = old_mirror

    def test_plain_strategy_reason_separates_post_submit_confirmation(self):
        text = self.tool.plain_strategy_reason(
            "下单失败(open_confirm_account_state_unavailable): fresh central account state unavailable for confirmation",
            "failed",
        )
        self.assertEqual(
            text,
            "订单已经提交，但成交后的账户回执还没回来；系统会等用户流或受控确认补证，不能把它当成策略没信号。",
        )

    def test_plain_strategy_reason_explains_submitted_unconfirmed_order(self):
        text = self.tool.plain_strategy_reason(
            "open_submitted_unconfirmed: order submitted but no executed quantity or confirmed position yet",
            "failed",
        )
        self.assertEqual(
            text,
            "订单已提交到交易所，但还没有确认成交成仓；系统不会先建本地假仓，会等回执或下一轮核对。",
        )

    def test_historical_kline_progress_shows_safe_report_only_status(self):
        payload = {
            "generated_at": "2026-06-09T12:00:00+08:00",
            "status": "planned",
            "mode": "plan_only",
            "apply_enabled": False,
            "live_scanner_impact": "none",
            "binance_requests_enabled": False,
            "strategy_frequency_change": False,
            "config": {"max_rps": 0.5, "intervals": ["15m", "1h"]},
            "universe": {"symbols": ["BTCUSDT", "ETHUSDT"]},
            "progress": {
                "total_tasks": 100,
                "completed_requests": 12,
                "failed_requests": 0,
                "skipped_existing": 3,
                "percent": 15.0,
                "written_rows": 2400,
                "planned_bars_estimate": 120000,
            },
            "last_task": {
                "symbol": "BTCUSDT",
                "interval": "1h",
                "start": "2026-06-08T00:00:00+08:00",
                "end": "2026-06-09T00:00:00+08:00",
                "provider": "bybit",
                "rows": 24,
            },
        }

        html = self.tool.render_historical_kline_progress({"historical_kline": payload})

        self.assertIn("15.00%", html)
        self.assertIn("BTCUSDT, ETHUSDT", html)
        self.assertIn("私有/下单请求 False", html)
        self.assertIn("扫描频率改动 False", html)
        self.assertIn("不影响三策略扫描频率", html)

    def test_completed_historical_baseline_shows_backtest_module_instead_of_progress_panel(self):
        historical = {
            "status": "complete",
            "progress": {"pending_tasks": 0, "percent": 100.0, "written_rows": 1602051},
            "quality": {
                "status": "complete_with_provider_gaps",
                "covered_symbol_count": 26,
                "target_symbol_count": 30,
                "covered_symbol_interval_count": 104,
                "target_symbol_interval_count": 120,
            },
        }
        backtest = {
            "status": "backtest_engine_ready",
            "capabilities": {
                "job_submit_api": True,
                "anti_overfit_gate": True,
                "strategy_replay_adapter": True,
                "strategy_pnl_metrics": True,
            },
            "parameter_schema": {
                "max_tuned_parameters": 3,
                "max_parameter_variants": 24,
                "strategies": {
                    "A/v11": [
                        {
                            "key": "entry_threshold",
                            "label": "入场分数阈值",
                            "min": 80,
                            "max": 140,
                            "step": 1,
                            "note": "越高越少开仓",
                        }
                    ]
                },
            },
            "latest_job": {
                "job_id": "bt-test",
                "created_at": "2026-06-10T15:00:00+08:00",
                "status": "completed",
                "spec": {
                    "strategy": "A/v11",
                    "symbols": ["BTCUSDT"],
                    "interval": "1h",
                    "period_days": 365,
                    "fill_model": "paper_fill_model_v2",
                },
                "result": {
                    "status": "completed",
                    "engine_parity": "research_adapter",
                    "summary": {
                        "net_profit_usdt": 12.34,
                        "trades": 7,
                        "max_drawdown_pct": 1.5,
                        "win_rate_pct": 57.14,
                        "profit_factor": 1.22,
                    },
                    "charts": {
                        "equity_curve": [
                            {"ts": "", "equity": 10000, "net_pnl": 0},
                            {"ts": "2026-06-01T00:00:00+08:00", "equity": 10012.34, "net_pnl": 12.34},
                        ],
                        "monthly_returns": [{"month": "2026-06", "return_pct": 0.12}],
                    },
                    "trades": [
                        {
                            "symbol": "BTCUSDT",
                            "side": "long",
                            "interval": "1h",
                            "entry_ts": "2026-06-01T00:00:00+08:00",
                            "exit_ts": "2026-06-01T04:00:00+08:00",
                            "entry_price": 100,
                            "exit_price": 103,
                            "net_pnl_usdt": 12.34,
                            "fee_usdt": 0.4,
                            "bars_held": 4,
                            "exit_reason": "take_profit",
                            "score": 88,
                            "threshold": 80,
                        }
                    ],
                    "recommendation": {"action": "research_review_only", "reason": "profitable_full_window_but_oos_gate_not_passed"},
                    "parameter_sweep": {"anti_overfit_review": {"status": "no_variant_passed"}},
                },
            },
            "recent_jobs": {
                "full_history_retained": True,
                "display_limit": 20,
                "total_jobs": 1,
                "jobs": [
                    {
                        "job_id": "bt-test",
                        "created_at": "2026-06-10T15:00:00+08:00",
                        "strategy": "A/v11",
                        "symbols": ["BTCUSDT"],
                        "interval": "1h",
                        "status": "completed",
                        "net_profit_usdt": 12.34,
                        "trades": 7,
                        "max_drawdown_pct": 1.5,
                        "profit_factor": 1.22,
                        "recommendation_reason": "profitable_full_window_but_oos_gate_not_passed",
                        "anti_overfit_status": "no_variant_passed",
                        "engine_parity": "research_adapter",
                    }
                ],
            },
        }

        html = self.tool.render_history_or_backtest_panel(
            {
                "historical_kline": historical,
                "historical_kline_incremental": {"status": "planned", "progress": {"written_rows": 10}},
                "backtest_module": backtest,
            }
        )

        self.assertIn("历史回测模块", html)
        self.assertIn("submitBacktestJob", html)
        self.assertIn("反拟合门控启用", html)
        self.assertIn("research_adapter", html)
        self.assertIn("不自动改策略", html)
        self.assertIn("净收益</span><b>+12.34 USDT", html)
        self.assertIn("权益 / PnL 曲线", html)
        self.assertIn("详细开平仓记录", html)
        self.assertIn("backtest-trades-scroll", html)
        self.assertIn("take_profit", html)
        self.assertIn('class="backtest-history-collapse"', html)
        self.assertIn("<details", html)
        self.assertIn("历史任务列表（1）", html)
        self.assertIn("deleteBacktestJob", html)
        self.assertIn("删除只删", html)
        self.assertIn("历史任务不是只保留一个", html)
        self.assertIn("可调参数", html)
        self.assertIn("entry_threshold", html)
        self.assertNotIn("<h2>历史数据拉取进度</h2>", html)

    def test_decision_portal_contains_backtest_api_submit_hook(self):
        html = self.tool.render_html()

        self.assertIn("submitBacktestJob", html)
        self.assertIn("/api/backtest/jobs", html)
        self.assertIn("estimateBacktestRuntime", html)
        self.assertIn("已耗时", html)
        self.assertIn("不显示假进度", html)
        self.assertIn("/api/backtest/job?id=", html)

    def test_strategy_detail_shows_position_pnl_and_fee_without_funding(self):
        account = {
            "accounts": [
                {
                    "account": "B",
                    "strategy": "B/v16",
                    "available_usdt": 4900,
                    "unrealized_pnl_usdt": 1.42161,
                    "open_positions": 1,
                    "positions": [
                        {
                            "symbol": "ARBUSDT",
                            "side": "SHORT",
                            "qty": 4738.7,
                            "entry": 0.0835,
                            "mark": 0.0832,
                            "upnl": 1.42161,
                            "notional": 394.25984,
                            "margin": 98.56496,
                        }
                    ],
                }
            ]
        }

        rows = self.tool.strategy_rows(
            {"strategies": []},
            {"services": {"crypto-scanner-v16.service": "active"}},
            account,
            include_details=True,
        )
        html = self.tool.render_strategy_table(rows)

        self.assertIn("查看持仓盈亏 / 手续费", html)
        self.assertIn("ARBUSDT", html)
        self.assertIn("+1.4216", html)
        self.assertIn("估算：按 taker 0.04%", html)
        self.assertNotIn("资金费率", html)
        self.assertNotIn("funding", html)

    def test_paper_exchange_summary_is_tabbed_report_section(self):
        html = self.tool.render_paper_exchange({
                "paper_exchange": {
                    "mode": "paper_exchange",
                    "ts": "2026-06-06T13:00:00+00:00",
                    "fidelity": {
                        "price": "OKX 15m/latest cached close; Binance mark/index may differ",
                        "time": "updated when paper_exchange_runner runs, not exchange tick-by-tick",
                        "slippage": "not exchange-order-book exact; use conservative model before strategy promotion",
                        "fees": "ledger fee_rate=0.000400",
                    },
                    "total_equity": 300000,
                "total_unrealized_pnl": 12.34,
                "open_positions": 3,
                "by_strategy": {
                    "A/v11": {"positions": 1, "unrealized_pnl": 1.2, "equity": 100001, "realized_pnl": 0, "fees_paid": 0.16},
                    "B/v16": {"positions": 1, "unrealized_pnl": 2.3, "equity": 100002, "realized_pnl": 0, "fees_paid": 0.16},
                    "C/v14": {"positions": 1, "unrealized_pnl": 8.84, "equity": 100008, "realized_pnl": 0, "fees_paid": 0.16},
                },
                "positions": [
                    {
                        "strategy": "A/v11",
                        "symbol": "BTCUSDT",
                        "side": "long",
                        "qty": 0.01,
                        "entry_price": 100,
                        "mark_price": 101,
                        "opened_at": "2026-06-06T13:00:00+00:00",
                        "unrealized_pnl": 1,
                        "notional": 101,
                        "margin": 25.25,
                        "fees_paid": 0.04,
                        "mark_source": "okx_15m",
                    }
                ],
            }
        })

        self.assertIn("自建模拟账本", html)
        self.assertIn("盯市刷新", html)
        self.assertIn("OKX 15分钟K线/本地缓存收盘价", html)
        self.assertIn("不是逐笔盘口撮合", html)
        self.assertIn("strategy-tab active", html)
        self.assertIn("paper-panel active", html)
        self.assertIn("position-detail-row", html)
        self.assertIn("BTCUSDT", html)
        self.assertIn("手续费", html)
        self.assertNotIn("资金费率", html)
        self.assertNotIn("funding", html)
        self.assertIn("300000.00 USDT", html)
        self.assertNotIn("+300000.00 USDT", html)
        self.assertIn("手续费 0.1600", html)
        self.assertNotIn("手续费 +0.1600", html)
        self.assertNotIn("Binance", html)

    def test_market_mover_followup_shows_entry_direction_and_pnl(self):
        html = self.tool.render_market_movers({
            "market": {
                "market_mover_preview": [
                    {
                        "symbol": "BEATUSDT",
                        "reason": "涨幅榜",
                        "change_pct": 27.0,
                        "quote_volume": 123456,
                        "sources": ["okx"],
                    },
                    {
                        "symbol": "MEUSDT",
                        "reason": "跌幅榜",
                        "change_pct": -8.0,
                        "velocity_pct": -0.8,
                        "quote_volume": 1000,
                        "sources": ["bybit"],
                    },
                    {
                        "symbol": "NOENTERUSDT",
                        "reason": "涨幅榜",
                        "change_pct": 2.5,
                        "velocity_pct": 0.9,
                        "quote_volume": 5000,
                        "sources": ["okx"],
                    },
                ]
            },
            "mover_diagnostics": {
                "NOENTERUSDT": {
                    "strategy_filter": "挡：A/v11；未扫：B/v16",
                    "no_entry_reason": "还没达到策略阈值，属于正常筛选。 阶段：阈值；筛选层：策略。",
                    "raw_no_entry_reason": "threshold score below gate",
                }
            },
            "paper_exchange": {
                "positions": [
                    {"strategy": "B/v16", "symbol": "BEATUSDT", "side": "long", "unrealized_pnl": 3.2},
                    {"strategy": "A/v11", "symbol": "MEUSDT", "side": "long", "unrealized_pnl": -1.1},
                ]
            },
        })

        self.assertIn("BEATUSDT", html)
        self.assertIn("已进场", html)
        self.assertIn("顺势", html)
        self.assertIn("MEUSDT", html)
        self.assertIn("逆势", html)
        self.assertIn("NOENTERUSDT", html)
        self.assertIn("起涨初段", html)
        self.assertIn("信号阶段", html)
        self.assertIn("进场结果", html)
        self.assertNotIn("方向关系", html)
        self.assertNotIn("当前结果", html)
        self.assertNotIn("未进场 +0.0000", html)
        self.assertIn("挡：A/v11；未扫：B/v16", html)
        self.assertNotIn("策略挡住(阈值/策略)", html)
        self.assertIn("还没达到策略阈值", html)
        self.assertIn("原始原因：threshold score below gate", html)
        self.assertIn("+3.2000", html)
        self.assertIn("-1.1000", html)

    def test_early_down_mover_uses_tick_direction_not_positive_24h_change(self):
        html = self.tool.render_market_movers({
            "market": {
                "market_mover_preview": [
                    {
                        "symbol": "EARLYDROPUSDT",
                        "reason": "起跌捕捉",
                        "phase": "起跌初段",
                        "change_pct": 1.2,
                        "velocity_pct": -0.2,
                        "price_tick_pct": -0.7,
                        "quote_volume": 1_500_000,
                        "sources": ["okx"],
                    }
                ]
            },
            "paper_exchange": {
                "positions": [
                    {"strategy": "C/v14", "symbol": "EARLYDROPUSDT", "side": "short", "unrealized_pnl": 2.4},
                ]
            },
        })

        self.assertIn("EARLYDROPUSDT", html)
        self.assertIn("起跌捕捉", html)
        self.assertIn("起跌初段", html)
        self.assertIn("起跌 24h +1.20%", html)
        self.assertIn("tick -0.70%", html)
        self.assertIn("C/v14 short", html)
        self.assertIn("顺势", html)

    def test_mover_diagnostics_compacts_strategy_filter(self):
        summary = self.tool.summarize_mover_diagnostics([
            {
                "strategy": "A/v11",
                "event_type": "OPEN_SKIPPED",
                "reason": "15m无确认信号",
                "stage": "confirmation",
                "layer": "strategy",
            },
            {
                "strategy": "C/v14",
                "event_type": "SENTINEL_SCANNED",
                "scan_result": "reject_tail_guard",
                "stage": "tail_guard",
                "layer": "strategy",
            },
        ])

        self.assertEqual("挡：A/v11；未扫：B/v16", summary["strategy_filter"])
        self.assertIn("15分钟确认没有跟上", summary["no_entry_reason"])
        self.assertNotIn("A/v11:", summary["strategy_filter"])

    def test_market_mover_phase_classifier_labels_stage(self):
        self.assertEqual("起涨初段", self.tool.market_mover_phase(1.4, 0.8))
        self.assertEqual("上涨中段加速", self.tool.market_mover_phase(7.5, 1.2))
        self.assertEqual("上涨末段放缓", self.tool.market_mover_phase(16.0, 0.1))
        self.assertEqual("起跌初段", self.tool.market_mover_phase(-2.0, -0.7))

    def test_render_html_has_countdown_and_no_legacy_report_sections(self):
        old_read_first_json = self.tool.read_first_json
        old_queue_summary = self.tool.queue_summary
        old_event_summary = self.tool.event_summary
        old_attention_items = self.tool.attention_items
        try:
            self.tool.read_first_json = lambda *paths: {}
            self.tool.queue_summary = lambda: {"active": 0, "cooldowns": 0, "last": [], "counts": {}}
            self.tool.event_summary = lambda: {"strategies": [], "events": 0}
            self.tool.attention_items = lambda: ({}, [])
            html = self.tool.render_html()
        finally:
            self.tool.read_first_json = old_read_first_json
            self.tool.queue_summary = old_queue_summary
            self.tool.event_summary = old_event_summary
            self.tool.attention_items = old_attention_items

        self.assertIn("refreshCountdown", html)
        self.assertIn("下次自动刷新", html)
        self.assertIn("今日涨跌榜跟踪", html)
        self.assertNotIn("从零运行状态", html)
        self.assertNotIn("服务器清理计划", html)
        self.assertNotIn("小放开闸门", html)
        self.assertNotIn("Binance", html)


if __name__ == "__main__":
    unittest.main()
