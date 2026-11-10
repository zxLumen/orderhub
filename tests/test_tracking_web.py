import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import db
from app.services import ingest, kuaidi_web


class CarrierGuessTest(unittest.TestCase):
    def test_prefix_guesses(self):
        self.assertEqual(kuaidi_web.guess_types("SF1234567890123")[0], "shunfeng")
        self.assertEqual(kuaidi_web.guess_types("JD0266813636558")[0], "jd")
        self.assertEqual(kuaidi_web.guess_types("YT1234567890123")[0], "yuantong")
        self.assertEqual(kuaidi_web.guess_types("ZTO12345678901")[0], "zhongtong")
        self.assertEqual(kuaidi_web.guess_types("EA123456789CN")[0], "ems")
        self.assertEqual(kuaidi_web.guess_types("JT1234567890")[0], "jtexpress")

    def test_numeric_is_ambiguous(self):
        types = kuaidi_web.guess_types("123456789012345")
        self.assertGreaterEqual(len(types), 3)
        self.assertNotIn("shunfeng", types[:1])

    def test_carrier_names(self):
        self.assertEqual(kuaidi_web.normalize_carrier("京东快递"), "jd")
        self.assertEqual(kuaidi_web.normalize_carrier("顺丰速运"), "sf")
        self.assertEqual(kuaidi_web.normalize_carrier("zto"), "zto")
        self.assertEqual(kuaidi_web.normalize_carrier(""), "")


class TrackingStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "test.db"
        db.init_db(self.path)
        self.conn = db.connect(self.path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_query_tracking_creates_shipment_and_events(self):
        result = {
            "ok": True,
            "type": "shunfeng",
            "carrier_name": "顺丰速运",
            "state": "out_for_delivery",
            "events": [
                {"time": "2026-11-10 08:30:00", "context": "快件正在派送中", "status": ""},
                {"time": "2026-11-10 06:00:00", "context": "到达北京分拨中心", "status": ""},
            ],
        }
        with patch.object(ingest.kuaidi_web, "query", return_value=result):
            out = ingest.query_tracking(self.conn, "SF1234567890123")
            self.conn.commit()
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["state"], "out_for_delivery")
        self.assertEqual(out["events"], 2)
        shipment = db.get_shipment(self.conn, "SF1234567890123")
        self.assertEqual(shipment["carrier"], "sf")
        self.assertEqual(shipment["status"], "out_for_delivery")
        self.assertEqual(len(shipment["events"]), 2)

    def test_query_failure_is_reported(self):
        with patch.object(ingest.kuaidi_web, "query", return_value={"ok": False, "error": "查无结果"}):
            out = ingest.query_tracking(self.conn, "123456789012345")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "查无结果")

    def test_rejects_recycled_waybill_trace(self):
        db.upsert_shipment(
            self.conn,
            tracking_no="JD0266708738659",
            carrier="jd",
            status="in_transit",
            last_event_at="2026-10-29T11:00:00+08:00",
        )
        self.conn.commit()
        result = {
            "ok": True,
            "type": "jd",
            "carrier_name": "京东物流",
            "state": "delivered",
            "events": [{"time": "2023-06-08 13:17:51", "context": "快件被签收", "status": ""}],
        }
        with patch.object(ingest.kuaidi_web, "query", return_value=result):
            out = ingest.query_tracking(self.conn, "JD0266708738659")
        self.assertFalse(out["ok"])
        self.assertIn("丢弃", out["error"])
        shipment = db.get_shipment(self.conn, "JD0266708738659")
        self.assertEqual(shipment["status"], "in_transit")
        self.assertEqual(shipment["events"], [])

    def test_poll_uses_web_when_paid_missing(self):
        db.upsert_shipment(
            self.conn,
            tracking_no="YT1234567890123",
            carrier="yto",
            status="in_transit",
        )
        self.conn.commit()
        result = {
            "ok": True,
            "type": "yuantong",
            "carrier_name": "圆通速递",
            "state": "delivered",
            "events": [{"time": "2026-11-10 10:00:00", "context": "已签收", "status": ""}],
        }
        with patch("app.services.kuaidi100.configured", return_value=False), patch.object(
            ingest.kuaidi_web, "query", return_value=result
        ):
            out = ingest.poll_parcels(self.conn, limit=5)
            self.conn.commit()
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["source"], "kuaidi_web")
        self.assertGreaterEqual(out["checked"], 1)
        self.assertEqual(db.get_shipment(self.conn, "YT1234567890123")["status"], "delivered")
