import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from app import db
from app.importers import parse_upload
from app.importers.files import decode_bytes
from app.importers.xlsx import read_xlsx
from app.services.imports import import_orders

ALIPAY_CSV = """支付宝交易记录明细查询
账号:[test@example.com]
起始日期:[2026-10-01 00:00:00]    终止日期:[2026-11-01 00:00:00]
---------------------------------交易记录明细列表------------------------------------
交易时间,交易分类,交易对方,对方账号,商品说明,收/支,金额,收/付款方式,交易状态,交易订单号,商家订单号,备注
2026-11-08 21:07:41,购物,黄飞红食品旗舰店,shop@example.com,麻辣花生米1斤,支出,24.29,余额宝,交易成功,2026110822001111111111,251108001234567890,淘宝
2026-11-07 12:00:00,购物,拼多多平台商户,pdd@example.com,抽纸 24 包,支出,39.90,花呗,交易成功,2026110722001111111112,251107001234567891,
2026-11-06 09:30:00,购物,闲鱼卖家,xy@example.com,二手机械键盘,支出,120.00,余额宝,交易成功,2026110622001111111113,251106001234567892,
2026-11-05 08:00:00,转账,张三,zhang@example.com,转账,不计收支,100.00,余额宝,交易成功,2026110522001111111114,,
2026-11-04 08:00:00,餐饮,美团外卖,mt@example.com,午餐,支出,32.00,余额宝,交易成功,2026110422001111111115,251104001234567893,
2026-11-03 08:00:00,购物,某小商店,shop2@example.com,矿泉水,支出,3.00,余额宝,退款成功,2026110322001111111116,251103001234567894,
"""

WECHAT_CSV = """微信支付账单明细
微信昵称:[测试]
起始时间:[2026-10-01 00:00:00] 终止时间:[2026-11-01 00:00:00]
导出类型:[全部]
----------------------微信支付账单明细列表--------------------
交易时间,交易类型,交易对方,商品,收/支,金额(元),支付方式,当前状态,交易单号,商户单号,备注
2026-11-09 10:00:00,商户消费,拼多多平台商户,纸巾 24 包,支出,¥19.90,零钱,支付成功,4200001234202611091234567890,251109001122334455,
2026-11-08 09:00:00,转账,李四,转账,支出,¥50.00,零钱,已转账,1000050001202611081234567890,,
2026-11-07 09:00:00,商户消费,抖音商城,洗衣液,支出,¥59.00,银行卡,已全额退款,4200001234202611071234567891,251107001122334456,
"""

GENERIC_CSV = """订单号,下单时间,商品名称,店铺名称,订单金额,订单状态,运单号,快递公司
TB202611010001,2026-11-01 12:00:00,机械键盘 87 键,某某数码旗舰店,299.00,已发货,SF1234567890123,顺丰
TB202611020002,2026-11-02 12:00:00,显示器支架,某某数码旗舰店,159.00,已完成,,
"""


def _xlsx_bytes() -> bytes:
    shared = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<si><t>订单号</t></si><si><t>下单时间</t></si><si><t>商品名称</t></si>
