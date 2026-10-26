import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("import_orders", ROOT / "scripts" / "import_orders.py")
import_orders = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(import_orders)


class ImportTest(unittest.TestCase):
    def test_nested_data_dict(self):
        payload = {"data": {"orders": [{"title": "耳机", "platform": "douyin"}]}}
        self.assertEqual(import_orders._extract_orders(payload), payload["data"]["orders"])

    def test_single_order_dict(self):
        payload = {"title": "充电线", "platform": "jd"}
        self.assertEqual(import_orders._extract_orders(payload), [payload])

    def test_nested_items(self):
        payload = {"items": [{"title": "A"}, {"title": "B"}]}
        self.assertEqual(len(import_orders._extract_orders(payload)), 2)

    def test_empty(self):
        self.assertEqual(import_orders._extract_orders({"data": {}}), [])


if __name__ == "__main__":
    unittest.main()
