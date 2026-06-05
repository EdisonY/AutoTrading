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


if __name__ == "__main__":
    unittest.main()
