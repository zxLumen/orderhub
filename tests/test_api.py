import base64
import gzip
import hashlib
import hmac
import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from app import auth, config, db
from app.api import Handler, Server, serve
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = {
            "DB_PATH": config.DB_PATH,
            "TOKEN": config.TOKEN,
            "WEB_DIR": config.WEB_DIR,
            "MAX_BODY": config.MAX_BODY_BYTES,
            "PASSWORD_HASH": config.PASSWORD_HASH,
            "SESSION_SECRET": config.SESSION_SECRET,
            "ADMIN_COOKIE_SECRET": config.ADMIN_COOKIE_SECRET,
            "ADMIN_COOKIE_NAME": config.ADMIN_COOKIE_NAME,
        }
        config.DB_PATH = Path(self.tmp.name) / "test.db"
        config.TOKEN = "testtoken"
        config.PASSWORD_HASH = ""
        config.SESSION_SECRET = ""
        config.ADMIN_COOKIE_SECRET = ""
        config.ADMIN_COOKIE_NAME = "zx_admin"
        config.WEB_DIR = ROOT / "web"
        config.MAX_BODY_BYTES = 2 * 1024 * 1024
        db.init_db(config.DB_PATH)
        self.httpd = Server(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)
        config.DB_PATH = self.old["DB_PATH"]
        config.TOKEN = self.old["TOKEN"]
        config.WEB_DIR = self.old["WEB_DIR"]
        config.MAX_BODY_BYTES = self.old["MAX_BODY"]
        config.PASSWORD_HASH = self.old["PASSWORD_HASH"]
        config.SESSION_SECRET = self.old["SESSION_SECRET"]
        config.ADMIN_COOKIE_SECRET = self.old["ADMIN_COOKIE_SECRET"]
        config.ADMIN_COOKIE_NAME = self.old["ADMIN_COOKIE_NAME"]
        self.tmp.cleanup()

    def request(self, method, path, body=None, token=None, content_type="application/json", extra_headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = dict(extra_headers or {})
        if body is not None:
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode("utf-8")
            elif isinstance(body, str):
                body = body.encode("utf-8")
            headers["Content-Type"] = content_type
        if token:
            headers["Authorization"] = f"Bearer {token}"
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            payload = raw
        return response.status, payload, dict(response.getheaders())

    @staticmethod
    def homepage_cookie(secret, expires_ms=None):
        expiry = int(expires_ms if expires_ms is not None else (time.time() + 3600) * 1000)
        signature = hmac.new(
            secret.encode("utf-8"), str(expiry).encode("ascii"), hashlib.sha256
        ).hexdigest()
        return f"zx_admin={expiry}.{signature}"

    def test_health_config_and_auth(self):
        status, payload, _ = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        status, payload, _ = self.request("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertTrue(payload["auth_required"])
        self.assertEqual(payload["role"], "visitor")
        self.assertFalse(payload["admin"])

        status, payload, _ = self.request("GET", "/api/orders")
        self.assertEqual(status, 200)
        self.assertEqual(payload["items"], [])
        status, payload, _ = self.request("GET", "/api/orders", token="wrong")
        self.assertEqual(status, 200)
        self.assertEqual(payload["items"], [])
        status, payload, _ = self.request("GET", "/api/orders", token="testtoken")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_static_and_path_traversal(self):
        status, _, headers = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        status, _, _ = self.request("GET", "/icons/icon.svg")
        self.assertEqual(status, 200)
        status, _, _ = self.request("GET", "/../.env")
        self.assertEqual(status, 403)

    def test_body_limit(self):
        config.MAX_BODY_BYTES = 10
        status, payload, _ = self.request(
            "POST", "/api/sms", {"text": "x" * 100}, token="testtoken"
        )
        self.assertEqual(status, 413)
        self.assertFalse(payload["ok"])

    def test_bad_requests(self):
        status, _, _ = self.request("POST", "/api/sms", b"{not json", token="testtoken")
        self.assertEqual(status, 400)
        status, _, _ = self.request("POST", "/api/sms", {"text": ""}, token="testtoken")
        self.assertEqual(status, 400)
        status, _, _ = self.request("POST", "/api/orders", {"title": "x"}, token="testtoken")
        self.assertEqual(status, 400)

    def test_sms_shipment_search(self):
        status, payload, _ = self.request(
            "POST",
            "/api/sms",
            {
                "text": "【顺丰速运】您的快件 SF1234567890123 已存放至阳光花园菜鸟驿站，取件码：8-2-1234。",
            },
            token="testtoken",
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["created"])
        self.assertEqual(payload["parsed"]["platform"], "")

        status, payload, _ = self.request("GET", "/api/shipments", token="testtoken")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(payload["items"][0]["pickup_code"], "8-2-1234")

        status, payload, _ = self.request(
            "GET", "/api/search?q=%E8%8F%9C%E9%B8%9F", token="testtoken"
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["items"])

    def test_order_export(self):
        status, payload, _ = self.request(
            "POST",
            "/api/orders",
            {
                "platform": "jd",
                "platform_order_id": "A1",
                "title": "绿联 Type-C 充电线",
                "amount": 29.9,
                "status": "shipped",
                "tracking_no": "SF1234567890123",
                "image_url": "https://img13.360buyimg.com/n0/s500x500/example.jpg",
            },
            token="testtoken",
        )
        self.assertEqual(status, 200)
        status, payload, _ = self.request("GET", "/api/export", token="testtoken")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["data"]["orders"]), 1)
        self.assertEqual(
            payload["data"]["orders"][0]["image_url"],
            "https://img13.360buyimg.com/n0/s500x500/example.jpg",
        )

    def test_password_login_and_session(self):
        config.PASSWORD_HASH = auth.hash_password("correct horse battery")
        config.SESSION_SECRET = "test-session-secret"
        try:
            status, _, _ = self.request("GET", "/")
            self.assertEqual(status, 200)
            status, _, _ = self.request("GET", "/login")
            self.assertEqual(status, 200)

            status, payload, headers = self.request(
                "POST", "/api/login", {"password": "wrong"}
            )
            self.assertEqual(status, 401)
            status, payload, headers = self.request(
                "POST", "/api/login", {"password": "correct horse battery"}
            )
            self.assertEqual(status, 200)
            cookie = headers.get("Set-Cookie", "").split(";", 1)[0]
            self.assertIn("orderhub_session=", cookie)

            status, payload, _ = self.request(
                "GET", "/api/orders", extra_headers={"Cookie": cookie}
            )
            self.assertEqual(status, 200)
            self.assertTrue(payload["ok"])
            status, _, _ = self.request("GET", "/", extra_headers={"Cookie": cookie})
            self.assertEqual(status, 200)
        finally:
            config.PASSWORD_HASH = ""
            config.SESSION_SECRET = ""

    def test_visitor_preview_and_homepage_admin(self):
        secret = "blog-session-secret"
        config.ADMIN_COOKIE_SECRET = secret
        with db.session(config.DB_PATH) as conn:
            first = db.upsert_order(
                conn,
                platform="jd",
                platform_order_id="PREVIEW-1",
                title="公开预览订单",
                shop="测试店",
                amount=99.5,
                status="shipped",
                tracking_no="SF1234567890123",
                logistics_text="北京市朝阳区某小区 13800138000",
                url="https://example.test/order/1",
                source="manual",
            )
            second = db.upsert_order(
                conn,
                platform="taobao",
                platform_order_id="PREVIEW-2",
                title="第二条预览订单",
                amount=12,
                status="completed",
            )
            hidden = db.upsert_order(
                conn,
                platform="pdd",
                platform_order_id="PRIVATE-1",
                title="未开放订单",
                amount=7,
                status="processing",
            )
            conn.commit()

        status, payload, _ = self.request("GET", "/api/orders")
        self.assertEqual(status, 200)
        self.assertEqual(payload["items"], [])
        self.assertTrue(payload["visitor"])

        status, _, _ = self.request("GET", f"/api/orders/{first}")
        self.assertEqual(status, 404)
        status, _, _ = self.request("GET", "/api/stats")
        self.assertEqual(status, 401)

        status, _, _ = self.request(
            "POST", "/api/orders/visibility", {"ids": [first], "visible": True}
        )
        self.assertEqual(status, 401)

        cookie = self.homepage_cookie(secret)
        status, payload, _ = self.request(
            "GET", "/api/config", extra_headers={"Cookie": cookie}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["role"], "admin")
        self.assertEqual(payload["auth_source"], "homepage")

        status, payload, _ = self.request(
            "GET", "/api/orders", extra_headers={"Cookie": cookie}
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["items"]), 3)
        self.assertTrue(any(item["amount"] == 99.5 for item in payload["items"]))

        status, payload, _ = self.request(
            "POST",
            "/api/orders/visibility",
            {"ids": [first, second], "visible": True},
            extra_headers={"Cookie": cookie},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["updated"], 2)

        status, payload, _ = self.request("GET", "/api/orders")
        self.assertEqual(status, 200)
        self.assertEqual({item["id"] for item in payload["items"]}, {first, second})
        for item in payload["items"]:
            for key in (
                "amount",
                "platform_order_id",
                "tracking_no",
                "url",
                "product_url",
                "logistics_text",
                "source",
                "shipments",
            ):
                self.assertNotIn(key, item)
            self.assertTrue(item["visitor_visible"])

        status, payload, _ = self.request("GET", f"/api/orders/{first}")
        self.assertEqual(status, 200)
        item = payload["item"]
        self.assertEqual(item["title"], "公开预览订单")
        self.assertTrue(item["visitor_view"])
        self.assertNotIn("tracking_no", item)
        self.assertNotIn("logistics_text", item)
        self.assertNotIn("amount", item)
        status, _, _ = self.request("GET", f"/api/orders/{hidden}")
        self.assertEqual(status, 404)

        status, payload, _ = self.request(
            "GET", f"/api/orders/{first}", extra_headers={"Cookie": cookie}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["item"]["tracking_no"], "SF1234567890123")
        self.assertEqual(payload["item"]["amount"], 99.5)

        status, payload, _ = self.request(
            "POST",
            "/api/orders/visibility",
            {"ids": [first], "visible": False},
            extra_headers={
                "Cookie": cookie,
                "Origin": "https://evil.example",
            },
        )
        self.assertEqual(status, 403)
        self.assertFalse(payload["ok"])

        expired = self.homepage_cookie(secret, expires_ms=(time.time() - 60) * 1000)
        status, payload, _ = self.request(
            "GET", "/api/config", extra_headers={"Cookie": expired}
        )
        self.assertEqual(payload["role"], "visitor")

        config.ADMIN_COOKIE_SECRET = ""

    def test_collector_queue_and_ingest(self):
        status, _, _ = self.request(
            "POST",
            "/api/collector/heartbeat",
            {"name": "win", "chrome_ok": True, "version": "test"},
            token="testtoken",
        )
        self.assertEqual(status, 200)
        status, payload, _ = self.request("GET", "/api/collector/status", token="testtoken")
        self.assertEqual(status, 200)
        self.assertTrue(payload["online"])

        status, payload, _ = self.request(
            "POST", "/api/collectors/jd/run", {"mode": "recent"}, token="testtoken"
        )
        self.assertEqual(status, 202)
        command_id = payload["command_id"]

        status, payload, _ = self.request(
            "GET", "/api/collector/commands/next?worker=win", token="testtoken"
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["command"]["id"], command_id)
        self.assertEqual(payload["command"]["status"], "running")

        status, payload, _ = self.request(
            "POST",
            "/api/collector/ingest",
            {
                "command_id": command_id,
                "status": "ok",
                "orders": [
                    {"platform": "jd", "platform_order_id": "J1", "title": "测试商品"},
                ],
                "runs": [
                    {
                        "platform": "jd",
                        "status": "ok",
                        "items": 1,
                        "started_at": "2026-10-27T08:00:00+00:00",
                    }
                ],
                "alerts": [{"kind": "test", "title": "测试提醒", "body": "ok"}],
                "worker": {"name": "win", "chrome_ok": True},
            },
            token="testtoken",
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["orders"], 1)
        self.assertEqual(payload["runs"], 1)
        self.assertEqual(payload["alerts"], 1)

        status, payload, _ = self.request("GET", "/api/collector/status", token="testtoken")
        self.assertEqual(status, 200)
        self.assertEqual(payload["commands"][0]["status"], "ok")
        self.assertTrue(payload["runs"])

    def test_collector_shipment_ingest(self):
        status, payload, _ = self.request(
            "POST",
            "/api/orders",
            {"platform": "jd", "platform_order_id": "J-TRACE-1", "title": "测试订单"},
            token="testtoken",
        )
        self.assertEqual(status, 200)
        order_id = payload["id"]
        status, payload, _ = self.request(
            "POST",
            "/api/collector/ingest",
            {
                "orders": [],
                "shipments": [
                    {
                        "tracking_no": "JD0266397396896",
                        "carrier": "京东快递",
                        "platform": "jd",
                        "status": "out_for_delivery",
                        "status_text": "派送中",
                        "last_event_at": "2026-10-27T16:32:15+08:00",
                    }
                ],
                "events": [
                    {
                        "tracking_no": "JD0266397396896",
                        "event_time": "2026-10-27T16:32:15+08:00",
                        "context": "您的订单正在派送中",
                        "status": "out_for_delivery",
                        "source": "jd_detail",
                    }
                ],
                "links": [
                    {
                        "platform": "jd",
                        "platform_order_id": "J-TRACE-1",
                        "tracking_no": "JD0266397396896",
                        "package_index": 0,
                        "source": "jd_detail",
                    }
                ],
            },
            token="testtoken",
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["shipments"], 1)
        self.assertEqual(payload["events"], 1)
        self.assertEqual(payload["links"], 1)
        status, payload, _ = self.request(
            "GET", f"/api/orders/{order_id}", token="testtoken"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["item"]["shipments"]), 1)
        self.assertEqual(payload["item"]["shipments"][0]["tracking_no"], "JD0266397396896")
        self.assertEqual(len(payload["item"]["shipments"][0]["events"]), 1)

    def test_push_subscription(self):
        status, payload, _ = self.request(
            "POST",
            "/api/push/subscribe",
            {
                "subscription": {
                    "endpoint": "https://push.example.test/1",
                    "keys": {"p256dh": "abc", "auth": "def"},
                }
            },
            token="testtoken",
        )
        self.assertEqual(status, 200)
        with db.session(config.DB_PATH) as conn:
            self.assertEqual(len(db.list_push_subscriptions(conn)), 1)
        status, _, _ = self.request(
            "POST",
            "/api/push/unsubscribe",
            {"endpoint": "https://push.example.test/1"},
            token="testtoken",
        )
        self.assertEqual(status, 200)
        with db.session(config.DB_PATH) as conn:
            self.assertEqual(len(db.list_push_subscriptions(conn)), 0)
        status, _, _ = self.request("POST", "/api/push/test", {}, token="testtoken")
        self.assertEqual(status, 400)

    def test_gzip_collector_body(self):
        body = gzip.compress(
            json.dumps(
                {"name": "win", "chrome_ok": True, "version": "test"}
            ).encode("utf-8")
        )
        status, payload, _ = self.request(
            "POST",
            "/api/collector/heartbeat",
            body=body,
            token="testtoken",
            extra_headers={"Content-Encoding": "gzip"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_import_file_then_platforms(self):
        csv_text = (
            "订单号,下单时间,商品名称,店铺名称,订单金额,订单状态,运单号,快递公司\n"
            "TB1,2026-11-01 12:00:00,机械键盘,某店,299.00,已发货,SF1234567890123,顺丰\n"
            "TB2,2026-11-02 12:00:00,显示器支架,某店,159.00,已完成,,\n"
        )
        payload = {
            "filename": "taobao_orders.csv",
            "data": base64.b64encode(csv_text.encode("utf-8")).decode("ascii"),
        }
        status, result, _ = self.request(
            "POST", "/api/import/file", payload, token="testtoken"
        )
        self.assertEqual(status, 200, result)
        self.assertEqual(result["created"], 2)
        self.assertEqual(result["by_platform"]["taobao"], 2)

        status, platforms, _ = self.request("GET", "/api/platforms", token="testtoken")
        self.assertEqual(status, 200)
        taobao = next(item for item in platforms["items"] if item["id"] == "taobao")
        self.assertEqual(taobao["orders"], 2)
        self.assertIn("agent", taobao["channels"])
        self.assertIn("export", taobao["channels"])
        self.assertEqual(taobao["agent_status"], "ready")
        douyin = next(item for item in platforms["items"] if item["id"] == "douyin")
        self.assertEqual(douyin["agent_status"], "ready")
        self.assertIn("xianyu", [item["id"] for item in platforms["items"]])

        status, orders, _ = self.request(
            "GET", "/api/orders?platform=taobao", token="testtoken"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(orders["items"]), 2)
        self.assertEqual(orders["items"][0]["source"], "export")

        status, result, _ = self.request(
            "POST", "/api/import/file", payload, token="testtoken"
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["updated"], 2)

    def test_import_rejects_bad_file(self):
        status, result, _ = self.request(
            "POST",
            "/api/import/file",
            {"filename": "x.txt", "data": base64.b64encode(b"hello").decode("ascii")},
            token="testtoken",
        )
        self.assertEqual(status, 400)
        self.assertFalse(result["ok"])

    def test_tracking_query_endpoint(self):
        result = {
            "ok": True,
            "type": "yuantong",
            "carrier_name": "圆通速递",
            "state": "in_transit",
            "events": [{"time": "2026-11-10 09:00:00", "context": "已到达北京转运中心", "status": ""}],
        }
        with patch("app.services.ingest.kuaidi_web.query", return_value=result):
            status, payload, _ = self.request(
                "POST",
                "/api/tracking/query",
                {"tracking_no": "YT1234567890123"},
                token="testtoken",
            )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["state"], "in_transit")
        status, payload, _ = self.request(
            "GET", "/api/shipments/YT1234567890123", token="testtoken"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["item"]["events"]), 1)

    def test_sms_bulk_endpoint(self):
        status, payload, _ = self.request(
            "POST",
            "/api/sms/bulk",
            {
                "texts": [
                    "【拼多多】您的包裹已存放至阳光花园菜鸟驿站，取件码 8-2-1234。",
                    "【顺丰速运】您的快件 SF1234567890123 已签收。",
                ]
            },
            token="testtoken",
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["received"], 2)
        self.assertEqual(payload["created"], 2)

    def test_collector_platform_queue(self):
        status, payload, _ = self.request(
            "POST", "/api/collectors/taobao/run", {"mode": "recent"}, token="testtoken"
        )
        self.assertEqual(status, 202, payload)
        self.assertTrue(payload["queued"])
        self.assertEqual(payload["platform"], "taobao")
        command_id = payload["command_id"]

        status, payload, _ = self.request(
            "GET", "/api/collector/commands/next?worker=win", token="testtoken"
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["command"]["id"], command_id)
        self.assertEqual(payload["command"]["platform"], "taobao")

        status, payload, _ = self.request(
            "POST", "/api/collectors/meituan/run", {}, token="testtoken"
        )
        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])

    def test_web_collector_requires_new_agent(self):
        self.request(
            "POST",
            "/api/collector/heartbeat",
            {"name": "win", "chrome_ok": True, "version": "0.3.0"},
            token="testtoken",
        )
        status, payload, _ = self.request(
            "POST", "/api/collectors/taobao/run", {}, token="testtoken"
        )
        self.assertEqual(status, 409)
        self.assertIn("v0.4.0", payload["error"])
        # JD keeps working with the old agent.
        status, payload, _ = self.request(
            "POST", "/api/collectors/jd/run", {}, token="testtoken"
        )
        self.assertEqual(status, 202)

    def test_refuses_public_bind_without_token(self):
        old_host, old_token = config.HOST, config.TOKEN
        config.HOST, config.TOKEN = "0.0.0.0", ""
        try:
            with patch("app.api.db.init_db"):
                with self.assertRaises(SystemExit):
                    serve()
        finally:
            config.HOST, config.TOKEN = old_host, old_token

    def test_options(self):
        status, _, headers = self.request("OPTIONS", "/api/sms")
        self.assertEqual(status, 204)
        self.assertEqual(headers.get("Allow"), "GET, POST, OPTIONS")


if __name__ == "__main__":
    unittest.main()
