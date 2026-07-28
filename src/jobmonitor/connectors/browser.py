"""Playwright fallback — the connector of last resort.

Reserved for sites that hard-block plain HTTP clients (Tesla and Apple both do)
or render the entire board client-side with no reachable JSON.

It is last for good reasons: a browser costs roughly a hundred times the memory
and latency of an API call, needs a system Chromium, and is the most likely
part of the stack to break unattended. If you find yourself adding companies
here, it is worth one more attempt at finding their XHR endpoint first —
open devtools, filter by Fetch/XHR, and look for the request the page makes.

The extra ``playwright`` install is optional; without it this connector raises
a clear error rather than failing at import time.
"""

from __future__ import annotations

import asyncio
from typing import Any, List, Optional

import structlog

from ..core.models import JobPosting
from ..errors import ConnectorError
from .base import Connector
from .jsonld import find_job_postings, iter_jsonld_blocks, posting_from_jsonld

log = structlog.get_logger(__name__)

# One browser per process, shared across companies. Launching Chromium per
# company would dominate the cycle budget.
_browser_lock = asyncio.Lock()
_browser: Any = None
_playwright: Any = None


async def _get_browser() -> Any:
    global _browser, _playwright
    async with _browser_lock:
        if _browser is not None:
            return _browser
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise ConnectorError(
                "the browser connector needs the optional extra: "
                "pip install 'jobmonitor[browser]' && playwright install chromium"
            ) from exc
        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        log.info("browser_launched")
        return _browser


async def shutdown_browser() -> None:
    """Called during orchestrator teardown so containers exit cleanly."""
    global _browser, _playwright
    async with _browser_lock:
        if _browser is not None:
            await _browser.close()
            _browser = None
        if _playwright is not None:
            await _playwright.stop()
            _playwright = None


class BrowserConnector(Connector):
    name = "browser"
    platform = "browser"
    required_params = ("url",)

    async def fetch_listings(self) -> List[JobPosting]:
        browser = await _get_browser()
        url = str(self.params["url"])
        wait_selector: Optional[str] = self.params.get("wait_for")
        # Sites that lazy-load results need a scroll to populate the list.
        scrolls = int(self.params.get("scrolls", 3))

        context = await browser.new_context(
            user_agent=self.http.settings.user_agent,
            viewport={"width": 1440, "height": 900},
            locale="en-US",
        )
        try:
            page = await context.new_page()
            # Images and fonts are pure cost here.
            await page.route(
                "**/*",
                lambda route: asyncio.ensure_future(
                    route.abort()
                    if route.request.resource_type in ("image", "font", "media")
                    else route.continue_()
                ),
            )
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            if wait_selector:
                try:
                    await page.wait_for_selector(wait_selector, timeout=20000)
                except Exception:
                    log.warning("browser_selector_timeout", selector=wait_selector, url=url)

            for _ in range(scrolls):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(900)

            html = await page.content()
        finally:
            await context.close()

        postings = self._from_jsonld(html, url)
        if postings:
            return postings

        # Reuse the heuristic parser rather than duplicating link logic.
        from .html_generic import HtmlConnector

        helper = HtmlConnector.__new__(HtmlConnector)
        helper.company = self.company
        helper.http = self.http
        helper.params = {"url": url, "selectors": self.params.get("selectors") or {}}
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        selectors = self.params.get("selectors") or {}
        postings = (
            helper._with_selectors(soup, selectors, url)
            if selectors
            else helper._heuristic(soup, url)
        )
        for posting in postings:
            posting.source_platform = self.platform

        if not postings:
            raise ConnectorError(
                f"browser render of {url} produced no recognisable postings",
                company=self.company.slug,
            )
        return postings

    def _from_jsonld(self, html: str, url: str) -> List[JobPosting]:
        postings = []
        for block in iter_jsonld_blocks(html):
            for node in find_job_postings(block):
                posting = posting_from_jsonld(
                    node,
                    company=self.company.name,
                    slug=self.company.slug,
                    platform=self.platform,
                    fallback_url=url,
                )
                if posting:
                    postings.append(posting)
        return postings

    async def fetch_detail(self, posting: JobPosting) -> JobPosting:
        browser = await _get_browser()
        context = await browser.new_context(user_agent=self.http.settings.user_agent)
        try:
            page = await context.new_page()
            await page.goto(posting.url, wait_until="domcontentloaded", timeout=45000)
            html = await page.content()
        finally:
            await context.close()

        for block in iter_jsonld_blocks(html):
            for node in find_job_postings(block):
                if node.get("description"):
                    posting.description = self.strip_html(str(node["description"]))
                    posting.detail_pending = False
                    return posting

        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        body = soup.find("main") or soup.body
        if body:
            posting.description = body.get_text(" ", strip=True)[:20000]
        posting.detail_pending = False
        return posting
