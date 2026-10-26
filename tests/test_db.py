import tempfile
import unittest
from pathlib import Path

from app import db
from app.services.ingest import ingest_capture, ingest_sms


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "test.db"
        db.init_db(self.path)
        self.conn = db.connect(self.path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_naive_time_is_china_time(self):
        self.assertEqual(
            db.normalize_time("2026-10-26 12:00:00"),
            "2026-10-26T04:00:00+00:00",
        )

    def test_order_upsert_and_search(self):
        order_id = db.upsert_order(
            self.conn,
            platform="jd",
            platform_order_id="29876543211",
            title="绿联 Type-C 充电线 1米 快充",
            shop="绿联京东自营旗舰店",
            amount=29.9,
            status="shipped",
            status_text="运输中",
            order_time="2026-10-20T09:12:00+00:00",
        )
        self.conn.commit()
        self.assertGreater(order_id, 0)
        results = db.search(self.conn, "充电线")
        self.assertTrue(results)
        self.assertEqual(results[0]["doc_type"], "order")

        # Upsert must not duplicate.
        db.upsert_order(
            self.conn,
            platform="jd",
            platform_order_id="29876543211",
            title="绿联 Type-C 充电线 1米 快充",
            amount=29.9,
        )
        self.conn.commit()
        self.assertEqual(db.stats(self.conn)["orders"], 1)

    def test_sms_ingest_idempotent(self):
        text = "【顺丰速运】您的快件 SF1234567890123 已存放至阳光花园菜鸟驿站，取件码：8-2-1234。"
        result = ingest_sms(self.conn, text=text)
        self.conn.commit()
        self.assertTrue(result["created"])
        self.assertEqual(result["parsed"]["tracking_no"], "SF1234567890123")
        self.assertEqual(db.stats(self.conn)["active_shipments"], 1)

        second = ingest_sms(self.conn, text=text)
        self.conn.commit()
        self.assertFalse(second["created"])
        self.assertEqual(db.stats(self.conn)["sms"], 1)

    def test_stale_event_does_not_downgrade(self):
        db.upsert_shipment(
            self.conn,
            tracking_no="SF1",
            carrier="sf",
            status="delivered",
            last_event_at="2026-10-26T10:00:00+00:00",
        )
        _, changed = db.upsert_shipment(
            self.conn,
            tracking_no="SF1",
            carrier="sf",
            status="in_transit",
            last_event_at="2026-10-26T09:00:00+00:00",
        )
        self.conn.commit()
        self.assertFalse(changed)
        row = db.get_shipment(self.conn, "SF1")
        self.assertEqual(row["status"], "delivered")

    def test_newer_event_can_correct_status(self):
        db.upsert_shipment(
            self.conn,
            tracking_no="SF2",
            carrier="sf",
            status="returned",
            last_event_at="2026-10-26T09:00:00+00:00",
        )
        _, changed = db.upsert_shipment(
            self.conn,
            tracking_no="SF2",
            carrier="sf",
            status="delivered",
            last_event_at="2026-10-26T10:00:00+00:00",
        )
        self.conn.commit()
        self.assertTrue(changed)
        self.assertEqual(db.get_shipment(self.conn, "SF2")["status"], "delivered")

    def test_search_does_not_match_single_character_noise(self):
        db.upsert_order(
            self.conn,
            platform="jd",
            platform_order_id="A1",
            title="绿联 Type-C 充电线 1米",
        )
        db.record_capture(
            self.conn,
            platform="douyin",
            text="抖音商城 订单 无线蓝牙耳机",
        )
        self.conn.commit()
        results = db.search(self.conn, "充电线")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["doc_type"], "order")

    def test_search_falls_back_without_fts5(self):
        db.upsert_order(
            self.conn,
            platform="taobao",
            platform_order_id="B1",
            title="小米移动电源 20000mAh",
        )
        self.conn.commit()
        self.conn.execute("DROP TABLE search_fts")
        self.conn.commit()
        results = db.search(self.conn, "移动电源")
        self.assertTrue(results)
        self.assertEqual(results[0]["doc_type"], "order")

    def test_capture_search(self):
        result = ingest_capture(
            self.conn,
            platform="douyin",
            text="抖音商城 订单 无线蓝牙耳机 已发货 运单号 YT9876543210123",
        )
        self.conn.commit()
        self.assertTrue(result["created"])
        self.assertEqual(result["parsed"]["tracking_no"], "YT9876543210123")
        results = db.search(self.conn, "蓝牙耳机")
        self.assertTrue(results)
        self.assertEqual(results[0]["doc_type"], "capture")

    def test_refund_updates_order_not_shipment(self):
        db.upsert_order(
            self.conn,
            platform="jd",
            platform_order_id="R123456",
            title="测试商品",
            tracking_no="JD0123456789012",
        )
        db.upsert_shipment(
            self.conn,
            tracking_no="JD0123456789012",
            carrier="jd",
            platform="jd",
            status="delivered",
            last_event_at="2026-10-26T10:00:00+00:00",
        )
        ingest_sms(
            self.conn,
            text="【京东】订单号：R123456 退款成功，退款将原路退回。",
            received_at="2026-10-26T11:00:00+00:00",
        )
        self.conn.commit()
        order = db.list_orders(self.conn, limit=10)[0]
        self.assertEqual(order["status"], "refunded")
        self.assertEqual(db.get_shipment(self.conn, "JD0123456789012")["status"], "delivered")

    def test_alert_created(self):
        ingest_sms(
            self.conn,
            text="【美团外卖】骑手已取餐，正在配送中，预计 12:35 送达。",
        )
        self.conn.commit()
        alerts = db.list_alerts(self.conn)
        self.assertTrue(alerts)
        self.assertIn("外卖", alerts[0]["title"])


if __name__ == "__main__":
    unittest.main()
