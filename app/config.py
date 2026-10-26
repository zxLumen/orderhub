"""Runtime configuration for OrderHub.

The app intentionally uses only the Python standard library so it can run on a
plain Windows install. Optional collectors (Playwright) are imported lazily.
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Tiny .env loader (KEY=VALUE, # comments). Existing env wins."""
    if not path.exists():
        return
    try:
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        pass


_load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


DATA_DIR = Path(os.environ.get("ORDERHUB_DATA_DIR", str(BASE_DIR / "data"))).expanduser()
DB_PATH = Path(os.environ.get("ORDERHUB_DB", str(DATA_DIR / "orderhub.db"))).expanduser()
WEB_DIR = Path(os.environ.get("ORDERHUB_WEB_DIR", str(BASE_DIR / "web"))).expanduser()
DEBUG_DIR = DATA_DIR / "debug"

HOST = os.environ.get("ORDERHUB_HOST", "0.0.0.0")
PORT = int(os.environ.get("ORDERHUB_PORT", "8000"))
TOKEN = os.environ.get("ORDERHUB_TOKEN", "").strip()

# Optional integrations
KUAIDI100_CUSTOMER = os.environ.get("KUAIDI100_CUSTOMER", "").strip()
KUAIDI100_KEY = os.environ.get("KUAIDI100_KEY", "").strip()
KUAIDI100_PHONE = os.environ.get("KUAIDI100_PHONE", "").strip()
BARK_URL = os.environ.get("BARK_URL", "").strip().rstrip("/")

ENABLE_SCHEDULER = _bool("ENABLE_SCHEDULER", True)
ENABLE_JD_COLLECTOR = _bool("ENABLE_JD_COLLECTOR", False)
JD_CDP_URL = os.environ.get("JD_CDP_URL", "http://127.0.0.1:9222").strip()
JD_ORDER_URL = os.environ.get(
    "JD_ORDER_URL", "https://order.jd.com/center/list.action"
).strip()
JD_INTERVAL_SECONDS = int(os.environ.get("JD_INTERVAL_SECONDS", "21600"))
PARCEL_POLL_SECONDS = int(os.environ.get("PARCEL_POLL_SECONDS", "1800"))

# Keep a sane upper bound on request bodies (screenshots are sent as text).
MAX_BODY_BYTES = int(os.environ.get("ORDERHUB_MAX_BODY", str(2 * 1024 * 1024)))

# User-facing defaults
APP_NAME = "OrderHub"
