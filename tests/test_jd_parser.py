import unittest
from pathlib import Path

from app.collectors.jd_parser import parse_jd_orders

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = FIXTURES / "jd_orders.html"
HASHED_FIXTURE = FIXTURES / "jd_orders_hashed.html"


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

    def test_hashed_css_module_layout(self):
        """JD 2026 pages use CSS Modules: orderCard-<hash>, orderTime-<hash>, ..."""
        orders = parse_jd_orders(HASHED_FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(len(orders), 2)

        first = orders[0]
        self.assertEqual(first["platform_order_id"], "3596481019513069")
        self.assertEqual(first["title"], "测试商品甲 / 测试商品乙")
        self.assertEqual(first["shop"], "京东自营")
        self.assertAlmostEqual(first["amount"], 69.61)
        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["status_text"], "已完成")
        self.assertEqual(first["order_time"], "2026-08-20 05:18:25")
        self.assertEqual(
            first["product_url"], "https://item.jd.com/2679536.html?pcdk=TOKEN"
        )
        self.assertEqual(first["url"], "")
        self.assertEqual(
            first["image_url"], "https://img13.360buyimg.com/n0/s500x500/test.jpg"
        )
        self.assertEqual(first["raw"]["quantity"], "2")
        self.assertIn("家门口", first["raw"]["logistics"])

        second = orders[1]
        self.assertEqual(second["platform_order_id"], "3589481017467839")
        self.assertEqual(second["title"], "测试商品丙")
        self.assertAlmostEqual(second["amount"], 29.70)
        self.assertEqual(second["status"], "shipped")
        self.assertEqual(second["order_time"], "2026-08-13 13:04:02")
        self.assertEqual(second["tracking_no"], "SF1234567890123")

    def test_jd_status_texts(self):
        """Real JD statuses that are not in the obvious word list."""
        from app.collectors.jd_parser import _status

        self.assertEqual(_status("商品出库")[0], "shipped")
        self.assertEqual(_status("等待收货")[0], "shipped")
        self.assertEqual(_status("付款成功")[0], "processing")
        self.assertEqual(_status("等待发货")[0], "processing")
        self.assertEqual(_status("已取消")[0], "cancelled")

    def test_normalize_mode(self):
        from app.collectors.jd import normalize_mode

        self.assertEqual(normalize_mode("full"), "full")
        self.assertEqual(normalize_mode(" FULL "), "full")
        self.assertEqual(normalize_mode("recent"), "recent")
        self.assertEqual(normalize_mode(None), "recent")
        self.assertEqual(normalize_mode("bogus"), "recent")

    def test_order_from_api(self):
        from app.collectors.jd_parser import order_from_api

        raw = {
            "orderId": "3663481010317077",
            "dateSubmit": "2026-10-26 14:18:43",
            "statusInfo": {"orderState": 9, "orderStateName": "商品出库"},
            "shopInfo": {"shopName": "京东"},
            "itemPayInfo": {
                "shouldPay": 248.04,
                "totalPrice": 358.0,
                "fistPrice": "248.04",
                "paymentTypeName": "在线支付",
            },
            "itemDetail": {
                "itemUrl": "//details.jd.com/normal/item.action?orderid=3663481010317077&PassKey=ABC"
            },
            "wareList": [
                {
                    "name": "测试商品甲",
                    "num": "1",
                    "imgPath": "//img10.360buyimg.com/n0/s500x500_jfs/t1/x.jpg",
                    "wareUrl": "//item.jd.com/100076420072.html",
                },
                {
                    "name": "测试商品乙",
                    "num": "2",
                    "imgPath": "//img10.360buyimg.com/n0/s500x500_jfs/t1/y.jpg",
                    "wareUrl": "//item.jd.com/2.html",
                },
            ],
        }
        order = order_from_api(raw)
        self.assertEqual(order["platform"], "jd")
        self.assertEqual(order["platform_order_id"], "3663481010317077")
        self.assertEqual(order["title"], "测试商品甲 / 测试商品乙")
        self.assertEqual(order["shop"], "京东")
        self.assertAlmostEqual(order["amount"], 248.04)
        self.assertEqual(order["status"], "shipped")
        self.assertEqual(order["status_text"], "商品出库")
        self.assertEqual(order["order_time"], "2026-10-26 14:18:43")
        self.assertEqual(
            order["url"],
            "https://details.jd.com/normal/item.action?orderid=3663481010317077&PassKey=ABC",
        )
        self.assertEqual(order["product_url"], "https://item.jd.com/100076420072.html")
        self.assertEqual(
            order["image_url"], "https://img10.360buyimg.com/n0/s500x500_jfs/t1/x.jpg"
        )
        self.assertEqual(order["raw"]["wareList"][1]["num"], "2")

    def test_empty_page(self):
        self.assertEqual(parse_jd_orders("<html><body>请登录</body></html>"), [])


if __name__ == "__main__":
    unittest.main()
