import tempfile
import unittest
from pathlib import Path

from app import db
from app.services import ingest


class PollLockTest(unittest.TestCase):
    def test_second_poll_is_rejected_while_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            db.init_db(path)
            with db.session(path) as conn:
                self.assertTrue(ingest._POLL_LOCK.acquire(blocking=False))
                try:
                    result = ingest.poll_parcels(conn)
                finally:
                    ingest._POLL_LOCK.release()
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "already_running")


if __name__ == "__main__":
    unittest.main()
