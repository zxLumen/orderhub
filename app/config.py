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


def resolve_path(value: str | Path | None, default: Path) -> Path:
    """Resolve relative paths against the project root, never the process CWD."""
    path = Path(value).expanduser() if value else default
    if not path.is_absolute():
        path = BASE_DIR / path
    return path.resolve()


DATA_DIR = resolve_path(os.environ.get("ORDERHUB_DATA_DIR"), BASE_DIR / "data")
DB_PATH = resolve_path(os.environ.get("ORDERHUB_DB"), DATA_DIR / "orderhub.db")
WEB_DIR = resolve_path(os.environ.get("ORDERHUB_WEB_DIR"), BASE_DIR / "web")
DEBUG_DIR = DATA_DIR / "debug"

HOST = os.environ.get("ORDERHUB_HOST", "127.0.0.1")
PORT = int(os.environ.get("ORDERHUB_PORT", "8000"))
TOKEN = os.environ.get("ORDERHUB_TOKEN", "").strip()

# Web login (public server). The token remains for API clients such as the
# collector agent and iOS Shortcuts.
PASSWORD_HASH = os.environ.get("ORDERHUB_PASSWORD_HASH", "").strip()
SESSION_SECRET = os.environ.get("ORDERHUB_SESSION_SECRET", "").strip()
SESSION_TTL_SECONDS = int(os.environ.get("ORDERHUB_SESSION_TTL", str(30 * 24 * 3600)))
COOKIE_SECURE = _bool("ORDERHUB_COOKIE_SECURE", False)
LOGIN_RATE_LIMIT = int(os.environ.get("ORDERHUB_LOGIN_RATE_LIMIT", "8"))
LOGIN_RATE_WINDOW = int(os.environ.get("ORDERHUB_LOGIN_RATE_WINDOW", "900"))

# Remote collector mode (Windows agent -> this server)
SERVER_URL = os.environ.get("ORDERHUB_SERVER_URL", "").strip().rstrip("/")
COLLECTOR_TOKEN = os.environ.get("ORDERHUB_COLLECTOR_TOKEN", "").strip() or TOKEN

# Web Push (browser native, no Bark)
VAPID_PUBLIC_KEY = os.environ.get("ORDERHUB_VAPID_PUBLIC_KEY", "").strip()
VAPID_PRIVATE_KEY = os.environ.get("ORDERHUB_VAPID_PRIVATE_KEY", "").strip()
VAPID_SUBJECT = os.environ.get("ORDERHUB_VAPID_SUBJECT", "mailto:admin@example.com").strip()

# Optional integrations
KUAIDI100_CUSTOMER = os.environ.get("KUAIDI100_CUSTOMER", "").strip()
KUAIDI100_KEY = os.environ.get("KUAIDI100_KEY", "").strip()
KUAIDI100_PHONE = os.environ.get("KUAIDI100_PHONE", "").strip()

ENABLE_SCHEDULER = _bool("ENABLE_SCHEDULER", True)
ENABLE_JD_COLLECTOR = _bool("ENABLE_JD_COLLECTOR", False)
JD_CDP_URL = os.environ.get("JD_CDP_URL", "http://127.0.0.1:9222").strip()
JD_ORDER_URL = os.environ.get(
    "JD_ORDER_URL", "https://order.jd.com/center/list.action"
).strip()
JD_INTERVAL_SECONDS = int(os.environ.get("JD_INTERVAL_SECONDS", "21600"))
JD_MAX_PAGES = max(1, int(os.environ.get("JD_MAX_PAGES", "50")))
JD_MAX_SCROLLS = max(1, int(os.environ.get("JD_MAX_SCROLLS", "8")))
JD_DETAIL_MAX = max(0, int(os.environ.get("JD_DETAIL_MAX", "10")))
PARCEL_POLL_SECONDS = int(os.environ.get("PARCEL_POLL_SECONDS", "1800"))

# Keep a sane upper bound on request bodies (screenshots are sent as text).
MAX_BODY_BYTES = int(os.environ.get("ORDERHUB_MAX_BODY", str(8 * 1024 * 1024)))

# User-facing defaults
APP_NAME = "OrderHub"
