import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, db
from app.collectors.jd import _save_trace
from app.collectors.jd_trace import parse_jd_trace
from app.services.tracking import extract

FIXTURE = Path(__file__).parent / "fixtures" / "jd_trace.json"


class JdTraceTest(unittest.TestCase):
    def test_trace(self):
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        packages = parse_jd_trace(payload, "3664481010490017")
        self.assertEqual(len(packages), 1)
        package = packages[0]
        self.assertEqual(package["tracking_no"], "JD0266397396896")
        self.assertEqual(package["carrier"], "京东快递")
        self.assertEqual(len(package["events"]), 13)
        self.assertEqual(package["status"], "out_for_delivery")
        self.assertEqual(package["status_text"], "派送中")
        self.assertEqual(package["courier"]["courierName"], "测试配送员")

    def test_save_trace_links_order(self):
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "trace.db"
            with patch.object(config, "DB_PATH", db_path):
                db.init_db()
                with db.session() as conn:
                    order_id = db.upsert_order(
                        conn,
                        platform="jd",
                        platform_order_id="3664481010490017",
                        title="测试订单",
                    )
                count = _save_trace(order_id, "3664481010490017", payload)
                with db.session() as conn:
                    order = db.get_order(conn, order_id)
                    self.assertEqual(count, 1)
                    self.assertEqual(order["tracking_no"], "JD0266397396896")
                    self.assertEqual(len(order["shipments"]), 1)
                    self.assertEqual(order["shipments"][0]["status"], "out_for_delivery")
                    self.assertGreaterEqual(len(order["shipments"][0]["events"]), 10)
                    self.assertTrue(db.get_shipment(conn, "JD0266397396896")["events"])

    def test_tracking_from_logistics_text(self):
        cases = [
            ("您的订单由第三方卖家拣货完成，待出库交付顺丰快递，运单号为SF0221076857166", "sf"),
            ("待出库交付圆通快递，运单号为YT3761133381092", "yto"),
            ("交付京东快递，运单号为JDVB55854561881", "jd"),
            ("交付中通快递，运单号为75624693934723", "zto"),
        ]
        for text, carrier in cases:
            number, code, _name = extract(text)
            self.assertTrue(number)
            self.assertEqual(code, carrier, text)

    def test_no_tracking_for_delivered_text(self):
        number, code, _name = extract("您的订单已送达，感谢您在京东购物。")
        self.assertEqual(number, "")
        self.assertEqual(code, "")


if __name__ == "__main__":
    unittest.main()
