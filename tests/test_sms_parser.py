import unittest

from app.parsers.sms import parse_sms


class SmsParserTest(unittest.TestCase):
    def test_sf_pickup(self):
        parsed = parse_sms(
            "【顺丰速运】您的快件 SF1234567890123 已存放至 阳光花园菜鸟驿站，"
            "取件码：8-2-1234，请及时取件。"
        )
        self.assertFalse(parsed.ignored)
        self.assertEqual(parsed.carrier, "sf")
        self.assertEqual(parsed.tracking_no, "SF1234567890123")
        self.assertEqual(parsed.status, "ready_for_pickup")
        self.assertEqual(parsed.pickup_code, "8-2-1234")
        self.assertIn("菜鸟驿站", parsed.location)
        self.assertEqual(parsed.platform, "")

    def test_cainiao_is_logistics_not_platform(self):
        parsed = parse_sms(
            "【菜鸟驿站】您的圆通包裹已到菜鸟驿站，取件码 5-1-2088，请凭取件码取件。"
        )
        self.assertEqual(parsed.platform, "")
        self.assertEqual(parsed.carrier, "yto")
        self.assertEqual(parsed.status, "ready_for_pickup")
        self.assertEqual(parsed.pickup_code, "5-1-2088")

    def test_sender_brand_wins_over_location(self):
        parsed = parse_sms(
            "您的快件 SF1234567890123 已存放至菜鸟驿站，取件码 1234。",
            sender="京东",
        )
        self.assertEqual(parsed.platform, "jd")

    def test_taobao_brand(self):
        parsed = parse_sms("【淘宝】您的订单已发货，运单号 YT9876543210123。")
        self.assertEqual(parsed.platform, "taobao")

    def test_jd_shipped(self):
        parsed = parse_sms(
            "【京东】您的订单已发货，京东物流单号 JD0123456789012，预计明天送达。"
        )
        self.assertEqual(parsed.platform, "jd")
        self.assertEqual(parsed.carrier, "jd")
        self.assertEqual(parsed.tracking_no, "JD0123456789012")
        self.assertEqual(parsed.status, "in_transit")

    def test_jd_logistics_is_not_platform(self):
        parsed = parse_sms("【京东物流】您的快件 JD0123456789012 正在派送中。")
        self.assertEqual(parsed.platform, "")
        self.assertEqual(parsed.carrier, "jd")

    def test_douyin(self):
        parsed = parse_sms(
            "【抖音】您的订单已发货，快递单号：YT9876543210123，点击查看物流详情。"
        )
        self.assertEqual(parsed.platform, "douyin")
        self.assertEqual(parsed.carrier, "yto")
        self.assertEqual(parsed.tracking_no, "YT9876543210123")
        self.assertEqual(parsed.status, "in_transit")

    def test_locker_is_pickup_not_delivered(self):
        parsed = parse_sms("【丰巢】您的包裹已放入丰巢快递柜，取件码 123456。")
        self.assertEqual(parsed.status, "ready_for_pickup")
        self.assertEqual(parsed.pickup_code, "123456")

    def test_meituan_delivered(self):
        parsed = parse_sms("【美团外卖】您的订单已送达，感谢您的耐心等待。")
        self.assertEqual(parsed.platform, "meituan")
        self.assertEqual(parsed.status, "delivered")
        self.assertEqual(parsed.tracking_no, "")

    def test_meituan_delivering(self):
        parsed = parse_sms("【美团外卖】骑手已取餐，正在配送中，预计 12:35 送达。")
        self.assertEqual(parsed.platform, "meituan")
        self.assertEqual(parsed.status, "delivering")

    def test_otp_is_ignored(self):
        parsed = parse_sms("【京东】验证码 123456，5分钟内有效，请勿泄露。")
        self.assertTrue(parsed.ignored)
        self.assertEqual(parsed.tracking_no, "")
        self.assertFalse(parsed.is_logistics)

    def test_phone_number_is_not_tracking(self):
        parsed = parse_sms("快递员电话 13800138000，正在派送中，请保持电话畅通。")
        self.assertNotEqual(parsed.tracking_no, "13800138000")
        self.assertEqual(parsed.status, "out_for_delivery")

    def test_order_number_is_not_tracking(self):
        parsed = parse_sms(
            "【京东】订单号：123456789012 已发货，运单号：JD0123456789012。"
        )
        self.assertEqual(parsed.order_id, "123456789012")
        self.assertEqual(parsed.tracking_no, "JD0123456789012")

    def test_numeric_tracking_requires_carrier_context(self):
        parsed = parse_sms("【中通快递】78123456789012 已到阳光花园驿站，请及时取件。")
        self.assertEqual(parsed.tracking_no, "78123456789012")
        self.assertEqual(parsed.carrier, "zto")

    def test_bare_numeric_order_id_is_not_tracking(self):
        parsed = parse_sms(
            "您的订单 123456789012345 已发货，运单号 YT9876543210123，请注意查收。"
        )
        self.assertEqual(parsed.tracking_no, "YT9876543210123")
        self.assertNotIn("123456789012345", parsed.tracking_numbers)

    def test_order_id_after_phone_is_not_tracking(self):
        parsed = parse_sms(
            "快递员电话 13800138000，订单号 123456789012，正在派送中。"
        )
        self.assertEqual(parsed.tracking_no, "")
        self.assertEqual(parsed.status, "out_for_delivery")

    def test_multiple_tracking_numbers(self):
        parsed = parse_sms(
            "您的包裹已发出，运单号 SF1234567890123，另一件 YT9876543210123，请注意查收。"
        )
        self.assertIn("SF1234567890123", parsed.tracking_numbers)
        self.assertIn("YT9876543210123", parsed.tracking_numbers)

    def test_empty(self):
        self.assertTrue(parse_sms("").ignored)


if __name__ == "__main__":
    unittest.main()
