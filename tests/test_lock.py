import os
import tempfile
import time
import unittest
from pathlib import Path

from app import config, db
from app.lockfile import InstanceLock
from app.scheduler import Scheduler


class InstanceLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "orderhub.lock"
        self.locks: list[InstanceLock] = []

    def tearDown(self):
        for lock in self.locks:
            lock.release()
        self.tmp.cleanup()

    def lock(self) -> InstanceLock:
        instance = InstanceLock(self.path)
        self.locks.append(instance)
        return instance

    def test_second_instance_is_rejected(self):
        first = self.lock()
        self.assertTrue(first.acquire())
        self.assertEqual(first.owner(), str(os.getpid()))

        second = self.lock()
        self.assertFalse(second.acquire())

        first.release()
        third = self.lock()
        self.assertTrue(third.acquire())

    def test_release_is_idempotent(self):
        lock = self.lock()
        self.assertTrue(lock.acquire())
        lock.release()
        lock.release()


class SchedulerRecentRunTest(unittest.TestCase):
    def test_last_jd_run_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = config.DB_PATH
            config.DB_PATH = Path(tmp) / "test.db"
            try:
                db.init_db(config.DB_PATH)
                with db.session() as conn:
                    run_id = db.start_collector_run(conn, "jd")
                    db.finish_collector_run(conn, run_id, status="ok", items=1)
                value = Scheduler()._last_jd_run_at()
                self.assertIsNotNone(value)
                self.assertLess(abs(time.time() - value), 10)
            finally:
                config.DB_PATH = old


if __name__ == "__main__":
    unittest.main()
