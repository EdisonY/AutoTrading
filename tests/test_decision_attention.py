import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    path = ROOT / "部署工具" / "decision_attention.py"
    spec = importlib.util.spec_from_file_location("decision_attention_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class DecisionAttentionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def test_report_decision_statuses_count_as_acknowledged(self):
        item = {
            "item_id": "rollback:exp-20260527-v16-atr-stop-bands",
            "priority": "P1",
            "category": "策略回滚",
            "title": "P1 B/v16 回滚观察",
            "evidence": "pnl_after_cost=-87.04; profit_factor=0.79<1.05",
            "source": "test",
        }
        ack = {
            item["item_id"]: {
                "item_id": item["item_id"],
                "status": "narrow_b_v16_requested",
                "fingerprint": self.tool.item_fingerprint(item),
            }
        }

        self.assertTrue(self.tool.is_acknowledged(item, ack))


if __name__ == "__main__":
    unittest.main()
