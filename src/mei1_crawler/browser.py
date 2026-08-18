from __future__ import annotations

import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from playwright.sync_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright


LOGIN_HINT_INTERVAL_SECONDS = 8


@contextmanager
def open_browser(user_data_dir: Path, *, headed: bool = True) -> Iterator[tuple[BrowserContext, Page]]:
    user_data_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        launch_kwargs = {
            "user_data_dir": str(user_data_dir),
            "headless": not headed,
            "viewport": {"width": 1440, "height": 950},
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        try:
            context = playwright.chromium.launch_persistent_context(channel="chrome", **launch_kwargs)
        except Exception:
            context = playwright.chromium.launch_persistent_context(**launch_kwargs)
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(15_000)
        try:
            yield context, page
        finally:
            context.close()


@contextmanager
def connect_existing_chrome_cdp(cdp_url: str, *, tab_url_contains: str) -> Iterator[tuple[BrowserContext, Page]]:
    """Attach to an already-running Chrome DevTools endpoint without creating a tab."""
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.connect_over_cdp(cdp_url)
        except Exception as exc:  # noqa: BLE001 - turn Playwright transport details into an operator message.
            raise RuntimeError(
                f"Cannot attach to existing Chrome at {cdp_url}. "
                "Start that Chrome profile with --remote-debugging-port or provide --cdp-url. "
                "No browser was launched."
            ) from exc
        page = _find_existing_page(browser.contexts, tab_url_contains)
        if page is None:
            raise RuntimeError(f"No existing Chrome tab URL contains: {tab_url_contains}")
        page.set_default_timeout(15_000)
        yield page.context, page


def goto_and_wait_ready(
    page: Page,
    entry_url: str,
    *,
    timeout_seconds: int = 0,
) -> None:
    page.goto(entry_url, wait_until="domcontentloaded")
    wait_until_ready(page, timeout_seconds=timeout_seconds)


def _find_existing_page(contexts: list[BrowserContext], tab_url_contains: str) -> Page | None:
    for context in contexts:
        for page in context.pages:
            if tab_url_contains in page.url:
                return page
    return None


def wait_until_ready(page: Page, *, timeout_seconds: int = 0) -> None:
    deadline = None if timeout_seconds <= 0 else time.monotonic() + timeout_seconds
    last_hint_at = 0.0
    flash_reason: str | None = None

    while True:
        state = inspect_page_state(page)
        if state == "ready":
            stop_flash(page)
            return

        reason = "请在此浏览器中登录美问" if state == "login" else "请关闭页面中的弹窗/广告后保持在顾客列表页"
        if reason != flash_reason:
            start_flash(page, reason)
            flash_reason = reason

        now = time.monotonic()
        if now - last_hint_at > LOGIN_HINT_INTERVAL_SECONDS:
            print(f"[mei1-crawler] waiting: {reason}", flush=True)
            last_hint_at = now

        if deadline is not None and now > deadline:
            raise TimeoutError(f"Timed out waiting for page state: {state}")
        time.sleep(1)


def inspect_page_state(page: Page) -> str:
    try:
        body_text = page.locator("body").inner_text(timeout=2_000)
    except PlaywrightTimeoutError:
        return "loading"

    if _looks_logged_in(body_text) and not _has_blocking_overlay(page):
        return "ready"
    if _looks_login(body_text, page.url):
        return "login"
    return "popup_or_loading"


def start_flash(page: Page, message: str) -> None:
    page.bring_to_front()
    safe_message = re.sub(r"[^\w\u4e00-\u9fff：:/ -]", "", message)[:40]
    page.evaluate(
        """
        (message) => {
          if (window.__mei1FlashTimer) return;
          window.__mei1OriginalTitle = document.title || '美问';
          let visible = false;
          window.__mei1FlashTimer = setInterval(() => {
            visible = !visible;
            document.title = visible ? `【需要处理】${message}` : window.__mei1OriginalTitle;
          }, 700);
        }
        """,
        safe_message,
    )


def stop_flash(page: Page) -> None:
    try:
        page.evaluate(
            """
            () => {
              if (window.__mei1FlashTimer) {
                clearInterval(window.__mei1FlashTimer);
                window.__mei1FlashTimer = null;
              }
              if (window.__mei1OriginalTitle) {
                document.title = window.__mei1OriginalTitle;
              }
            }
            """
        )
    except Exception:
        pass


def _looks_logged_in(body_text: str) -> bool:
    return "顾客列表" in body_text and ("共搜到" in body_text or "顾客姓名/顾客编号/手机号" in body_text)


def _looks_login(body_text: str, url: str) -> bool:
    login_words = ("登录", "手机号", "验证码", "密码", "请输入账号", "请输入密码")
    if "顾客列表" in body_text:
        return False
    return "login" in url.lower() or sum(word in body_text for word in login_words) >= 2


def _has_blocking_overlay(page: Page) -> bool:
    selectors = [
        ".md-dialog-container",
        ".modal-dialog",
        ".ant-modal",
        ".el-dialog__wrapper",
        ".layui-layer",
        ".advertisement",
    ]
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if loc.is_visible(timeout=300):
                return True
        except Exception:
            continue
    return False
