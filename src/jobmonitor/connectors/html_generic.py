"""Heuristic HTML connector — the last stop before a browser.

This exists for career portals with no API and no structured data. It is
deliberately the least-preferred option: CSS selectors break on every site
redesign, whereas the API connectors above break approximately never.

Configure ``selectors`` in companies.yaml to pin the parse for a specific site;
without them the connector guesses from link shape, which works often enough to
be worth trying but should be treated as temporary.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

import structlog
from bs4 import BeautifulSoup

from ..core.models import JobPosting
from ..errors import ConnectorError
from .base import Connector

log = structlog.get_logger(__name__)

# Paths that almost always denote an individual posting.
_JOB_PATH = re.compile(
    r"/(job|jobs|career|careers|opening|openings|position|positions|vacancy|vacancies|req)"
    r"[/-][A-Za-z0-9._~-]{2,}",
    re.I,
)
# Navigation and boilerplate that matches the path pattern but is not a job.
_NOT_A_JOB = re.compile(
    r"(search|login|sign-?in|apply-?now|privacy|cookie|terms|benefits|culture|"
    r"faq|alert|rss|feed|category|location|department|page=|#)",
    re.I,
)


class HtmlConnector(Connector):
    name = "html"
    platform = "html"
    required_params = ("url",)

    async def fetch_listings(self) -> List[JobPosting]:
        urls = self.params.get("urls") or [self.params["url"]]
        selectors: Dict[str, str] = self.params.get("selectors") or {}
        postings: List[JobPosting] = []
        seen = set()

        for url in urls:
            html, _ = await self.http.get_text(url, headers={"Accept": "text/html"})
            soup = BeautifulSoup(html, "lxml")
            found = (
                self._with_selectors(soup, selectors, url)
                if selectors
                else self._heuristic(soup, url)
            )
            for posting in found:
                if posting.job_key not in seen:
                    seen.add(posting.job_key)
                    postings.append(posting)

        if not postings:
            raise ConnectorError(
                f"no postings recognised in HTML at {urls[0]}; "
                "configure `selectors` or switch this company to the browser connector",
                company=self.company.slug,
            )
        return postings

    def _with_selectors(
        self, soup: BeautifulSoup, selectors: Dict[str, str], base_url: str
    ) -> List[JobPosting]:
        postings = []
        for node in soup.select(selectors.get("item", "")):
            title_node = node.select_one(selectors["title"]) if selectors.get("title") else node
            link_node = node.select_one(selectors.get("link", "a")) or node.find("a")
            if title_node is None or link_node is None:
                continue
            href = link_node.get("href") or ""
            title = title_node.get_text(" ", strip=True)
            if not title or not href:
                continue
            url = urljoin(base_url, href)
            postings.append(
                self._posting(
                    external_id=self._id_from_url(url),
                    title=title,
                    url=url,
                    location=self._sub_text(node, selectors.get("location")),
                    department=self._sub_text(node, selectors.get("department")),
                    detail_pending=True,
                )
            )
        return postings

    def _heuristic(self, soup: BeautifulSoup, base_url: str) -> List[JobPosting]:
        base_host = urlparse(base_url).netloc
        postings = []
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            if not _JOB_PATH.search(href) or _NOT_A_JOB.search(href):
                continue
            url = urljoin(base_url, href)
            # Stay on the employer's own domain; outbound links are aggregators.
            if urlparse(url).netloc not in (base_host, ""):
                continue
            title = anchor.get_text(" ", strip=True)
            # Real titles are a few words; anything longer is a card blob.
            if not (5 <= len(title) <= 140):
                continue
            postings.append(
                self._posting(
                    external_id=self._id_from_url(url),
                    title=title,
                    url=url,
                    detail_pending=True,
                )
            )
        return postings

    @staticmethod
    def _sub_text(node: Any, selector: Optional[str]) -> Optional[str]:
        if not selector:
            return None
        found = node.select_one(selector)
        return found.get_text(" ", strip=True) if found else None

    @staticmethod
    def _id_from_url(url: str) -> str:
        path = urlparse(url).path.rstrip("/")
        digits = re.findall(r"(\d{4,})", path)
        # A numeric requisition ID is stable; a slug changes when the title is
        # edited, which would look like a brand-new posting.
        return digits[-1] if digits else path.rsplit("/", 1)[-1] or url

    async def fetch_detail(self, posting: JobPosting) -> JobPosting:
        html, _ = await self.http.get_text(posting.url, headers={"Accept": "text/html"})
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        container = (
            soup.find(class_=re.compile("job-?description|jobDescription|posting", re.I))
            or soup.find("main")
            or soup.find("article")
            or soup.body
        )
        if container:
            posting.description = container.get_text(" ", strip=True)[:20000]
        posting.detail_pending = False
        return posting
