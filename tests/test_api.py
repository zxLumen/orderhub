import gzip
import http.client
import json
import tempfile
import threading
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
        }
        config.DB_PATH = Path(self.tmp.name) / "test.db"
        config.TOKEN = "testtoken"
        config.PASSWORD_HASH = ""
        config.SESSION_SECRET = ""
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

    def test_health_config_and_auth(self):
        status, payload, _ = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        status, payload, _ = self.request("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertTrue(payload["auth_required"])

        status, _, _ = self.request("GET", "/api/orders")
        self.assertEqual(status, 401)
        status, _, _ = self.request("GET", "/api/orders", token="wrong")
        self.assertEqual(status, 401)
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
            self.assertEqual(status, 302)
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
