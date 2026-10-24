"""``BrowserTab``: the one visible gmgn.ai tab of the browser bridge (G25).

The official Playwright Chromium, opened by ``launch_persistent_context`` with a visible
window and nothing else changed: no extra launch flag, no script injected before the page,
no fingerprint or identity change, no captcha solving. The profile folder lives in the
user's home and keeps the session the user may start by signing in by hand in this window;
this module never reads, sets, exports or logs a cookie.

A request is the page's own ``fetch`` of a relative gmgn.ai address, run inside the tab, so
the site sees its own page asking. A Cloudflare challenge is waited for (the user may click
it in the window), never solved; a captcha or a block ends the load with a pause.
"""

import asyncio
import contextlib
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from gmgn_service.extract import PageState, classify_page
from gmgn_service.page import document_state
from gmgn_service.service import PageUnavailable
from gmgn_service.webapi import WebAnswer, WebRequest

_TEXT_LIMIT = 2_000_000
_POLL_SECONDS = 0.5
log = logging.getLogger("gmgn.tab")

# The page's own fetch of a relative address; a JSON body only for POST, as the page sends it.
_FETCH = """
async ([path, method, body, timeoutMs, limit]) => {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const init = {method, signal: controller.signal};
    if (body !== null) {
      init.body = body;
      init.headers = {'content-type': 'application/json'};
    }
    const response = await fetch(path, init);
    const text = await response.text();
    const type = response.headers.get('content-type') || '';
    return {status: response.status, type, text: text.slice(0, limit), timedOut: false};
  } catch (error) {
    return {status: 0, type: '', text: '', timedOut: error && error.name === 'AbortError'};
  } finally {
    clearTimeout(timer);
  }
}
"""

# Signed out: the page shows its own "Log In" / "Sign Up" buttons. Only visible button texts
# are looked at - no storage, no cookie.
_LOGIN_PROBE = """
() => {
  const words = new Set(['log in', 'login', 'sign in', 'sign up', 'войти', 'регистрация']);
  const visible = Array.from(document.querySelectorAll('button, a, [role="button"]'))
    .filter((item) => item.offsetParent !== null);
  const texts = visible.map((item) => (item.innerText || '').trim().toLowerCase());
  return {buttons: visible.length, signedOut: texts.some((text) => words.has(text))};
}
"""


class BrowserTab:
    def __init__(
        self,
        *,
        base_url: str,
        profile_dir: Path,
        page_timeout: float,
        request_timeout: float = 20.0,
        challenge_wait: float = 30.0,
    ) -> None:
        self._home = f"{base_url}/?chain=sol"
        self._host = urlsplit(base_url).hostname or ""
        self._profile_dir = profile_dir
        self._page_timeout = page_timeout
        self._request_timeout = request_timeout
        self._challenge_wait = challenge_wait
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._reload = False
        self.closed = asyncio.Event()

    @property
    def ready(self) -> bool:
        return self._context is not None and not self.closed.is_set()

    async def start(self) -> None:
        """Opens the window on gmgn.ai; a page that does not load yet is not fatal."""

        self._profile_dir.mkdir(parents=True, exist_ok=True)
        profile = str(self._profile_dir)
        self._playwright = await async_playwright().start()
        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                profile, headless=False
            )
        except PlaywrightError:
            await self._playwright.stop()
            self._playwright = None
            raise RuntimeError(
                "the Chromium window did not open: install the browser once with "
                "'uv run --frozen playwright install chromium' in services/gmgn"
            ) from None
        self._context.on("close", lambda _context: self.closed.set())
        try:
            await self._open(await self._tab())
        except PageUnavailable as unavailable:
            log.warning("gmgn.ai did not open yet: %s", unavailable.reason)

    async def close(self) -> None:
        if self._context is not None:
            with contextlib.suppress(PlaywrightError):
                await self._context.close()
            self._context = None
        if self._playwright is not None:
            with contextlib.suppress(PlaywrightError):
                await self._playwright.stop()
            self._playwright = None
        self.closed.set()

    def mark_blocked(self) -> None:
        self._reload = True

    async def prepare(self) -> None:
        if not self.ready:
            raise PageUnavailable("BROWSER_CLOSED")
        try:
            page = await self._tab()
            if self._reload or urlsplit(page.url).hostname != self._host:
                await self._open(page)
        except PlaywrightError:
            raise PageUnavailable("BROWSER_ERROR") from None
        deadline = time.monotonic() + self._challenge_wait
        while True:
            title, text, frames = await document_state(page)
            state = classify_page(title, text, frames)
            if state is PageState.READY:
                return
            if state is PageState.BLOCKED or state is PageState.CAPTCHA:
                self._reload = True
                reason = "ACCESS_BLOCKED" if state is PageState.BLOCKED else "CAPTCHA"
                raise PageUnavailable(reason, pause=True)
            if time.monotonic() >= deadline:
                self._reload = True
                raise PageUnavailable("CHALLENGE", pause=True)
            await asyncio.sleep(_POLL_SECONDS)

    async def request(self, request: WebRequest) -> WebAnswer:
        page = self._page
        if page is None or not self.ready:
            return WebAnswer(0, "", "")
        arguments = [
            request.path,
            request.method,
            request.body,
            int(self._request_timeout * 1000),
            _TEXT_LIMIT,
        ]
        try:
            result = await asyncio.wait_for(
                page.evaluate(_FETCH, arguments), self._request_timeout + 5
            )
        except TimeoutError:
            return WebAnswer(0, "", "", timed_out=True)
        except PlaywrightError:
            return WebAnswer(0, "", "")
        return _answer(result)

    async def refresh_login(self) -> str:
        page = self._page
        if page is None or not self.ready:
            return "UNKNOWN"
        title, text, frames = await document_state(page)
        if not title or classify_page(title, text, frames) is not PageState.READY:
            return "UNKNOWN"
        try:
            probe = await page.evaluate(_LOGIN_PROBE)
        except PlaywrightError:
            return "UNKNOWN"
        if not isinstance(probe, dict) or not isinstance(probe.get("buttons"), int):
            return "UNKNOWN"
        if probe.get("signedOut") is True:
            return "LOGGED_OUT"
        # A page that has not drawn its header yet says nothing either way.
        return "LOGGED_IN" if probe["buttons"] >= 5 else "UNKNOWN"

    async def _tab(self) -> Page:
        """The bridge's tab: the first open page of the window, a new one if all closed."""

        if self._page is not None and not self._page.is_closed():
            return self._page
        if self._context is None:
            raise PageUnavailable("BROWSER_CLOSED")
        pages = [page for page in self._context.pages if not page.is_closed()]
        self._page = pages[0] if pages else await self._context.new_page()
        return self._page

    async def _open(self, page: Page) -> None:
        try:
            await page.goto(
                self._home, wait_until="domcontentloaded", timeout=self._page_timeout * 1000
            )
            with contextlib.suppress(PlaywrightTimeoutError):
                await page.wait_for_load_state("load", timeout=self._page_timeout * 1000)
        except PlaywrightTimeoutError:
            raise PageUnavailable("PAGE_TIMEOUT") from None
        except PlaywrightError:
            raise PageUnavailable("NETWORK_ERROR") from None
        self._reload = False


def _answer(result: Any) -> WebAnswer:
    if not isinstance(result, dict):
        return WebAnswer(0, "", "")
    status, kind, text = result.get("status"), result.get("type"), result.get("text")
    return WebAnswer(
        status if isinstance(status, int) and not isinstance(status, bool) else 0,
        kind if isinstance(kind, str) else "",
        text if isinstance(text, str) else "",
        timed_out=result.get("timedOut") is True,
    )
