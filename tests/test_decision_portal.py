import importlib.util
import json
import sys
import tempfile
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
        self.tool.ATTENTION_JSON = self.attention_json

    def tearDown(self):
        self.tool.ATTENTION_JSON = self.old_attention_json
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
        self.assertIn("A/v11 已上线改动需要复核", html)
        self.assertIn("决定继续观察、收窄，或准备回滚", html)
        self.assertNotIn("EXP-current", html)
        self.assertNotIn("EXP-old", html)
        self.assertNotIn("replacement-quality", html)

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

    def test_strategy_detail_shows_position_pnl_fee_and_funding_caveat(self):
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

        self.assertIn("查看持仓盈亏 / 手续费 / 资金费率", html)
        self.assertIn("ARBUSDT", html)
        self.assertIn("+1.4216", html)
        self.assertIn("估算：按 taker 0.04%", html)
        self.assertIn("待补资金费率流水", html)

    def test_paper_exchange_summary_is_first_class_report_section(self):
        html = self.tool.render_paper_exchange({
            "paper_exchange": {
                "mode": "paper_exchange",
                "total_equity": 300000,
                "total_unrealized_pnl": 12.34,
                "open_positions": 3,
                "by_strategy": {
                    "A/v11": {"positions": 1, "unrealized_pnl": 1.2, "equity": 100001, "realized_pnl": 0, "fees_paid": 0.16, "funding_paid": 0},
                    "B/v16": {"positions": 1, "unrealized_pnl": 2.3, "equity": 100002, "realized_pnl": 0, "fees_paid": 0.16, "funding_paid": 0},
                    "C/v14": {"positions": 1, "unrealized_pnl": 8.84, "equity": 100008, "realized_pnl": 0, "fees_paid": 0.16, "funding_paid": 0},
                },
                "positions": [
                    {
                        "strategy": "A/v11",
                        "symbol": "BTCUSDT",
                        "side": "long",
                        "qty": 0.01,
                        "entry_price": 100,
                        "mark_price": 101,
                        "unrealized_pnl": 1,
                        "notional": 101,
                        "margin": 25.25,
                        "fees_paid": 0.04,
                        "funding_paid": 0,
                        "funding_source": "okx",
                        "mark_source": "okx_15m",
                    }
                ],
            }
        })

        self.assertIn("paper exchange", html)
        self.assertIn("BTCUSDT", html)
        self.assertIn("手续费", html)
        self.assertIn("资金费率", html)


if __name__ == "__main__":
    unittest.main()
