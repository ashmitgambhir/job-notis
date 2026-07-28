"""Avature CRM career portals.

Used by Siemens. Avature tenants are configured per customer with no shared
API, but every one of them ships schema.org markup for Google Jobs. This
connector therefore walks the paginated search page and harvests JSON-LD.
"""

from __future__ import annotations

from typing import List
from urllib.parse import urljoin

from ..core.models import JobPosting
from ..errors import ConnectorError, PermanentError, TransientError
from .base import Connector
from .jsonld import find_job_postings, iter_jsonld_blocks, posting_from_jsonld

MAX_PAGES = 12


class AvatureConnector(Connector):
    name = "avature"
    platform = "avature"
    required_params = ("host",)

    @property
    def _base(self) -> str:
        return f"https://{str(self.params['host']).replace('https://', '').strip('/')}"

    async def fetch_listings(self) -> List[JobPosting]:
        search_path = str(self.params.get("search_path", "/careers/SearchJobs"))
        postings: List[JobPosting] = []
        seen = set()

        for page in range(1, MAX_PAGES + 1):
            params = {"jobRecordsPerPage": 50, "jobOffset": (page - 1) * 50}
            if self.params.get("query"):
                params["searchByKeyword"] = self.params["query"]
            try:
                html, _ = await self.http.get_text(
                    urljoin(self._base, search_path),
                    params=params,
                    headers={"Accept": "text/html"},
                )
            except (PermanentError, TransientError) as exc:
                if page == 1:
                    raise ConnectorError(
                        f"avature portal {self._base} unreachable: {exc}",
                        company=self.company.slug,
                    ) from exc
                break

            fresh = []
            for block in iter_jsonld_blocks(html):
                for node in find_job_postings(block):
                    posting = posting_from_jsonld(
                        node,
                        company=self.company.name,
                        slug=self.company.slug,
                        platform=self.platform,
                        fallback_url=self._base,
                    )
                    if posting and posting.job_key not in seen:
                        seen.add(posting.job_key)
                        fresh.append(posting)

            if not fresh:
                break
            postings.extend(fresh)

        if not postings:
            raise ConnectorError(
                f"avature portal {self._base} exposed no schema.org postings",
                company=self.company.slug,
            )
        return postings
