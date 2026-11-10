"""Douyin (抖音电商) order collector.

The Douyin App still hides its order list from accessibility services and draws
product names on a canvas, but the list itself is backed by a plain JSON API::

    https://ec.snssdk.com/order/page?tab=0&page=1
        &aid=1128&app_name=aweme&device_platform=android&version_code=410100

The endpoint authenticates with the account session cookies and needs no
request signature. Those cookies live in the App's WebView cookie database
(``app_webview/Default/Cookies``) as *plaintext* rows. On a rooted emulator the
agent can therefore refresh them with adb and pull every order page - no OCR,
no Frida, no clicking through individual orders.

Typical usage::

    python -m app.collectors.douyin            # collect + print a summary
    python -m app.collectors.douyin --refresh  # only refresh cookies
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .. import config
from ..db import finish_collector_run, session, start_collector_run, upsert_order
from .web_parser import _amount, _time, map_status

log = logging.getLogger(__name__)

API = "https://ec.snssdk.com/order/page"
DETAIL_URL = "https://haohuo.jinritemai.com/views/neworder/index?id={order_id}"
CENTER_URL = "https://haohuo.jinritemai.com/views/neworder/center"

UA = (
    "Mozilla/5.0 (Linux; Android 12; DCO-AL00 Build/HUAWEIDCO-AL00; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/107.0.0.0 "
    "Mobile Safari/537.36 aweme_410100"
)

# App params are required: without app_name/aid the same endpoint answers
# ``st=0`` with an empty list even for a logged-in account.
APP_PARAMS = {
    "aid": "1128",
    "app_name": "aweme",
    "device_platform": "android",
    "version_code": "410100",
    "device_id": "0",
}

COOKIE_NAMES = (
    "sessionid",
    "sessionid_ss",
    "sid_tt",
    "sid_guard",
    "uid_tt",
    "uid_tt_ss",
    "odin_tt",
    "passport_csrf_token",
    "ttwid",
)

COOKIE_FILE = config.DATA_DIR / "douyin-cookies.json"

COOKIE_DB = "/data/data/com.ss.android.ugc.aweme/app_webview/Default/Cookies"
ADB_SERIAL = os.environ.get("ORDERHUB_ADB_SERIAL", "127.0.0.1:16384").strip()

_ADB_CANDIDATES = (
    os.environ.get("ORDERHUB_ADB", "").strip(),
    r"F:\Program Files\Netease\MuMuPlayer-12.0\nx_device\12.0\shell\adb.exe",
    r"C:\Program Files\Netease\MuMuPlayer-12.0\nx_device\12.0\shell\adb.exe",
    "adb",
)

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class DouyinAuthError(RuntimeError):
    """The session cookies are missing or expired."""


def _adb() -> str | None:
    for candidate in _ADB_CANDIDATES:
        if not candidate:
            continue
        if candidate == "adb" or Path(candidate).exists():
            return candidate
    return None


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        cmd,
        capture_output=True,
        timeout=timeout,
        creationflags=_NO_WINDOW,
    )


def refresh_cookies_from_emulator() -> dict[str, str]:
    """Copy the WebView cookie DB off the emulator and extract the session."""
    adb = _adb()
    if not adb:
        raise DouyinAuthError("adb_not_found")
    remote_dir = "/data/local/tmp"
    remote = f"{remote_dir}/orderhub-cookies.db"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            for suffix in ("", "-wal", "-journal"):
                _run(
                    [
                        adb,
                        "-s",
                        ADB_SERIAL,
                        "shell",
                        f"cp {COOKIE_DB}{suffix} {remote}{suffix} 2>/dev/null || true",
                    ],
                    timeout=30,
                )
                _run(
                    [adb, "-s", ADB_SERIAL, "pull", f"{remote}{suffix}", str(tmpdir / f"Cookies{suffix}")],
                    timeout=60,
                )
            db_path = tmpdir / "Cookies"
            if not db_path.exists() or db_path.stat().st_size == 0:
                raise DouyinAuthError("douyin_cookie_db_missing")
            con = sqlite3.connect(db_path)
            try:
                rows = con.execute(
                    "select host_key, name, value from cookies where length(value) > 0"
                ).fetchall()
            finally:
                con.close()
    finally:
        # The copy carries live session cookies; never leave it on /data/local/tmp.
        _run(
            [adb, "-s", ADB_SERIAL, "shell", f"rm -f {remote} {remote}-wal {remote}-journal"],
            timeout=20,
        )

    # Values are shared across the ByteDance cookie domains; prefer the host
    # that the e-commerce API actually reads.
    preferred_hosts = (".snssdk.com", ".douyin.com", ".ecombdapi.com", ".amemv.com")
    cookies: dict[str, str] = {}
    for host, name, value in rows:
        if name in COOKIE_NAMES and host in preferred_hosts:
            cookies.setdefault(name, value)
    if not cookies:
        for host, name, value in rows:
            if name in COOKIE_NAMES:
                cookies.setdefault(name, value)
    if not cookies.get("sessionid"):
        raise DouyinAuthError("douyin_sessionid_missing")

    COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
    COOKIE_FILE.write_text(json.dumps(cookies, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("refreshed Douyin cookies from emulator (%s keys)", len(cookies))
    return cookies


def load_cookies(*, refresh: bool = True) -> dict[str, str]:
    if COOKIE_FILE.exists():
        try:
            data = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("sessionid"):
                return {k: str(v) for k, v in data.items() if k in COOKIE_NAMES}
        except (OSError, ValueError):
            pass
    if not refresh:
        raise DouyinAuthError("douyin_login_required")
    return refresh_cookies_from_emulator()


def _cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{name}={value}" for name, value in cookies.items() if value)


def fetch_page(page: int, *, tab: int = 0, cookies: dict[str, str]) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"tab": tab, "page": page, **APP_PARAMS})
    request = urllib.request.Request(
        f"{API}?{query}",
        headers={
            "User-Agent": UA,
            "Cookie": _cookie_header(cookies),
            "Accept": "application/json",
            "Referer": CENTER_URL,
        },
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        payload = json.loads(response.read().decode("utf-8"))

    if payload.get("code") == "10001010A" or payload.get("st") not in (0, "0"):
        message = str(payload.get("msg") or "")
        if "登录" in message or payload.get("code") == "10001010A" or payload.get("st") in (None, 8, 10005):
            raise DouyinAuthError(f"douyin_auth:{payload.get('st')}:{message}")
        raise RuntimeError(f"douyin_api_error:{payload.get('st')}:{message}")
    return payload.get("data") or []


def _logistics_text(order: dict[str, Any]) -> str | None:
    logistics = order.get("logistics")
    if isinstance(logistics, dict):
        parts = [
            str(logistics.get(key) or "").strip()
            for key in ("company_name", "company", "tracking_number", "tracking_no")
        ]
        text = " ".join(part for part in parts if part)
        if text:
            return text
    tracking = str(order.get("post_code") or "").strip()
    return tracking or None


def to_order(order: dict[str, Any]) -> dict[str, Any] | None:
    """Map one Douyin API order to the OrderHub order shape."""
    order_id = str(order.get("order_id") or "").strip()
    if not order_id:
        return None

    title = str(order.get("product_name") or "").strip()
    sub_orders = order.get("sub_orders") or []
    if not title and sub_orders:
        title = str(sub_orders[0].get("product_name") or "").strip()

    spec = str(order.get("spec_desc") or "").strip()
    if spec and spec != "默认":
        title = f"{title}（{spec}）" if title else spec

    shop = str(order.get("old_shop_name") or order.get("shop_name") or "").strip()
    # "xx的橱窗" is the buyer-side storefront label; the real shop is nicer.
    if shop.endswith("的橱窗"):
        shop = shop[: -len("的橱窗")]

    status, status_text = map_status(order.get("order_status_str") or "")
    amount = _amount(order.get("total_amount"), "douyin")

    # The user wants the order time, not the row's update time. `create_time`
    # is the checkout time in Beijing local time.
    order_time = _time(order.get("create_time")) or _time(order.get("pay_time"))

    tracking = str(order.get("post_code") or "").strip() or None
    if not tracking and isinstance(order.get("logistics"), dict):
        tracking = str(
            order["logistics"].get("tracking_number")
            or order["logistics"].get("tracking_no")
            or ""
        ).strip() or None

    raw = {
        "_source": "douyin_api",
        "_platform": "douyin",
        "order_id": order_id,
        "product_id": order.get("product_id"),
        "shop_id": order.get("shop_id"),
        "order_status": order.get("order_status"),
        "final_status": order.get("final_status"),
        "total_amount": order.get("total_amount"),
        "create_time": order.get("create_time"),
        "pay_time": order.get("pay_time"),
        "post_addr": order.get("post_addr"),
        "post_receiver": order.get("post_receiver"),
        "spec_desc": order.get("spec_desc"),
        "sub_order_ids": [
            str(item.get("order_id") or "")
            for item in sub_orders
            if isinstance(item, dict)
        ],
    }

    return {
        "platform": "douyin",
        "platform_order_id": order_id,
        "title": title or f"抖音订单 {order_id}",
        "shop": shop,
        "amount": amount,
        "status": status,
        "status_text": status_text,
        "order_time": order_time,
        "tracking_no": tracking,
        "logistics_text": _logistics_text(order),
        "url": DETAIL_URL.format(order_id=order_id),
        "product_url": DETAIL_URL.format(order_id=order_id),
        "image_url": str(order.get("img") or "").strip() or None,
        "source": "douyin-api",
        "raw": raw,
    }


def collect_douyin(mode: str = "recent", *, max_pages: int = 50) -> dict[str, Any]:
    """Pull every order page and upsert the results."""
    started = time.time()
    with session() as conn:
        run_id = start_collector_run(conn, "douyin")

    try:
        cookies = load_cookies(refresh=True)
        try:
            batches = _fetch_all(cookies, max_pages)
        except DouyinAuthError:
            log.warning("Douyin cookies rejected; refreshing from emulator")
            cookies = refresh_cookies_from_emulator()
            batches = _fetch_all(cookies, max_pages)

        orders = [mapped for batch in batches for item in batch if (mapped := to_order(item))]
        pages = len(batches)

        with session() as conn:
            for order in orders:
                upsert_order(conn, **order)
            finish_collector_run(
                conn,
                run_id,
                status="ok" if orders else "empty",
                items=len(orders),
                message=f"{len(orders)} orders from {pages} page(s), {time.time() - started:.1f}s",
            )

        log.info("douyin collection: %s orders from %s pages", len(orders), pages)
        return {
            "ok": True,
            "platform": "douyin",
            "items": len(orders),
            "pages": pages,
            "source": "api",
        }
    except Exception as exc:  # network / cookie / API failures
        log.exception("douyin collector failed")
        with session() as conn:
            finish_collector_run(conn, run_id, status="error", message=str(exc)[:1000])
        error = str(exc)
        if isinstance(exc, DouyinAuthError) or "登录" in error:
            error = "login_required"
        return {"ok": False, "error": error, "platform": "douyin"}


def _fetch_all(cookies: dict[str, str], max_pages: int) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    for page in range(1, max_pages + 1):
        batch = fetch_page(page, tab=0, cookies=cookies)
        if not batch:
            break
        batches.append(batch)
    if not batches:
        # A logged-out session can also come back as a single empty page.
        raise DouyinAuthError("douyin_empty_result")
    return batches


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect Douyin orders via the e-commerce API")
    parser.add_argument("--refresh", action="store_true", help="only refresh cookies from the emulator")
    parser.add_argument("--json", action="store_true", help="print mapped orders as JSON")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.refresh:
        cookies = refresh_cookies_from_emulator()
        print(f"refreshed {len(cookies)} cookies -> {COOKIE_FILE}")
        return 0

    if args.json:
        cookies = load_cookies(refresh=True)
        orders = [mapped for batch in _fetch_all(cookies, 50) for item in batch if (mapped := to_order(item))]
        print(json.dumps(orders, ensure_ascii=False, indent=2))
        return 0

    result = collect_douyin()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
