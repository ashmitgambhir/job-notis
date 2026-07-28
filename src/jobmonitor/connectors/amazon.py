"""amazon.jobs search API.

Covers both Amazon and AWS; the two differ only by the ``business_category``
filter. Descriptions and qualifications come back inline, so one request per
page is all it takes.
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..core.models import JobPosting
from ..errors import ConnectorError
from .base import Connector

SEARCH_URL = "https://www.amazon.jobs/en/search.json"
PAGE_SIZE = 100
MAX_PAGES = 12


# `base_query` is an AND match across all terms, so one broad query has poor
# recall — "mechanical engineer intern" returns a single posting. Several
# narrow queries unioned together cover far more ground than one broad one,
# and each stays cheap.
DEFAULT_QUERIES = [
    "mechanical engineer",
    "mechanical engineering intern",
    "design engineer",
    "manufacturing engineer",
    "hardware development engineer",
    "engineering intern",
]


class AmazonConnector(Connector):
    name = "amazon"
    platform = "amazon_jobs"

    async def fetch_listings(self) -> List[JobPosting]:
        queries = self.params.get("queries") or DEFAULT_QUERIES
        if isinstance(queries, str):
            queries = [queries]
        # Amazon's board runs to tens of thousands of postings, so the whole
        # board is never pulled — only the slices that could plausibly match.
        by_id: Dict[str, JobPosting] = {}
        for query in queries:
            for posting in await self._search(query):
                by_id.setdefault(posting.external_id, posting)
        return list(by_id.values())

    async def _search(self, query: str) -> List[JobPosting]:
        # The API's own `business_category[]` filter silently returns zero
        # hits for valid values, so the category is applied client-side.
        wanted_category = self.params.get("business_category")
        postings: List[JobPosting] = []
        offset = 0

        for _ in range(MAX_PAGES):
            params: Dict[str, Any] = {
                "result_limit": PAGE_SIZE,
                "sort": "recent",
                "base_query": query,
                "offset": offset,
            }
            if self.params.get("country"):
                params["country[]"] = self.params["country"]

            payload, _cached = await self.http.get_json(
                SEARCH_URL, params=params, headers={"Accept": "application/json"}
            )
            if not isinstance(payload, dict):
                raise ConnectorError(
                    f"amazon.jobs returned {type(payload).__name__}", company=self.company.slug
                )
            jobs = payload.get("jobs") or []
            if not jobs:
                break

            for job in jobs:
                job_id = job.get("id_icims") or job.get("id")
                if not job_id:
                    continue
                if wanted_category and job.get("business_category") != wanted_category:
                    continue
                path = job.get("job_path") or ""
                postings.append(
                    self._posting(
                        external_id=str(job_id),
                        title=job.get("title") or "",
                        url=f"https://www.amazon.jobs{path}" if path else "https://www.amazon.jobs",
                        location=job.get("normalized_location") or job.get("location"),
                        department=job.get("business_category") or job.get("job_category"),
                        posted_at=self.parse_date(
                            job.get("posted_date") or job.get("updated_time")
                        ),
                        description=self._description(job),
                        detail_pending=False,
                        raw={
                            "job_family": job.get("job_family"),
                            # Amazon labels student roles explicitly; a much
                            # stronger signal than parsing the title.
                            "is_intern": job.get("is_intern"),
                        },
                    )
                )

            offset += PAGE_SIZE
            if offset >= int(payload.get("hits") or 0):
                break

        return postings

    def _description(self, job: Dict[str, Any]) -> str:
        parts = [
            job.get("description"),
            job.get("basic_qualifications"),
            job.get("preferred_qualifications"),
        ]
        return "\n".join(self.strip_html(p) for p in parts if p).strip()
