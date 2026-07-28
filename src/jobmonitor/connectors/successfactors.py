"""SAP SuccessFactors career sites.

Used by AGCO, ExxonMobil and Hershey. SuccessFactors career sites are
server-rendered and emit complete schema.org markup, which is both the most
stable and the cheapest source available — the OData API behind them needs
customer credentials.

The connector walks the paginated search page and harvests JSON-LD, falling
back to link scraping when a tenant has structured data switched off.
"""

from __future__ import annotations

import re
from typing import List, Optional
from urllib.parse import urljoin

import structlog
from bs4 import BeautifulSoup

from ..core.models import JobPosting
from ..errors import ConnectorError, PermanentError, TransientError
from .base import Connector
from .jsonld import find_job_postings, iter_jsonld_blocks, posting_from_jsonld

log = structlog.get_logger(__name__)
MAX_PAGES = 12
PAGE_SIZE = 25
_JOB_HREF = re.compile(r"/job/[^/]*?(\d{4,})", re.I)


class SuccessFactorsConnector(Connector):
    name = "successfactors"
    platform = "successfactors"
    required_params = ("host",)

    @property
    def _base(self) -> str:
        return f"https://{str(self.params['host']).replace('https://', '').strip('/')}"

    @property
    def _search_path(self) -> str:
        return str(self.params.get("search_path", "/search/"))

    async def fetch_listings(self) -> List[JobPosting]:
        postings: List[JobPosting] = []
        seen = set()

        for page in range(MAX_PAGES):
            params = {"startrow": page * PAGE_SIZE}
            if self.params.get("query"):
                params["q"] = self.params["query"]
            try:
                html, _ = await self.http.get_text(
                    urljoin(self._base, self._search_path),
                    params=params,
                    headers={"Accept": "text/html"},
                )
            except (PermanentError, TransientError) as exc:
                if page == 0:
                    raise ConnectorError(
                        f"successfactors site {self._base} unreachable: {exc}",
                        company=self.company.slug,
                    ) from exc
                break

            page_postings = self._from_jsonld(html) or self._from_links(html)
            fresh = [p for p in page_postings if p.job_key not in seen]
            if not fresh:
                break
            for posting in fresh:
                seen.add(posting.job_key)
            postings.extend(fresh)

        if not postings:
            raise ConnectorError(
                f"successfactors site {self._base} returned no parseable postings",
                company=self.company.slug,
            )
        return postings

    def _from_jsonld(self, html: str) -> List[JobPosting]:
        postings = []
        for block in iter_jsonld_blocks(html):
            for node in find_job_postings(block):
                posting = posting_from_jsonld(
                    node,
                    company=self.company.name,
                    slug=self.company.slug,
                    platform=self.platform,
                    fallback_url=self._base,
                )
                if posting:
                    postings.append(posting)
        return postings

    def _from_links(self, html: str) -> List[JobPosting]:
        soup = BeautifulSoup(html, "lxml")
        postings: List[JobPosting] = []
        for anchor in soup.select("a[href*='/job/']"):
            href = anchor.get("href") or ""
            match = _JOB_HREF.search(href)
            title = anchor.get_text(" ", strip=True)
            if not match or not title:
                continue
            postings.append(
                self._posting(
                    external_id=match.group(1),
                    title=title,
                    url=urljoin(self._base, href),
                    location=self._row_field(anchor, "location"),
                    department=self._row_field(anchor, "department|category|function"),
                    detail_pending=True,
                )
            )
        return postings

    @staticmethod
    def _row_field(anchor, pattern: str) -> Optional[str]:
        row = anchor.find_parent("tr") or anchor.parent
        if row is None:
            return None
        cell = row.find(class_=re.compile(pattern, re.I))
        return cell.get_text(" ", strip=True) if cell else None

    async def fetch_detail(self, posting: JobPosting) -> JobPosting:
        try:
            html, _ = await self.http.get_text(posting.url, headers={"Accept": "text/html"})
        except (PermanentError, TransientError):
            posting.detail_pending = False
            return posting

        for block in iter_jsonld_blocks(html):
            for node in find_job_postings(block):
                if node.get("description"):
                    posting.description = self.strip_html(str(node["description"]))
                    posting.posted_at = posting.posted_at or self.parse_date(node.get("datePosted"))
                    posting.detail_pending = False
                    return posting

        soup = BeautifulSoup(html, "lxml")
        container = (
            soup.find(class_=re.compile("jobdescription|job-description", re.I)) or soup.body
        )
        if container:
            posting.description = container.get_text(" ", strip=True)[:20000]
        posting.detail_pending = False
        return posting
