"""``BrowserPageFetcher``: one visit of the public token page, as a plain visitor.

Official Playwright Chromium with its defaults: ``chromium.launch()`` (headless, no extra
arguments), a fresh ``browser.new_context()`` per visit (no cookies, no stored state, no
account) and no change to the user agent or any other fingerprint. No captcha is solved
and no challenge is worked around: a block or a captcha ends the visit at once, and a
challenge that does not clear by itself within ``page_timeout`` is ``CHALLENGE_TIMEOUT``.

The metrics come only from the page's own network answers (``extract.INTERESTING_ENDPOINTS``);
the service sends no request of its own to the GMGN API.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from playwright.async_api import Browser, Page, Playwright, Response, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from gmgn_service.config import ServiceSettings
from gmgn_service.extract import (
    CapturedResponse,
    Extraction,
    PageState,
    classify_page,
    extract_signals,
    is_interesting,
    parse_body,
)
from gmgn_service.service import PageUnavailable

_POLL_SECONDS = 0.5
_TEXT_LIMIT = 4000
log = logging.getLogger("gmgn.page")


@dataclass
class Visit:
    """What one visit saw: the page state, the captured answers and the metrics in them."""

    url: str
    state: PageState = PageState.READY
    title: str = ""
    reason: str | None = None
    captured: list[CapturedResponse] = field(default_factory=list)
    statuses: list[int] = field(default_factory=list)
    extraction: Extraction | None = None
    elapsed: float = 0.0


class BrowserPageFetcher:
    def __init__(self, settings: ServiceSettings) -> None:
        self._settings = settings
        self._host = urlsplit(settings.base_url).hostname or ""
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._launch_lock = asyncio.Lock()

    @property
    def ready(self) -> bool:
        return self._browser is not None and self._browser.is_connected()

    async def start(self) -> None:
        try:
            await self._ensure_browser()
        except PageUnavailable:
            log.error("browser_launch_failed")

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def fetch(self, chain: str, mint: str) -> Extraction:
        visit = await self.visit(chain, mint)
        log.info(
            "page_visit mint=%s state=%s reason=%s elapsed=%.1fs",
            mint,
            visit.state.value,
            visit.reason,
            visit.elapsed,
        )
        if visit.reason is not None or visit.extraction is None:
            raise PageUnavailable(visit.reason or "NO_SIGNALS")
        return visit.extraction

    async def visit(self, chain: str, mint: str) -> Visit:
        browser = await self._ensure_browser()
        visit = Visit(url=f"{self._settings.base_url}/{chain}/token/{mint}")
        started = time.monotonic()
        context = await browser.new_context()
        reads: set[asyncio.Task[None]] = set()
        try:
            page = await context.new_page()

            def on_response(response: Response) -> None:
                task = asyncio.ensure_future(self._capture(response, visit))
                reads.add(task)
                task.add_done_callback(reads.discard)

            page.on("response", on_response)
            await self._observe(page, visit, chain, mint, started)
        finally:
            for task in reads:
                task.cancel()
            await context.close()
            visit.elapsed = time.monotonic() - started
        return visit

    async def _observe(
        self, page: Page, visit: Visit, chain: str, mint: str, started: float
    ) -> None:
        settings = self._settings
        deadline = started + settings.page_timeout
        try:
            await page.goto(
                visit.url, wait_until="domcontentloaded", timeout=settings.page_timeout * 1000
            )
        except PlaywrightTimeoutError:
            visit.reason = "PAGE_TIMEOUT"
            return
        except PlaywrightError:
            visit.reason = "NETWORK_ERROR"
            return
        first_metrics: float | None = None
        while True:
            visit.title, text, frames = await document_state(page)
            visit.state = classify_page(visit.title, text, frames)
            if visit.state is PageState.BLOCKED:
                visit.reason = "ACCESS_BLOCKED"
                return
            if visit.state is PageState.CAPTCHA:
                visit.reason = "CAPTCHA"
                return
            visit.extraction = extract_signals(visit.captured, chain=chain, mint=mint)
            now = time.monotonic()
            if visit.extraction.complete:
                return
            if not visit.extraction.empty:
                first_metrics = first_metrics or now
                if now - first_metrics >= settings.settle:
                    return
            if now >= deadline:
                break
            await asyncio.sleep(_POLL_SECONDS)
        if visit.extraction is not None and not visit.extraction.empty:
            return
        if visit.state is PageState.CHALLENGE:
            visit.reason = "CHALLENGE_TIMEOUT"
        elif 429 in visit.statuses:
            visit.reason = "RATE_LIMITED"
        elif any(status in (401, 403) for status in visit.statuses):
            visit.reason = "API_BLOCKED"
        else:
            visit.reason = "NO_SIGNALS"

    async def _capture(self, response: Response, visit: Visit) -> None:
        parts = urlsplit(response.url)
        if parts.hostname != self._host or not is_interesting(parts.path):
            return
        if response.request.resource_type not in ("xhr", "fetch"):
            return
        visit.statuses.append(response.status)
        try:
            text = await response.text()
        except PlaywrightError:
            return
        visit.captured.append(
            CapturedResponse(
                method=response.request.method,
                path=parts.path,
                status=response.status,
                body=parse_body(text),
            )
        )

    async def _ensure_browser(self) -> Browser:
        async with self._launch_lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            try:
                if self._playwright is None:
                    self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch()
            except PlaywrightError as error:
                self._browser = None
                raise PageUnavailable("BROWSER_UNAVAILABLE") from error
            return self._browser


async def document_state(page: Page) -> tuple[str, str, list[str]]:
    """Title, visible text and frame URLs; empty while the page is navigating."""

    try:
        title = await page.title()
        text = await page.evaluate(
            f"() => document.body ? document.body.innerText.slice(0, {_TEXT_LIMIT}) : ''"
        )
    except PlaywrightError:
        return "", "", []
    return title, text if isinstance(text, str) else "", [frame.url for frame in page.frames]
