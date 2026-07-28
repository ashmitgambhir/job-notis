"""iCIMS career portals.

Used by Rivian and Navistar. iCIMS has no public JSON API — the REST API needs
customer credentials — but the public search page is a stable, paginated HTML
list. It also refuses to render unless it believes it is inside the customer's
iframe, hence the ``in_iframe=1`` parameter and the Referer header.
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

log = structlog.get_logger(__name__)
MAX_PAGES = 15
_JOB_HREF = re.compile(r"/jobs/(\d+)/", re.I)


class IcimsConnector(Connector):
    name = "icims"
    platform = "icims"
    required_params = ("tenant",)

    @property
    def _base(self) -> str:
        tenant = str(self.params["tenant"]).replace(".icims.com", "").strip("/")
        return f"https://{tenant}.icims.com"

    async def fetch_listings(self) -> List[JobPosting]:
        postings: List[JobPosting] = []
        seen = set()

        for page in range(1, MAX_PAGES + 1):
            params = {
                "ss": 1,
                "in_iframe": 1,
                "pr": page,
                "searchRelation": "keyword_all",
            }
            if self.params.get("query"):
                params["searchKeyword"] = self.params["query"]
            try:
                html, _ = await self.http.get_text(
                    f"{self._base}/jobs/search",
                    params=params,
                    headers={
                        "Accept": "text/html",
                        # iCIMS bounces requests that do not look framed.
                        "Referer": self.company.careers_url or self._base,
                    },
                )
            except (PermanentError, TransientError) as exc:
                if page == 1:
                    raise ConnectorError(
                        f"icims portal {self._base} unreachable: {exc}",
                        company=self.company.slug,
                    ) from exc
                break

            page_postings = self._parse(html)
            fresh = [p for p in page_postings if p.job_key not in seen]
            if not fresh:
                break
            for posting in fresh:
                seen.add(posting.job_key)
            postings.extend(fresh)

        if not postings:
            raise ConnectorError(
                f"icims portal {self._base} returned no parseable postings",
                company=self.company.slug,
            )
        return postings

    def _parse(self, html: str) -> List[JobPosting]:
        soup = BeautifulSoup(html, "lxml")
        postings: List[JobPosting] = []

        for anchor in soup.select("a[href*='/jobs/']"):
            href = anchor.get("href") or ""
            match = _JOB_HREF.search(href)
            if not match:
                continue
            title = anchor.get_text(strip=True)
            if not title or len(title) < 3:
                continue

            postings.append(
                self._posting(
                    external_id=match.group(1),
                    title=title,
                    url=urljoin(self._base, href),
                    location=self._sibling_text(anchor, ("job-location", "location")),
                    department=self._sibling_text(anchor, ("job-category", "category")),
                    detail_pending=True,
                )
            )
        return postings

    @staticmethod
    def _sibling_text(anchor, class_hints) -> Optional[str]:
        """Find a labelled field near the job link.

        iCIMS markup varies per tenant, so match on class-name substrings and
        walk up a couple of levels rather than assuming one structure.
        """
        node = anchor
        for _ in range(3):
            node = node.parent
            if node is None:
                return None
            for hint in class_hints:
                found = node.find(class_=re.compile(hint, re.I))
                if found:
                    text = found.get_text(" ", strip=True)
                    if text:
                        return text
        return None

    async def fetch_detail(self, posting: JobPosting) -> JobPosting:
        try:
            html, _ = await self.http.get_text(
                posting.url,
                params={"in_iframe": 1},
                headers={"Accept": "text/html", "Referer": self._base},
            )
        except (PermanentError, TransientError):
            posting.detail_pending = False
            return posting

        soup = BeautifulSoup(html, "lxml")
        container = (
            soup.find(class_=re.compile("iCIMS_JobContent|job-description", re.I))
            or soup.find("main")
            or soup.body
        )
        if container:
            posting.description = container.get_text(" ", strip=True)[:20000]
        posting.detail_pending = False
        return posting
