"""Jobvite job feeds.

Jobvite publishes a public JSON feed per company at
``https://jobs.jobvite.com/api/v1/jobs?companyId={id}``. Older tenants only
serve the ``/careers`` HTML, which carries schema.org markup, so that is the
fallback.
"""

from __future__ import annotations

from typing import List

from ..core.models import JobPosting
from ..errors import ConnectorError, PermanentError, TransientError
from .base import Connector
from .jsonld import find_job_postings, iter_jsonld_blocks, posting_from_jsonld


class JobviteConnector(Connector):
    name = "jobvite"
    platform = "jobvite"
    required_params = ("company_id",)

    async def fetch_listings(self) -> List[JobPosting]:
        company_id = str(self.params["company_id"])
        try:
            payload, _ = await self.http.get_json(
                "https://jobs.jobvite.com/api/v1/jobs",
                params={"companyId": company_id, "count": 500},
                headers={"Accept": "application/json"},
            )
        except (PermanentError, TransientError):
            return await self._fallback(company_id)

        jobs = (payload or {}).get("jobs") if isinstance(payload, dict) else None
        if not jobs:
            return await self._fallback(company_id)

        postings: List[JobPosting] = []
        for job in jobs:
            job_id = job.get("eId") or job.get("id")
            if not job_id:
                continue
            postings.append(
                self._posting(
                    external_id=str(job_id),
                    title=job.get("title") or "",
                    url=job.get("applyUrl")
                    or f"https://jobs.jobvite.com/{company_id}/job/{job_id}",
                    location=job.get("location"),
                    department=job.get("category") or job.get("department"),
                    posted_at=self.parse_date(job.get("postedDate") or job.get("date")),
                    description=self.strip_html(job.get("description")),
                    detail_pending=not job.get("description"),
                    raw={"job_type": job.get("jobType")},
                )
            )
        return postings

    async def _fallback(self, company_id: str) -> List[JobPosting]:
        url = f"https://jobs.jobvite.com/{company_id}/search"
        html, _ = await self.http.get_text(url, headers={"Accept": "text/html"})
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
        if not postings:
            raise ConnectorError(
                f"jobvite company '{company_id}' returned no postings",
                company=self.company.slug,
            )
        return postings