<si><t>店铺名称</t></si><si><t>订单金额</t></si><si><t>订单状态</t></si>
<si><t>运单号</t></si><si><t>快递公司</t></si>
<si><t>PDD20261103001</t></si><si><t>2026-11-03 10:00:00</t></si>
<si><t>牛奶一箱</t></si><si><t>多多超市</t></si><si><t>49.90</t></si>
<si><t>已签收</t></si><si><t>YT1234567890123</t></si><si><t>圆通</t></si>
</sst>"""
    sheet = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>
<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="C1" t="s"><v>2</v></c><c r="D1" t="s"><v>3</v></c><c r="E1" t="s"><v>4</v></c><c r="F1" t="s"><v>5</v></c><c r="G1" t="s"><v>6</v></c><c r="H1" t="s"><v>7</v></c></row>
<row r="2"><c r="A2" t="s"><v>8</v></c><c r="B2" t="s"><v>9</v></c><c r="C2" t="s"><v>10</v></c><c r="D2" t="s"><v>11</v></c><c r="E2" s="1"><v>49.9</v></c><c r="F2" t="s"><v>13</v></c><c r="G2" t="s"><v>14</v></c><c r="H2" t="s"><v>15</v></c></row>
</sheetData></worksheet>"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/sharedStrings.xml", shared)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return buffer.getvalue()


class ImporterTest(unittest.TestCase):
    def test_decode_gbk_and_bom(self):
        self.assertIn("交易时间", decode_bytes(ALIPAY_CSV.encode("gbk")))
        self.assertIn("交易时间", decode_bytes(("﻿" + WECHAT_CSV).encode("utf-8")))

    def test_alipay_bill(self):
        parsed = parse_upload(ALIPAY_CSV.encode("gbk"), "alipay_record_20261110.csv")
        self.assertTrue(parsed["ok"], parsed)
        self.assertEqual(parsed["sources"], ["alipay"])
        platforms = {item["platform"] for item in parsed["orders"]}
        self.assertEqual(platforms, {"taobao", "pdd", "xianyu", "meituan", "other"})
        self.assertEqual(parsed["stats"]["skipped_income"], 1)
        keyboard = next(item for item in parsed["orders"] if "键盘" in item["title"])
        self.assertEqual(keyboard["platform"], "xianyu")
        self.assertEqual(keyboard["amount"], 120.0)
        self.assertEqual(keyboard["platform_order_id"], "251106001234567892")
        refund = next(item for item in parsed["orders"] if "矿泉水" in item["title"])
        self.assertEqual(refund["status"], "refunded")

    def test_wechat_bill(self):
        parsed = parse_upload(("﻿" + WECHAT_CSV).encode("utf-8"), "微信支付账单.csv")
        self.assertTrue(parsed["ok"], parsed)
        self.assertEqual(parsed["sources"], ["wechat"])
        platforms = [item["platform"] for item in parsed["orders"]]
        self.assertEqual(platforms.count("pdd"), 1)
        self.assertEqual(platforms.count("douyin"), 1)
        pdd = next(item for item in parsed["orders"] if item["platform"] == "pdd")
        self.assertEqual(pdd["amount"], 19.9)
        douyin = next(item for item in parsed["orders"] if item["platform"] == "douyin")
        self.assertEqual(douyin["status"], "refunded")

    def test_generic_export_csv(self):
        parsed = parse_upload(GENERIC_CSV.encode("utf-8"), "taobao_orders.csv")
        self.assertTrue(parsed["ok"], parsed)
        self.assertEqual(len(parsed["orders"]), 2)
        self.assertTrue(all(item["platform"] == "taobao" for item in parsed["orders"]))
        first = parsed["orders"][0]
        self.assertEqual(first["tracking_no"], "SF1234567890123")
        self.assertEqual(first["status"], "shipped")

    def test_xlsx_export(self):
        rows = read_xlsx(_xlsx_bytes())
        self.assertEqual(rows[0][0], "订单号")
        parsed = parse_upload(_xlsx_bytes(), "pdd_orders.xlsx")
        self.assertTrue(parsed["ok"], parsed)
        item = parsed["orders"][0]
        self.assertEqual(item["platform"], "pdd")
        self.assertEqual(item["title"], "牛奶一箱")
        self.assertEqual(item["amount"], 49.9)
        self.assertEqual(item["tracking_no"], "YT1234567890123")

    def test_zip_with_mixed_files(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("alipay_record.csv", ALIPAY_CSV.encode("gbk"))
            archive.writestr("pdd_orders.xlsx", _xlsx_bytes())
        parsed = parse_upload(buffer.getvalue(), "支付宝交易流水.zip")
        self.assertTrue(parsed["ok"], parsed)
        self.assertEqual(len(parsed["tables"]), 2)
        platforms = {item["platform"] for item in parsed["orders"]}
        self.assertIn("pdd", platforms)
        self.assertIn("xianyu", platforms)

    def test_unknown_rows_and_include_unknown(self):
        parsed = parse_upload(ALIPAY_CSV.encode("gbk"), "alipay.csv")
        self.assertIn("other", {item["platform"] for item in parsed["orders"]})
        parsed = parse_upload(
            ALIPAY_CSV.encode("gbk"), "alipay.csv", include_unknown=False
        )
        self.assertEqual(parsed["stats"]["rows"], 6)

    def test_invalid_file(self):
        parsed = parse_upload(b"hello world", "notes.txt")
        self.assertFalse(parsed["ok"])
        self.assertIn("表头", parsed["error"])


class ImportServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "test.db"
        db.init_db(self.path)
        self.conn = db.connect(self.path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_import_is_idempotent_and_keeps_source(self):
        parsed = parse_upload(GENERIC_CSV.encode("utf-8"), "taobao_orders.csv")
        first = import_orders(self.conn, parsed["orders"])
        self.conn.commit()
        self.assertEqual(first["created"], 2)
        self.assertEqual(first["updated"], 0)
        second = import_orders(self.conn, parsed["orders"])
        self.conn.commit()
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["updated"], 2)
        self.assertEqual(db.stats(self.conn)["orders"], 2)

        rows = db.list_orders(self.conn, platform="taobao")
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["source"] == "export" for row in rows))
        # Tracking number creates and links a shipment.
        self.assertEqual(db.stats(self.conn)["shipments"], 1)
        self.assertEqual(db.stats(self.conn)["platforms"][0]["platform"], "taobao")
