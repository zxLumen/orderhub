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
from .jd_parser import order_from_api, parse_jd_orders

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


def _scroll_and_parse(page: Any) -> dict[str, dict[str, Any]]:
    """Scroll step by step and parse on every step.

    Product thumbnails are lazy-loaded: a card only gets its <img> once it
    enters the viewport. Jumping straight to the bottom leaves the cards we
    scrolled past without images, so parse at each step and keep whichever
    version carries an image URL.
    """
    found: dict[str, dict[str, Any]] = {}
    stable = 0
    last_height = -1

    try:
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(300)
    except Exception:  # pragma: no cover - defensive
        pass

    def merge() -> None:
        for order in parse_jd_orders(page.content()):
            key = (
                order.get("platform_order_id")
                or f"{order.get('title')}|{order.get('amount')}|{order.get('order_time')}"
            )
            previous = found.get(key)
            if previous is None or (not previous.get("image_url") and order.get("image_url")):
                found[key] = order

    for _ in range(config.JD_MAX_SCROLLS):
        merge()
        height = page.evaluate(
            "Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)"
        )
        at_bottom = page.evaluate(
            "window.innerHeight + window.scrollY >= "
            "Math.max(document.body.scrollHeight, document.documentElement.scrollHeight) - 200"
        )
        if at_bottom and height == last_height:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        last_height = height
        page.mouse.wheel(0, 1400)
        page.wait_for_timeout(700)

    merge()
    return found


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


MODE_RECENT = "recent"
MODE_FULL = "full"


def normalize_mode(value: object) -> str:
    """Only an explicit ``full`` triggers the multi-period history scan."""
    return MODE_FULL if str(value or "").strip().lower() == MODE_FULL else MODE_RECENT


def _open_period_menu(page: Any) -> Any | None:
    """Open the time-range dropdown, retrying until its items exist.

    JD removes the option nodes from the DOM while the dropdown is closed, and
    a click landing during the close animation is swallowed silently.
    """
    select = page.locator("div[class^='selectContainer-'] div[class^='select-']").first
    items = page.locator("div[class^='selectList-'] div[class^='item-']")
    for _ in range(3):
        try:
            select.click(timeout=5_000)
        except Exception:  # pragma: no cover - transient
            pass
        page.wait_for_timeout(600)
        if items.count() > 0:
            return items
        page.wait_for_timeout(500)
    return None


def _period_labels(page: Any) -> list[str]:
    """Time ranges offered by JD, e.g. 近3个月订单 / 今年内订单 / 2025年订单."""
    items = _open_period_menu(page)
    if items is None:
        return []
    labels: list[str] = []
    for index in range(items.count()):
        label = items.nth(index).inner_text().strip()
        if label and label not in labels:
            labels.append(label)
    try:
        page.keyboard.press("Escape")
    except Exception:  # pragma: no cover - defensive
        pass
    return labels


def _select_period(page: Any, label: str) -> bool:
    """Pick one time range in JD's custom dropdown. False when unavailable."""
    items = _open_period_menu(page)
    if items is None:
        return False

    current = page.locator(
        "div[class^='selectContainer-'] span[class^='text-']"
    ).first
    for attempt in range(2):
        for index in range(items.count()):
            item = items.nth(index)
            if item.inner_text().strip() != label:
                continue
            try:
                item.click(timeout=5_000)
            except Exception:  # pragma: no cover - transient
                continue
            page.wait_for_timeout(1_800)
            try:
                if current.inner_text().strip() == label:
                    return True
            except Exception:  # pragma: no cover - transient
                pass
        # The menu may have closed before the click landed; reopen and retry.
        items = _open_period_menu(page)
        if items is None:
            break
    log.warning("JD period select failed for %s", label)
    return False


# JD's order-list XHR; its JSON holds every field the page renders plus the
# per-order detail PassKey, so it is the primary data source.
ORDER_API_HINT = "pcorder_center_list"


class OrderApiSink:
    """Defer API body reads until we are outside the response event handler."""

    def __init__(self) -> None:
        self._responses: list[Any] = []
        self.orders: dict[str, dict[str, Any]] = {}

    def on_response(self, response: Any) -> None:
        try:
            if ORDER_API_HINT in response.url:
                self._responses.append(response)
        except Exception:  # pragma: no cover - defensive
            pass

    def drain(self) -> None:
        while self._responses:
            response = self._responses.pop(0)
            try:
                payload = response.json()
            except Exception:
                continue
            data = payload.get("data") if isinstance(payload, dict) else None
            for order in (data or {}).get("orderList") or []:
                if isinstance(order, dict) and order.get("orderId"):
                    self.orders[str(order["orderId"])] = order


