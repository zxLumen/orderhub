import unittest

from app.collectors.web import DEFAULT_URLS, page_state
from app.collectors.web_parser import extract_orders, parse_order_text


class TaobaoParserTest(unittest.TestCase):
    def test_main_orders(self):
        payload = {
            "code": 0,
            "mainOrders": [
                {
                    "orderId": "4053256789012345678",
                    "statusInfo": {"text": "卖家已发货"},
                    "payInfo": {"actualFee": "299.00"},
                    "orderInfo": {
                        "createTime": "2026-11-01 12:00:00",
                        "detailUrl": "https://trade.taobao.com/detail?bizOrderId=4053256789012345678",
                    },
                    "seller": {"shopName": "某某数码旗舰店"},
                    "subOrders": [
                        {
                            "itemInfo": {
                                "title": "机械键盘 87 键",
                                "pic": "https://img.alicdn.com/keyboard.jpg",
                                "itemUrl": "https://item.taobao.com/item.htm?id=1",
                            }
                        }
                    ],
                }
            ],
        }
        orders = extract_orders(payload, "taobao")
        self.assertEqual(len(orders), 1)
        order = orders[0]
        self.assertEqual(order["platform"], "taobao")
        self.assertEqual(order["platform_order_id"], "4053256789012345678")
        self.assertEqual(order["title"], "机械键盘 87 键")
        self.assertEqual(order["shop"], "某某数码旗舰店")
        self.assertEqual(order["amount"], 299.0)
        self.assertEqual(order["status"], "shipped")
        self.assertEqual(order["order_time"], "2026-11-01T04:00:00+00:00")
        self.assertIn("img.alicdn.com", order["image_url"])


class GenericPayloadTest(unittest.TestCase):
    def test_pdd_cents_and_tracking(self):
        payload = {
            "orders": [
                {
                    "orderSn": "251106-123456789012345",
                    "orderAmount": 3990,
                    "goodsName": "抽纸 24 包",
                    "mallName": "拼多多平台商户",
                    "orderStatus": "已签收",
                    "createTime": 1793505600,
                    "trackingNo": "YT1234567890123",
                }
            ]
        }
        orders = extract_orders(payload, "pdd")
        self.assertEqual(len(orders), 1)
        order = orders[0]
        self.assertEqual(order["amount"], 39.9)
        self.assertEqual(order["status"], "completed")
        self.assertEqual(order["tracking_no"], "YT1234567890123")
        self.assertEqual(order["order_time"], "2026-11-01T04:00:00+00:00")

    def test_douyin_nested_item(self):
        payload = {
            "order_list": [
                {
                    "orderId": "6921234567890123456",
                    "orderAmount": 5900,
                    "item": {"productName": "洗衣液 3kg"},
                    "shopName": "抖音超市",
                    "orderStatus": "待收货",
                    "createTime": "2026-11-01 12:00:00",
                }
            ]
        }
        orders = extract_orders(payload, "douyin")
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["title"], "洗衣液 3kg")
        self.assertEqual(orders[0]["amount"], 59.0)
        self.assertEqual(orders[0]["status"], "shipped")

    def test_xianyu_mtop(self):
        payload = {
            "data": {
                "orderList": [
                    {
                        "bizOrderId": "2511060012345678901",
                        "itemTitle": "二手相机",
                        "price": "1899.00",
                        "orderStatus": "交易成功",
                        "createTime": "2026-11-02 12:00:00",
                        "sellerNick": "闲鱼卖家",
                        "picUrl": "https://img.alicdn.com/camera.jpg",
                    }
                ]
            }
        }
        orders = extract_orders(payload, "xianyu")
        self.assertEqual(len(orders), 1)
        order = orders[0]
        self.assertEqual(order["amount"], 1899.0)
        self.assertEqual(order["shop"], "闲鱼卖家")
        self.assertEqual(order["status"], "completed")

    def test_zhuanzhuan(self):
        payload = {
            "data": [
                {
                    "orderId": "ZZ202611020002",
                    "productTitle": "二手相机",
                    "orderAmount": "1899.00",
                    "orderStatus": "已完成",
                    "createTime": "2026-11-02 12:00:00",
                    "storeName": "转转严选",
                }
            ]
        }
        orders = extract_orders(payload, "zhuanzhuan")
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["amount"], 1899.0)
        self.assertEqual(orders[0]["status"], "completed")

    def test_unknown_shapes_are_ignored(self):
        payload = {"data": {"items": [{"id": 1, "name": "item"}, {"foo": "bar"}]}}
        self.assertEqual(extract_orders(payload, "pdd"), [])

    def test_dedupes_same_order(self):
        payload = {
            "orders": [
                {"orderSn": "A123456789", "goodsName": "商品一", "orderAmount": 1000},
                {"orderSn": "A123456789", "goodsName": "商品一", "orderAmount": 1000, "orderStatus": "已发货"},
            ]
        }
        orders = extract_orders(payload, "pdd")
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["status"], "shipped")


class TextFallbackTest(unittest.TestCase):
    def test_text_block(self):
        text = """我的订单
订单号：4053256789012345678
机械键盘 87 键
实付款 ¥299.00
2026-11-01 12:00:00
待收货
"""
        orders = parse_order_text(text, "taobao")
        self.assertEqual(len(orders), 1)
        order = orders[0]
        self.assertEqual(order["platform_order_id"], "4053256789012345678")
        self.assertEqual(order["amount"], 299.0)
        self.assertEqual(order["status"], "shipped")
        self.assertTrue(order["order_time"])

    def test_requires_order_id(self):
        self.assertEqual(parse_order_text("机械键盘 ¥299.00 待收货", "taobao"), [])


class PageStateTest(unittest.TestCase):
    def test_login_redirect(self):
        self.assertEqual(
            page_state("https://login.taobao.com/member/login.jhtml", "淘宝网登录", "扫码登录"),
            "login_required",
        )

    def test_pdd_login_path(self):
        self.assertEqual(
            page_state("https://mobile.yangkeduo.com/login.html", "拼多多", "手机号登录"),
            "login_required",
        )

    def test_captcha(self):
        self.assertEqual(
            page_state("https://buyertrade.taobao.com/trade/itemlist/list_bought_items.htm", "淘宝", "请拖动滑块完成验证"),
            "captcha",
        )

    def test_order_page(self):
        self.assertEqual(
            page_state(
                "https://buyertrade.taobao.com/trade/itemlist/list_bought_items.htm",
                "已买到的宝贝",
                "全部订单 订单号 4053 待收货",
            ),
            "ok",
        )

    def test_invalid_page(self):
        self.assertEqual(page_state("https://m.zhuanzhuan.com/u/order/list", "404 Not Found", "页面不存在"), "invalid_page")

    def test_default_urls_present(self):
        self.assertEqual(set(DEFAULT_URLS), {"taobao", "pdd", "douyin", "xianyu", "zhuanzhuan"})
        self.assertTrue(all(DEFAULT_URLS.values()))


if __name__ == "__main__":
    unittest.main()
