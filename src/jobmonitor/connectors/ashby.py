"""Ashby public job board API.

``https://api.ashbyhq.com/posting-api/job-board/{token}`` returns the whole
board in one response, descriptions included when asked.
"""

from __future__ import annotations

from typing import List

from ..core.models import JobPosting
from ..errors import ConnectorError
from .base import Connector

API_ROOT = "https://api.ashbyhq.com/posting-api/job-board"


class AshbyConnector(Connector):
    name = "ashby"
    platform = "ashby"
    required_params = ("board_token",)

    async def fetch_listings(self) -> List[JobPosting]:
        token = str(self.params["board_token"]).strip("/")
        payload, _ = await self.http.get_json(
            f"{API_ROOT}/{token}",
            params={"includeCompensation": "true"},
            headers={"Accept": "application/json"},
        )
        if not isinstance(payload, dict) or "jobs" not in payload:
            raise ConnectorError(
                f"ashby board '{token}' returned no 'jobs' key", company=self.company.slug
            )

        postings: List[JobPosting] = []
        for job in payload.get("jobs") or []:
            job_id = job.get("id")
            if not job_id:
                continue
            description = job.get("descriptionPlain") or self.strip_html(
                job.get("descriptionHtml")
            )
            postings.append(
                self._posting(
                    external_id=str(job_id),
                    title=job.get("title") or "",
                    url=job.get("jobUrl") or job.get("applyUrl") or "",
                    location=job.get("location")
                    or self.join_locations(job.get("secondaryLocations")),
                    department=job.get("department") or job.get("team"),
                    posted_at=self.parse_date(job.get("publishedAt")),
                    updated_at=self.parse_date(job.get("updatedAt")),
                    description=description,
                    detail_pending=not description,
                    raw={
                        "employment_type": job.get("employmentType"),
                        "is_remote": job.get("isRemote"),
                    },
                )
            )
        return postings
