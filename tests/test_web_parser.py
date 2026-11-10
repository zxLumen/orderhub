import unittest

from app.collectors.web import DEFAULT_URLS, page_state
from app.collectors.web_parser import (
    extract_embedded_payloads,
    extract_orders,
    parse_html_orders,
    parse_order_text,
    parse_pdd_payload,
    parse_xianyu_html,
)


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


class PddParserTest(unittest.TestCase):
    PAYLOAD = {
        "ordersStore": {
            "orders": [
                {
                    "orderSn": "261001-277390354763200",
                    "orderAmount": "27.89",
                    "displayAmount": 2789,
                    "orderTime": 1790837383,
                    "status": 4,
                    "orderStatusPrompt": "交易成功",
                    "trackingNumber": "79146268267530",
                    "orderLinkUrl": "order.html?order_sn=261001-277390354763200",
                    "mall": {"mallName": "启智毛毡"},
                    "orderGoods": [
                        {
                            "goodsName": "便携式1000块加厚毛毡拼图板",
                            "thumbUrl": "https://img.pddpic.com/example.jpeg",
                        }
                    ],
                },
                {
                    "orderSn": "260711-596388135243200",
                    "orderAmount": "9.88",
                    "orderTime": 1783758527,
                    "status": 4,
                    "orderStatusPrompt": "拼过2次",
                    "mall": {"mallName": "珍辰模型"},
                    "orderGoods": [{"goodsName": "清仓模型零件收纳架"}],
                },
            ]
        }
    }

    def test_parse_orders(self):
        orders = parse_pdd_payload(self.PAYLOAD)
        self.assertEqual(len(orders), 2)

        first = orders[0]
        self.assertEqual(first["platform"], "pdd")
        self.assertEqual(first["platform_order_id"], "261001-277390354763200")
        self.assertEqual(first["title"], "便携式1000块加厚毛毡拼图板")
        self.assertEqual(first["shop"], "启智毛毡")
        self.assertAlmostEqual(first["amount"], 27.89)
        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["status_text"], "交易成功")
        self.assertIsNotNone(first["order_time"])
        self.assertEqual(first["tracking_no"], "79146268267530")
        self.assertTrue(first["url"].startswith("https://mobile.yangkeduo.com/order.html"))
        self.assertEqual(first["image_url"], "https://img.pddpic.com/example.jpeg")
        self.assertEqual(first["raw"]["order"]["orderSn"], "261001-277390354763200")

    def test_group_prompt_falls_back_to_status_code(self):
        orders = parse_pdd_payload(self.PAYLOAD)
        second = orders[1]
        self.assertEqual(second["status"], "completed")

    def test_extract_orders_dispatch(self):
        orders = extract_orders(self.PAYLOAD, "pdd")
        self.assertEqual(len(orders), 2)

    def test_embedded_raw_data(self):
        html = (
            "<html><script>window.rawData="
            '{"ordersStore":{"orders":[{"orderSn":"260101-000000000000001",'
            '"orderAmount":"1.00","status":4}]}};'
            "</script></html>"
        )
        payloads = extract_embedded_payloads(html, "pdd")
        self.assertEqual(len(payloads), 1)
        orders = extract_orders(payloads[0], "pdd")
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["platform_order_id"], "260101-000000000000001")


class XianyuHtmlTest(unittest.TestCase):
    HTML = """
    <div class="container--card">
      <div class="container--head">
        <a class="left--seller" href="https://www.goofish.com/personal?userId=1">
          <div class="name--seller"><span>卖家A</span></div>
        </a>
        <div class="status--state"><span>交易成功</span></div>
      </div>
      <div class="container--main">
        <div class="picture--pic"><img src="//img.alicdn.com/item.jpg"></div>
        <a class="info--link" href="https://www.goofish.com/order-detail?orderId=5127877909065017624">
          <div class="desc--d"><div class="name--product">测试商品</div></div>
          <div class="price--p"><span>¥</span><span>930</span><span>.00</span></div>
        </a>
      </div>
    </div>
    """

    def test_parse_cards(self):
        orders = parse_xianyu_html(self.HTML)
        self.assertEqual(len(orders), 1)
        order = orders[0]
        self.assertEqual(order["platform"], "xianyu")
        self.assertEqual(order["platform_order_id"], "5127877909065017624")
        self.assertEqual(order["title"], "测试商品")
        self.assertEqual(order["shop"], "卖家A")
        self.assertAlmostEqual(order["amount"], 930.0)
        self.assertEqual(order["status"], "completed")
        self.assertEqual(order["status_text"], "交易成功")
        self.assertEqual(order["url"], "https://www.goofish.com/order-detail?orderId=5127877909065017624")
        self.assertEqual(order["image_url"], "https://img.alicdn.com/item.jpg")

    def test_html_dispatch(self):
        self.assertEqual(len(parse_html_orders(self.HTML, "xianyu")), 1)
        self.assertEqual(parse_html_orders(self.HTML, "taobao"), [])


if __name__ == "__main__":
    unittest.main()
