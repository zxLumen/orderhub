import unittest
from pathlib import Path

from app.collectors.jd_parser import parse_jd_orders

FIXTURE = Path(__file__).parent / "fixtures" / "jd_orders.html"


class JdParserTest(unittest.TestCase):
    def test_parse_fixture(self):
        orders = parse_jd_orders(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(len(orders), 2)
        first = orders[0]
        self.assertEqual(first["platform"], "jd")
        self.assertEqual(first["platform_order_id"], "29876543210")
        self.assertIn("iPhone 15", first["title"])
        self.assertEqual(first["shop"], "京东自营")
        self.assertEqual(first["amount"], 5999.0)
        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["tracking_no"], "JD0123456789012")
        self.assertEqual(first["order_time"], "2026-10-01 12:30:00")

        second = orders[1]
        self.assertEqual(second["platform_order_id"], "29876543211")
        self.assertIn("充电线", second["title"])
        self.assertEqual(second["status"], "shipped")
        self.assertEqual(second["tracking_no"], "SF1234567890123")

    def test_empty_page(self):
        self.assertEqual(parse_jd_orders("<html><body>请登录</body></html>"), [])


if __name__ == "__main__":
    unittest.main()
