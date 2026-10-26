"""OrderHub entrypoint: python -m app.server"""

from __future__ import annotations

import logging

from . import config, db
from .api import serve
from .scheduler import Scheduler


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    db.init_db()
    scheduler = Scheduler()
    if config.ENABLE_SCHEDULER:
        scheduler.start()
    logging.getLogger(__name__).info(
        "OrderHub %s | data=%s | auth=%s | kuaidi100=%s | bark=%s | jd=%s",
        "0.1.0",
        config.DB_PATH,
        "on" if config.TOKEN else "off",
        "on" if config.KUAIDI100_CUSTOMER and config.KUAIDI100_KEY else "off",
        "on" if config.BARK_URL else "off",
        "on" if config.ENABLE_JD_COLLECTOR else "off",
    )
    try:
        serve()
    finally:
        scheduler.stop()


if __name__ == "__main__":
    main()
