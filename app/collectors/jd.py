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


def _page_state(page: Any) -> tuple[bool, bool]:
    url = (page.url or "").lower()
    body = page.inner_text("body")[:20_000]
    captcha = any(hint in body for hint in CAPTCHA_HINTS) or "risk" in url
    login_required = (
        "passport.jd.com" in url
        or "login.jd.com" in url
        or (any(hint in body for hint in LOGIN_HINTS) and "我的订单" not in body)
    )
    return captcha, login_required


def _scroll_to_bottom(page: Any) -> None:
    stable = 0
    last_height = -1
    for _ in range(config.JD_MAX_SCROLLS):
        height = page.evaluate(
            "Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)"
        )
        page.mouse.wheel(0, 2500)
        page.wait_for_timeout(850)
        new_height = page.evaluate(
            "Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)"
        )
        if new_height == height or new_height == last_height:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        last_height = new_height


def _next_page(page: Any) -> bool:
    for selector in (
        "a:has-text('下一页')",
        "button:has-text('下一页')",
        ".pn-next",
        "a.next",
    ):
        try:
            locator = page.locator(selector).first
            if locator.count() == 0 or not locator.is_visible() or not locator.is_enabled():
                continue
            locator.click(timeout=5_000)
            page.wait_for_timeout(2_500)
            return True
        except Exception:
            continue
    return False


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

            captcha, login_required = _page_state(page)

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

            collected: dict[str, dict[str, Any]] = {}
            pages = 0
            for page_index in range(config.JD_MAX_PAGES):
                _scroll_to_bottom(page)
                parsed_orders = parse_jd_orders(page.content())
                pages += 1
                for order in parsed_orders:
                    key = (
                        order.get("platform_order_id")
                        or f"{order.get('title')}|{order.get('amount')}|{order.get('order_time')}"
                    )
                    collected[key] = order

                if page_index + 1 >= config.JD_MAX_PAGES:
                    break
                if not _next_page(page):
                    break
                captcha, login_required = _page_state(page)
                if captcha or login_required:
                    break

            orders = list(collected.values())
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
                    message=(
                        f"{len(orders)} orders across {pages} page(s) "
                        f"in {time.time() - started:.1f}s"
                    ),
                )
            log.info("JD collection: %s orders across %s pages", len(orders), pages)
            return {"ok": True, "items": len(orders), "pages": pages}
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
