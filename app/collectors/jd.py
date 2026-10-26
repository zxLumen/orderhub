"""JD order collector driven by a real, already-logged-in Chrome via CDP.

Why CDP: launching a fresh automation browser is easy for JD to fingerprint.
Attaching to the user's normal Chrome keeps the real cookies, IP and browser
fingerprint, which dramatically reduces captcha frequency.

Playwright is optional. The rest of OrderHub runs without it.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from .. import config
from ..db import (
    add_alert,
    finish_collector_run,
    session,
    start_collector_run,
    upsert_order,
)
from ..services import bark
from .jd_parser import parse_jd_orders

log = logging.getLogger(__name__)

CAPTCHA_HINTS = ("安全验证", "滑块", "请完成验证", "拖动滑块", "验证过于频繁")
LOGIN_HINTS = ("请登录", "登录京东", "账户登录")


def _debug_dump(page: Any, prefix: str) -> tuple[str, str]:
    config.DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    html_path = config.DEBUG_DIR / f"{prefix}-{stamp}.html"
    png_path = config.DEBUG_DIR / f"{prefix}-{stamp}.png"
    try:
        html_path.write_text(page.content(), encoding="utf-8")
    except OSError:
        pass
    try:
        page.screenshot(path=str(png_path), full_page=False)
    except OSError:
        pass
    return str(html_path), str(png_path)


_RUN_LOCK = threading.Lock()


def collect_jd() -> dict[str, Any]:
    if not _RUN_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "already_running"}
    try:
        return _collect_jd_impl()
    finally:
        _RUN_LOCK.release()


def _collect_jd_impl() -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"ok": False, "error": "playwright_not_installed"}

    with session() as conn:
        run_id = start_collector_run(conn, "jd")
        conn.commit()

    started = time.time()
    page = None
    browser = None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(config.JD_CDP_URL)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()
            page.set_default_timeout(30_000)
            page.goto(config.JD_ORDER_URL, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(2_500)

            # Lazy-loaded order cards.
            for _ in range(3):
                page.mouse.wheel(0, 2200)
                page.wait_for_timeout(900)

            url = (page.url or "").lower()
            body = page.inner_text("body")[:20_000]
            captcha = any(hint in body for hint in CAPTCHA_HINTS) or "risk" in url
            login_required = (
                "passport.jd.com" in url
                or "login.jd.com" in url
                or (any(hint in body for hint in LOGIN_HINTS) and "我的订单" not in body)
            )

            if captcha:
                html_path, png_path = _debug_dump(page, "jd-captcha")
                with session() as conn:
                    finish_collector_run(
                        conn,
                        run_id,
                        status="captcha",
                        message=f"需要人工验证: {html_path} {png_path}",
                    )
                    add_alert(
                        conn,
                        kind="collector_captcha",
                        title="京东需要验证码",
                        body="打开 Chrome 完成滑块后，下一次采集会自动恢复。",
                    )
                bark.send("京东需要验证码", "打开 Chrome 完成滑块后自动恢复。")
                return {"ok": False, "error": "captcha", "debug": html_path}

            if login_required:
                with session() as conn:
                    finish_collector_run(
                        conn, run_id, status="login_required", message="京东登录已失效"
                    )
                    add_alert(
                        conn,
                        kind="collector_login",
                        title="京东登录已失效",
                        body="打开 Chrome 扫码登录京东，下一次采集会自动恢复。",
                    )
                bark.send("京东登录已失效", "打开 Chrome 扫码登录京东。")
                return {"ok": False, "error": "login_required"}

            html = page.content()
            orders = parse_jd_orders(html)
            if not orders:
                _debug_dump(page, "jd-empty")

            with session() as conn:
                for order in orders:
                    upsert_order(conn, **order)
                finish_collector_run(
                    conn,
                    run_id,
                    status="ok" if orders else "empty",
                    items=len(orders),
                    message=f"{len(orders)} orders in {time.time() - started:.1f}s",
                )
            log.info("JD collection: %s orders", len(orders))
            return {"ok": True, "items": len(orders)}
    except Exception as exc:  # pragma: no cover - network/browser failures
        log.exception("JD collector failed")
        with session() as conn:
            finish_collector_run(conn, run_id, status="error", message=str(exc)[:1000])
        return {"ok": False, "error": str(exc)}
    finally:
        try:
            if page is not None:
                page.close()
        except Exception:
            pass
        # Do NOT call browser.close() on a CDP-attached browser: it may close
        # the user's real Chrome. Closing the page is enough.



def collect_jd_async() -> None:
    """Thread entrypoint used by the API/scheduler."""
    result = collect_jd()
    if not result.get("ok") and result.get("error") not in {
        "already_running",
        "captcha",
        "login_required",
        "playwright_not_installed",
    }:
        log.warning("JD collection result: %s", result)
