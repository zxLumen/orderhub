import tempfile
import unittest
from pathlib import Path

from app import config, db


class PathTest(unittest.TestCase):
    def test_relative_path_uses_project_root(self):
        resolved = config.resolve_path("./somewhere", config.BASE_DIR / "data")
        self.assertEqual(resolved, (config.BASE_DIR / "somewhere").resolve())

    def test_custom_db_does_not_create_default_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            old_data, old_debug = config.DATA_DIR, config.DEBUG_DIR
            config.DATA_DIR = tmp_path / "default-data"
            config.DEBUG_DIR = config.DATA_DIR / "debug"
            try:
                custom = tmp_path / "custom" / "orders.db"
                db.init_db(custom)
                self.assertTrue(custom.exists())
                self.assertTrue((tmp_path / "custom" / "debug").exists())
                self.assertFalse(config.DATA_DIR.exists())
            finally:
                config.DATA_DIR, config.DEBUG_DIR = old_data, old_debug


if __name__ == "__main__":
    unittest.main()