def _merge_sources(
    api_orders: dict[str, dict[str, Any]],
    dom_orders: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """API wins; the DOM adds the rendered logistics line and image fallback."""
    merged: dict[str, dict[str, Any]] = {}
    for order_id, raw in api_orders.items():
        order = order_from_api(raw)
        dom = dom_orders.get(order_id)
        if dom:
            if not order.get("logistics_text"):
                order["logistics_text"] = dom.get("logistics_text") or ""
            if not order.get("image_url"):
                order["image_url"] = dom.get("image_url") or ""
        merged[order_id] = order
    for key, dom in dom_orders.items():
        merged.setdefault(key, dom)
    return merged


def _collect_period(
    page: Any,
    max_pages: int,
    sink: OrderApiSink,
) -> tuple[dict[str, dict[str, Any]], int]:
    """Scroll and paginate the selected period; returns (dom_orders, pages_read)."""
    collected: dict[str, dict[str, Any]] = {}
    pages = 0
    while pages < max_pages:
        for key, order in _scroll_and_parse(page).items():
            previous = collected.get(key)
            if previous is None or (not previous.get("image_url") and order.get("image_url")):
                collected[key] = order
        sink.drain()
        pages += 1
        if pages >= max_pages:
            break
        if not _next_page(page):
            break
        sink.drain()
        captcha, login_required = _page_state(page)
        if captcha or login_required:
            break
    return collected, pages


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


@contextmanager
def _attached_page(pw: Any) -> Iterator[Any]:
    """Open one tab in the user's real Chrome and always close it again.

    The tab must be closed while the CDP connection is still alive: if
    ``page.close()`` runs after ``sync_playwright()`` exits, it fails silently
    and every run leaks an order-list tab. Never close the browser itself -- it
    is the user's Chrome.
    """
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


def collect_jd(mode: str = MODE_RECENT) -> dict[str, Any]:
    if not _RUN_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "already_running"}
    try:
        return _collect_jd_impl(normalize_mode(mode))
    finally:
        _RUN_LOCK.release()


def _collect_jd_impl(mode: str = MODE_RECENT) -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"ok": False, "error": "playwright_not_installed"}

    with session() as conn:
        run_id = start_collector_run(conn, "jd")
        conn.commit()

    started = time.time()
    try:
        with sync_playwright() as pw, _attached_page(pw) as page:
            page.set_default_timeout(30_000)
            sink = OrderApiSink()
            page.on("response", sink.on_response)
            page.goto(config.JD_ORDER_URL, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(2_500)
            sink.drain()

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
                    notifier.notify(
                        conn,
                        kind="collector_captcha",
                        title="京东需要验证码",
                        body="打开 Chrome 完成滑块后，下一次采集会自动恢复。",
                        push=False,
                    )
                return {"ok": False, "error": "captcha", "debug": html_path}

            if login_required:
                with session() as conn:
                    finish_collector_run(
                        conn, run_id, status="login_required", message="京东登录已失效"
                    )
                    notifier.notify(
                        conn,
                        kind="collector_login",
                        title="京东登录已失效",
                        body="打开 Chrome 扫码登录京东，下一次采集会自动恢复。",
                        push=False,
                    )
                return {"ok": False, "error": "login_required"}

            labels = _period_labels(page)
            if mode == MODE_FULL and labels:
                targets: list[str | None] = list(labels)
            else:
                # The order page opens on 近3个月订单; a scheduled run only needs that.
                targets = [None]

            dom_orders: dict[str, dict[str, Any]] = {}
            pages = 0
            period_summary: list[str] = []
            stop_reason = ""
            for label in targets:
                if label is not None and not _select_period(page, label):
                    period_summary.append(f"{label}: skipped")
                    continue
                sink.drain()
                period_orders, period_pages = _collect_period(
                    page, config.JD_MAX_PAGES, sink
                )
                dom_orders.update(period_orders)
                pages += period_pages
                period_summary.append(
                    f"{label or 'default'}: {len(period_orders)}/{period_pages}p"
                )
                captcha, login_required = _page_state(page)
                if captcha:
                    stop_reason = "captcha"
                    break
                if login_required:
                    stop_reason = "login_required"
                    break

            sink.drain()
            orders = list(_merge_sources(sink.orders, dom_orders).values())
            if not orders:
                _debug_dump(page, "jd-empty")

            if stop_reason:
                with session() as conn:
                    notifier.notify(
                        conn,
                        kind=f"collector_{stop_reason}",
                        title="京东需要人工处理",
                        body=f"采集在 {period_summary[-1] if period_summary else '中途'} 中断：{stop_reason}",
                        push=False,
                    )

            with session() as conn:
                for order in orders:
                    upsert_order(conn, **order)
                finish_collector_run(
                    conn,
                    run_id,
                    status="partial" if stop_reason else ("ok" if orders else "empty"),
                    items=len(orders),
                    message=(
                        f"mode={mode} {len(orders)} orders across {pages} page(s) "
                        f"in {time.time() - started:.1f}s"
                        + (f" stopped={stop_reason}" if stop_reason else "")
                        + " :: "
                        + ", ".join(period_summary)
                    ),
                )
            log.info(
                "JD collection: mode=%s %s orders across %s pages",
                mode,
                len(orders),
                pages,
            )
            return {
                "ok": True,
                "items": len(orders),
                "pages": pages,
                "mode": mode,
                "stopped": stop_reason or None,
            }
    except Exception as exc:  # pragma: no cover - network/browser failures
        log.exception("JD collector failed")
        with session() as conn:
            finish_collector_run(conn, run_id, status="error", message=str(exc)[:1000])
        return {"ok": False, "error": str(exc)}


def collect_jd_async(mode: str = MODE_RECENT) -> None:
    """Thread entrypoint used by the API/scheduler."""
    result = collect_jd(mode)
    if not result.get("ok") and result.get("error") not in {
        "already_running",
        "captcha",
        "login_required",
        "playwright_not_installed",
    }:
        log.warning("JD collection result: %s", result)
