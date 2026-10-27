"""OrderHub entrypoint: python -m app.server"""

from __future__ import annotations

import logging

from . import config, db
from .api import serve
from .lockfile import InstanceLock
from .scheduler import Scheduler


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger(__name__)

    lock = InstanceLock(config.DATA_DIR / "orderhub.lock")
    if not lock.acquire():
        owner = lock.owner() or "unknown"
        log.error(
            "Another OrderHub instance is already running (pid %s). "
            "Stop it first; refusing to start a second one.",
            owner,
        )
        raise SystemExit(1)

    try:
        db.init_db()
        scheduler: Scheduler | None = None
        if config.ENABLE_SCHEDULER:
            scheduler = Scheduler()
            scheduler.start()
        log.info(
            "OrderHub %s | data=%s | auth=%s | kuaidi100=%s | jd=%s",
            "0.1.0",
            config.DB_PATH,
            "on" if config.TOKEN else "off",
            "on" if config.KUAIDI100_CUSTOMER and config.KUAIDI100_KEY else "off",
            "on" if config.ENABLE_JD_COLLECTOR else "off",
        )
        serve()
    finally:
        if scheduler is not None:
            scheduler.stop()
        lock.release()


if __name__ == "__main__":
    main()
