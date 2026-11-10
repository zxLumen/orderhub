"""Browser collectors for Taobao / Pinduoduo / Douyin / Xianyu / Zhuanzhuan.

Runs on the Windows box, attaches to the user's already-logged-in Chrome over
CDP (same model as the JD collector), opens the order page, sniffs the JSON the
page itself loads, and falls back to the rendered text. This is deliberately
"experimental": platform pages change, and login/captcha is expected. The
collector reports ``login_required`` / ``captcha`` / ``invalid_page`` instead of
inventing data.

Run it from the agent:
    python -m app.collectors.web taobao
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .. import config
from ..db import (
    finish_collector_run,
    session,
    start_collector_run,
    upsert_order,
)
from ..services import notifier
from .web_parser import extract_orders, parse_order_text

log = logging.getLogger(__name__)

SUPPORTED = ("taobao", "pdd", "douyin", "xianyu", "zhuanzhuan")

# Default consumer order pages. They drift; every one can be overridden with
# ORDERHUB_URL_<PLATFORM> (e.g. ORDERHUB_URL_DOUYIN).
DEFAULT_URLS = {
    "taobao": "https://buyertrade.taobao.com/trade/itemlist/list_bought_items.htm",
    "pdd": "https://mobile.yangkeduo.com/orders.html",
    "douyin": "https://www.douyin.com/",
    "xianyu": "https://www.goofish.com/personal?tab=order",
    "zhuanzhuan": "https://www.zhuanzhuan.com/",
}

# Douyin / Zhuanzhuan do not expose a stable public order URL; start at the
# homepage and follow visible entries. If the platform changed them, set
# ORDERHUB_URL_<PLATFORM> to the URL copied from the logged-in Chrome tab.
NAV_TEXTS = {
    "douyin": ("商城", "我的订单", "订单"),
    "zhuanzhuan": ("我的", "我的订单", "我买到的", "购买"),
    "xianyu": ("我的", "我的订单"),
}

LOGIN_URLS = {
    "taobao": "https://login.taobao.com/",
    "pdd": "https://mobile.yangkeduo.com/login.html",
    "douyin": "https://www.douyin.com/",
    "xianyu": "https://www.goofish.com/",
    "zhuanzhuan": "https://m.zhuanzhuan.com/",
}

MOBILE = {"pdd"}
MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
)

LOGIN_HOSTS = (
    "login.taobao.com",
    "login.tmall.com",
    "passport.",
    "login.zhuanzhuan.com",
    "sso.douyin.com",
    "yangkeduo.com/login",
    "goofish.com/login",
)
CAPTCHA_WORDS = (
    "安全验证", "请完成验证", "拖动滑块", "滑块验证", "验证码", "访问过于频繁",
    "操作频繁", "请稍后再试", "人机验证", "risk",
)
LOGIN_WORDS = ("请登录", "登录后", "扫码登录", "密码登录", "手机号登录", "立即登录")
INVALID_WORDS = ("页面不存在", "404 Not Found", "访问的页面不存在", "页面走丢了")
ORDER_WORDS = ("全部订单", "我的订单", "订单号", "待收货", "待发货", "已完成", "暂无订单")

_RUN_LOCK = threading.Lock()


def order_url(platform: str) -> str:
    env_key = f"ORDERHUB_URL_{platform.upper()}"
    return (os.environ.get(env_key) or DEFAULT_URLS.get(platform) or "").strip()


def login_url(platform: str) -> str:
    return LOGIN_URLS.get(platform, "")


def page_state(url: str, title: str, body: str) -> str:
    """Classify a page as ok / login_required / captcha / invalid_page."""
    lowered_url = (url or "").lower()
    title = title or ""
    body = body or ""
    haystack = f"{title}\n{body[:30_000]}"
    if any(host in lowered_url for host in LOGIN_HOSTS) or "/login" in lowered_url:
        return "login_required"
    if any(word in haystack for word in INVALID_WORDS):
        return "invalid_page"
    has_orders = any(word in haystack for word in ORDER_WORDS)
    if not has_orders and any(word in haystack for word in LOGIN_WORDS):
        return "login_required"
    if any(word.lower() in haystack.lower() for word in CAPTCHA_WORDS):
        return "captcha"
    if not has_orders:
        return "invalid_page"
    return "ok"


class JsonSink:
    """Capture JSON responses, but read bodies outside the event handler."""

    def __init__(self, platform: str, *, max_responses: int = 120) -> None:
        self.platform = platform
        self.max_responses = max_responses
        self._pending: list[Any] = []
        self.orders: dict[str, dict[str, Any]] = {}
        self.seen = 0

    def on_response(self, response: Any) -> None:
        try:
            content_type = (response.headers.get("content-type") or "").lower()
            if "json" not in content_type:
                return
            length = response.headers.get("content-length")
            if length and int(length) > 4 * 1024 * 1024:
                return
            if self.seen >= self.max_responses:
                return
            self.seen += 1
            self._pending.append(response)
        except Exception:  # pragma: no cover - response already gone
            pass

    def drain(self) -> None:
        while self._pending:
            response = self._pending.pop(0)
            try:
                payload = response.json()
            except Exception:
                continue
            for order in extract_orders(payload, self.platform):
                self.orders.setdefault(order["platform_order_id"], order)


def _prepare_mobile(page: Any) -> None:
    """Make the attached desktop tab look like a phone for mobile-only pages."""
    try:
        cdp = page.context.new_cdp_session(page)
        cdp.send(
            "Emulation.setUserAgentOverride",
            {"userAgent": MOBILE_UA, "platform": "Android"},
        )
        cdp.send(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": 390,
                "height": 844,
                "deviceScaleFactor": 2,
                "mobile": True,
            },
        )
    except Exception:  # pragma: no cover - Chrome version dependent
        log.debug("mobile emulation unavailable", exc_info=True)


def _scroll(page: Any, sink: JsonSink, *, max_scrolls: int, max_pages: int) -> int:
    pages = 0
    for _ in range(max_scrolls):
        try:
            page.mouse.wheel(0, 1800)
        except Exception:
            break
        page.wait_for_timeout(900)
        sink.drain()
    for _ in range(max_pages):
        clicked = False
        for label in ("加载更多", "查看更多", "下一页"):
            try:
                locator = page.get_by_text(label, exact=False).first
                if locator.count() and locator.is_visible() and locator.is_enabled():
                    locator.click(timeout=3_000)
                    page.wait_for_timeout(1_800)
                    sink.drain()
                    pages += 1
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            break
    return pages


def _try_navigation(page: Any, platform: str) -> bool:
    """Follow visible '商城 / 我的订单' entries for platforms without a stable URL."""
    for label in NAV_TEXTS.get(platform, ()):
        for exact in (True, False):
            try:
                locator = page.get_by_text(label, exact=exact).first
                if not locator.count() or not locator.is_visible() or not locator.is_enabled():
                    continue
                locator.click(timeout=3_000)
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


@contextmanager
def _attached_page(pw: Any) -> Iterator[Any]:
    browser = pw.chromium.connect_over_cdp(config.JD_CDP_URL)
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()
    try:
        yield page
    finally:
        try:
            page.close()
        except Exception:
            pass


def collect_web(platform: str, mode: str = "recent") -> dict[str, Any]:
    platform = platform.strip().lower()
    if platform not in SUPPORTED:
        return {"ok": False, "error": "unsupported_platform", "platform": platform}
    if not _RUN_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "already_running", "platform": platform}
    try:
        return _collect_impl(platform)
    finally:
        _RUN_LOCK.release()


def _collect_impl(platform: str) -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"ok": False, "error": "playwright_not_installed", "platform": platform}

    url = order_url(platform)
    if not url:
        return {"ok": False, "error": "no_order_url", "platform": platform}

    with session() as conn:
        run_id = start_collector_run(conn, platform)
        conn.commit()

    started = time.time()
    try:
        with sync_playwright() as pw, _attached_page(pw) as page:
            page.set_default_timeout(20_000)
            if platform in MOBILE:
                _prepare_mobile(page)
            sink = JsonSink(platform)
            page.on("response", sink.on_response)
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(3_000)
            sink.drain()

            title = page.title()
            body = page.inner_text("body")[:30_000] if page.locator("body").count() else ""
            state = page_state(page.url, title, body)

            if state == "invalid_page":
                for _ in range(3):
                    if not _try_navigation(page, platform):
                        break
                    sink.drain()
                    title = page.title()
                    body = page.inner_text("body")[:30_000] if page.locator("body").count() else ""
                    state = page_state(page.url, title, body)
                    if state != "invalid_page":
                        break

            if state != "ok":
                html_path, png_path = _debug_dump(page, f"{platform}-{state}")
                with session() as conn:
                    finish_collector_run(
                        conn,
                        run_id,
                        status=state,
                        message=f"{state}: {page.url} {html_path}",
                    )
                    notifier.notify(
                        conn,
                        kind=f"collector_{state}_{platform}",
                        title=f"{platform} 采集需要人工处理",
                        body=(
                            f"请在这台 Windows 的 Chrome 登录 {platform}，"
                            f"然后重新点采集。调试文件：{html_path}"
                        ),
                        push=False,
                    )
                return {
                    "ok": False,
                    "error": state,
                    "platform": platform,
                    "url": page.url,
                    "debug": html_path,
                }

            pages = _scroll(
                page,
                sink,
                max_scrolls=max(1, config.WEB_MAX_SCROLLS),
                max_pages=max(0, config.WEB_MAX_PAGES),
            )
            sink.drain()
            orders = list(sink.orders.values())

            if not orders:
                orders = parse_order_text(body, platform)
            if not orders:
                _debug_dump(page, f"{platform}-empty")

            with session() as conn:
                for order in orders:
                    upsert_order(conn, **order)
                finish_collector_run(
                    conn,
                    run_id,
                    status="ok" if orders else "empty",
                    items=len(orders),
                    message=(
                        f"{len(orders)} orders, {pages} extra page(s), "
                        f"json_responses={sink.seen}, {time.time() - started:.1f}s"
                    ),
                )
            log.info(
                "%s collection: %s orders, %s extra pages, %s json responses",
                platform,
                len(orders),
                pages,
                sink.seen,
            )
            return {
                "ok": True,
                "platform": platform,
                "items": len(orders),
                "pages": pages,
                "json_responses": sink.seen,
                "source": "json" if sink.orders else "text",
            }
    except Exception as exc:  # pragma: no cover - browser/network failures
        log.exception("%s collector failed", platform)
        with session() as conn:
            finish_collector_run(conn, run_id, status="error", message=str(exc)[:1000])
        return {"ok": False, "error": str(exc), "platform": platform}


def main(argv: list[str] | None = None) -> int:
    import argparse
    import logging as _logging

    parser = argparse.ArgumentParser(description="Collect one platform's orders")
    parser.add_argument("platform", choices=SUPPORTED)
    args = parser.parse_args(argv)
    _logging.basicConfig(level=_logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    result = collect_web(args.platform)
    print(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
