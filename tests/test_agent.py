import unittest

from app.collector_agent import Agent


class LoginDetectionTest(unittest.TestCase):
    def test_logged_in_order_page(self):
        pages = [
            {
                "type": "page",
                "url": "https://order.jd.com/center/list.action?page=1",
                "title": "订单列表-京东商城",
            },
        ]
        self.assertTrue(Agent._order_page_open(pages))

    def test_passport_url_is_not_logged_in(self):
        pages = [
            {
                "type": "page",
                "url": (
                    "https://passport.jd.com/new/login.aspx"
                    "?ReturnUrl=https%3A%2F%2Forder.jd.com%2Fcenter%2Flist.action"
                ),
                "title": "京东-欢迎登录",
            },
        ]
        self.assertFalse(Agent._order_page_open(pages))

    def test_order_url_before_redirect_is_not_logged_in(self):
        pages = [
            {
                "type": "page",
                "url": "https://order.jd.com/center/list.action",
                "title": "京东-欢迎登录",
            },
        ]
        self.assertFalse(Agent._order_page_open(pages))

    def test_other_pages_are_ignored(self):
        pages = [
            {"type": "page", "url": "chrome://newtab/", "title": "新标签页"},
            {
                "type": "iframe",
                "url": "https://order.jd.com/center/list.action",
                "title": "订单列表-京东商城",
            },
        ]
        self.assertFalse(Agent._order_page_open(pages))

    def test_empty(self):
        self.assertFalse(Agent._order_page_open([]))


if __name__ == "__main__":
    unittest.main()
