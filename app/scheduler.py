"""Tiny background scheduler (no APScheduler dependency)."""

from __future__ import annotations

import importlib.util
import logging
import threading
import time

from . import config
from .collectors.jd import collect_jd
from .db import session
from .services import kuaidi100
from .services.ingest import poll_parcels

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._jd_lock = threading.Lock()
        self._next_parcel = time.time() + 60
        self._next_jd = time.time() + 180
        self._warned_playwright = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="orderhub-scheduler", daemon=True)
        self._thread.start()
        log.info("Scheduler started")

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            if now >= self._next_parcel:
                self._next_parcel = now + max(300, config.PARCEL_POLL_SECONDS)
                if kuaidi100.configured():
                    threading.Thread(target=self._run_parcel, daemon=True).start()
                else:
                    log.debug("Kuaidi100 not configured; parcel polling skipped")
            if config.ENABLE_JD_COLLECTOR and now >= self._next_jd:
                self._next_jd = now + max(1800, config.JD_INTERVAL_SECONDS)
                if importlib.util.find_spec("playwright") is None:
                    if not self._warned_playwright:
                        log.warning("JD collector enabled but Playwright is not installed")
                        self._warned_playwright = True
                else:
                    threading.Thread(target=self._run_jd, daemon=True).start()
            self._stop.wait(15)

    def _run_parcel(self) -> None:
        try:
            with session() as conn:
                result = poll_parcels(conn)
            log.info("Scheduled parcel poll: %s", result)
        except Exception:
            log.exception("Scheduled parcel poll failed")

    def _run_jd(self) -> None:
        if not self._jd_lock.acquire(blocking=False):
            return
        try:
            result = collect_jd()
            log.info("Scheduled JD collection: %s", result)
        except Exception:
            log.exception("Scheduled JD collection failed")
        finally:
            self._jd_lock.release()
