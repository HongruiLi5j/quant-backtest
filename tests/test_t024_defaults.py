"""T024：预算上限放宽到 1200，但调度器默认预算保持 600 不变。"""
import inspect
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.market_monitor import scheduler  # noqa: E402


class BudgetDefaultTests(unittest.TestCase):
    def test_default_stays_600_max_is_1200(self):
        self.assertEqual(scheduler.DEFAULT_BUDGET, 600)
        self.assertEqual(scheduler.MAX_BUDGET, 1200)
        sig = inspect.signature(scheduler.MonitorScheduler.__init__)
        self.assertEqual(sig.parameters["request_budget"].default, 600)


if __name__ == "__main__":
    unittest.main()
