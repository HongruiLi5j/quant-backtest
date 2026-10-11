"""会员 + 模拟交易的单元测试（只用标准库）：python3 tests/test_members.py"""
import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import members as m

Q = {"price": 10.0, "name": "X", "day": "2026-10-09", "fee_bp": 8, "lot": 100}


class T(unittest.TestCase):
    def setUp(self):
        m.set_db(Path(tempfile.mkdtemp()) / "t.db")
        self.u = m.register("tester", "password123")["id"]

    def test_auth(self):
        with self.assertRaises(m.MemberError): m.register("tester", "password123")
        with self.assertRaises(m.MemberError): m.register("ab", "password123")
        with self.assertRaises(m.MemberError): m.register("another", "short")
        _, tok = m.login("TESTER", "password123")
        self.assertEqual(m.user_for(tok)["name"], "tester")
        with self.assertRaises(m.MemberError): m.login("tester", "wrongpass1")
        m.set_disabled(self.u, True)
        self.assertIsNone(m.user_for(tok))

    def test_trade_rules(self):
        m.order(self.u, "cn", "600000", "buy", 1000, Q)
        with self.assertRaises(m.MemberError): m.order(self.u, "cn", "600000", "sell", 100, Q)       # T+1
        with self.assertRaises(m.MemberError): m.order(self.u, "cn", "600000", "buy", 150, Q)        # 整手
        with self.assertRaises(m.MemberError): m.order(self.u, "cn", "600000", "buy", 10 ** 7, Q)    # 现金不够
        q2 = dict(Q, day="2026-10-10", price=12.0)
        r = m.order(self.u, "cn", "600000", "sell", 500, q2)
        self.assertAlmostEqual(r["pnl"], 12 * 500 - r["fee"] - (10 * 1000 + 8 * 1000 * 10 / 1e4 * 1) / 1000 * 500, places=2)
        p = m.portfolio(self.u, "cn", lambda c: 12.0, "2026-10-10")
        self.assertEqual(p["positions"][0]["qty"], 500)

    def test_reset_and_delete(self):
        m.order(self.u, "us", "AAPL", "buy", 3, dict(Q, lot=1))
        m.reset_account(self.u, "us")
        self.assertEqual(m.portfolio(self.u, "us", lambda c: 1, "d")["cash"], 100000.0)
        m.delete_user(self.u)
        self.assertEqual(m.list_users(), [])


if __name__ == "__main__":
    unittest.main()
