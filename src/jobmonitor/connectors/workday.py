"""Workday CXS API.

Verified against Boeing, Abbott, ITW, NVIDIA and Intel. Workday is the single
most common platform among large industrial employers, so this connector
carries the most weight in the whole system.

The endpoint is a POST to::

    https://{tenant}.{pod}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs

Workday only exposes a fuzzy ``postedOn`` string ("Posted Today", "Posted 30+
Days Ago"), so real posting timestamps come from the detail endpoint, which
returns an ISO ``startDate``. That is another reason detail fetches are
reserved for postings that already look relevant.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from ..core.models import JobPosting
from ..errors import ConnectorError
from .base import Connector

# Workday rejects limit > 20 with a 400, so 20 it is.
PAGE_SIZE = 20
# Ceiling for the seeding pass only. After that, early-stop paging means a
# typical cycle costs one or two requests regardless of board size.
MAX_PAGES = 60


class WorkdayConnector(Connector):
    name = "workday"
    platform = "workday"
    required_params = ("tenant", "site")
    # Confirmed against Boeing's board: offset 0 is "Posted Today" and the
    # postings get monotonically older as the offset grows.
    ordered_by_recency = True

    @property
    def _tenant(self) -> str:
        return str(self.params["tenant"])

    @property
    def _pod(self) -> str:
        return str(self.params.get("pod", "wd1"))

    @property
    def _site(self) -> str:
        return str(self.params["site"])

    @property
    def _host(self) -> str:
        # Some tenants are only reachable on their vanity domain.
        custom = self.params.get("host")
        if custom:
            return str(custom).replace("https://", "").replace("http://", "").strip("/")
        return f"{self._tenant}.{self._pod}.myworkdayjobs.com"

    @property
    def _api_url(self) -> str:
        return f"https://{self._host}/wday/cxs/{self._tenant}/{self._site}/jobs"

    @property
    def _public_root(self) -> str:
        return f"https://{self._host}/{self._site}"

    async def fetch_listings(self) -> List[JobPosting]:
        # A search term cuts thousands of postings down to the relevant slice
        # before they ever cross the wire. Set to "" to pull an entire board.
        search_text = self.params.get("search_text", "")
        applied_facets: Dict[str, Any] = self.params.get("applied_facets") or {}

        postings: List[JobPosting] = []
        seen_ids = set()
        offset = 0
        # Workday reports `total` only on the first page; every later page
        # carries `total: 0`. Trusting it per-page stops paging after 40
        # postings, so capture it once and hold on to it.
        total: Optional[int] = None

        max_pages = int(self.params.get("max_pages") or MAX_PAGES)
        for pages_done in range(max_pages):
            body = {
                "appliedFacets": applied_facets,
                "limit": PAGE_SIZE,
                "offset": offset,
                "searchText": search_text,
            }
            payload, _cached = await self.http.post_json(
                self._api_url,
                body,
                headers={"Accept": "application/json"},
            )
            if not isinstance(payload, dict):
                raise ConnectorError(
                    f"workday tenant '{self._tenant}' returned {type(payload).__name__}",
                    company=self.company.slug,
                )
            page = payload.get("jobPostings") or []
            if not page:
                break
            if total is None:
                try:
                    total = int(payload.get("total") or 0)
                except (TypeError, ValueError):
                    total = 0

            page_ids = []
            for job in page:
                external_path = job.get("externalPath") or ""
                job_id = self._external_id(job, external_path)
                if not job_id:
                    continue
                page_ids.append(job_id)
                if job_id in seen_ids:
                    continue
                seen_ids.add(job_id)
                postings.append(
                    self._posting(
                        external_id=job_id,
                        title=job.get("title") or "",
                        url=(
                            self._public_root + external_path
                            if external_path
                            else self._public_root
                        ),
                        location=job.get("locationsText"),
                        posted_at=self.parse_date(job.get("postedOn")),
                        detail_pending=True,
                        raw={
                            "external_path": external_path,
                            "time_type": job.get("timeType"),
                            "posted_on": job.get("postedOn"),
                        },
                    )
                )

            # Everything on this page is already known, and the board is
            # newest-first, so nothing newer can be hiding further down.
            if self.should_stop_paging(page_ids, pages_done):
                break

            offset += PAGE_SIZE
            if total and offset >= total:
                break
            # A short page means the board is exhausted regardless of `total`.
            if len(page) < PAGE_SIZE:
                break

        return postings

    @staticmethod
    def _external_id(job: Dict[str, Any], external_path: str) -> Optional[str]:
        """Prefer the requisition number; fall back to the URL path.

        ``bulletFields`` normally holds the requisition ID (JR2015626), which is
        stable across re-indexing in a way the path is not.
        """
        bullets = job.get("bulletFields") or []
        if bullets and isinstance(bullets, list) and bullets[0]:
            return str(bullets[0])
        if external_path:
            return external_path.rsplit("/", 1)[-1]
        return None

    async def fetch_detail(self, posting: JobPosting) -> JobPosting:
        external_path = posting.raw.get("external_path")
        if not external_path:
            posting.detail_pending = False
            return posting

        url = f"https://{self._host}/wday/cxs/{self._tenant}/{self._site}{external_path}"
        payload, _ = await self.http.get_json(url, headers={"Accept": "application/json"})
        info = (payload or {}).get("jobPostingInfo") or {}

        posting.description = self.strip_html(info.get("jobDescription"))
        posting.department = (
            posting.department or info.get("jobFamily") or info.get("jobFamilyGroup")
        )
        posting.location = info.get("location") or posting.location
        # The detail endpoint has a real timestamp, unlike the listing.
        real_date = self.parse_date(info.get("startDate") or info.get("postedOn"))
        if real_date:
            posting.posted_at = real_date
        if info.get("endDate"):
            posting.deadline = self.parse_date(info.get("endDate"))
        if info.get("externalUrl"):
            posting.url = info["externalUrl"]
        posting.raw["time_type"] = info.get("timeType") or posting.raw.get("time_type")
        posting.raw["remote_type"] = info.get("remoteType")
        posting.detail_pending = False
        return posting

    @staticmethod
    def parse_host(url: str) -> Optional[Dict[str, str]]:
        """Pull tenant/pod out of a myworkdayjobs URL, for auto-discovery."""
        host = urlparse(url).netloc
        if "myworkdayjobs.com" not in host:
            return None
        parts = host.split(".")
        if len(parts) < 3:
            return None
        return {"tenant": parts[0], "pod": parts[1], "host": host}
