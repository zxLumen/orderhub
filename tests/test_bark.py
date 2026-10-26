import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, db
from app.services import bark, ingest


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b"{}"


class BarkTest(unittest.TestCase):
    def test_payload(self):
        old = config.BARK_URL
        config.BARK_URL = "https://api.day.app/test-key"
        try:
            with patch("urllib.request.urlopen", return_value=FakeResponse()) as mocked:
                self.assertTrue(bark.send("标题", "内容", url="https://example.com"))
                request = mocked.call_args[0][0]
                payload = json.loads(request.data.decode("utf-8"))
                self.assertEqual(payload["title"], "标题")
                self.assertEqual(payload["body"], "内容")
                self.assertEqual(payload["url"], "https://example.com")
                self.assertEqual(payload["group"], "OrderHub")
        finally:
            config.BARK_URL = old

    def test_not_configured(self):
        old = config.BARK_URL
        config.BARK_URL = ""
        try:
            self.assertFalse(bark.send("标题", "内容"))
        finally:
            config.BARK_URL = old

    def test_ingest_calls_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            db.init_db(path)
            old_url = config.BARK_URL
            config.BARK_URL = "https://api.day.app/test-key"
            try:
                with db.session(path) as conn:
                    with patch("app.services.ingest.threading.Thread") as thread_cls, patch(
                        "app.services.ingest.bark.send"
                    ) as send:
                        thread_instance = thread_cls.return_value
                        thread_instance.start.side_effect = lambda: thread_cls.call_args[1][
                            "target"
                        ](*thread_cls.call_args[1]["args"], **thread_cls.call_args[1]["kwargs"])
                        ingest.ingest_sms(
                            conn,
                            text="【顺丰速运】您的快件 SF1234567890123 已存放至驿站，取件码 1234。",
                        )
                        send.assert_called_once()
            finally:
                config.BARK_URL = old_url


if __name__ == "__main__":
    unittest.main()
