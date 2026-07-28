"""Greenhouse job boards API.

Verified against Anduril, SpaceX and Rocket Lab.

The board endpoint supports ``?content=true`` to inline every description, but
a board like SpaceX's carries 2,000 postings and that response runs to tens of
megabytes. Polling it every few minutes would be wasteful and conspicuous, so
listings are fetched lean and descriptions are pulled per posting only after
the title survives the prefilter.
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..core.models import JobPosting
from ..errors import ConnectorError
from .base import Connector

API_ROOT = "https://boards-api.greenhouse.io/v1/boards"


class GreenhouseConnector(Connector):
    name = "greenhouse"
    platform = "greenhouse"
    required_params = ("board_token",)

    @property
    def _token(self) -> str:
        return str(self.params["board_token"]).strip("/")

    async def fetch_listings(self) -> List[JobPosting]:
        url = f"{API_ROOT}/{self._token}/jobs"
        payload, _ = await self.http.get_json(url, headers={"Accept": "application/json"})
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if jobs is None:
            raise ConnectorError(
                f"greenhouse board '{self._token}' returned no 'jobs' key",
                company=self.company.slug,
            )

        postings: List[JobPosting] = []
        for job in jobs:
            job_id = job.get("id") or job.get("internal_job_id")
            if job_id is None:
                continue
            postings.append(
                self._posting(
                    external_id=str(job_id),
                    title=job.get("title") or "",
                    url=job.get("absolute_url") or f"{API_ROOT}/{self._token}/jobs/{job_id}",
                    location=self.join_locations(job.get("location")),
                    department=self._department(job),
                    posted_at=self.parse_date(
                        job.get("first_published") or job.get("updated_at")
                    ),
                    updated_at=self.parse_date(job.get("updated_at")),
                    detail_pending=True,
                    raw={"requisition_id": job.get("requisition_id")},
                )
            )
        return postings

    @staticmethod
    def _department(job: Dict[str, Any]) -> Any:
        departments = job.get("departments") or []
        if departments and isinstance(departments, list):
            return departments[0].get("name") if isinstance(departments[0], dict) else None
        return None

    async def fetch_detail(self, posting: JobPosting) -> JobPosting:
        url = f"{API_ROOT}/{self._token}/jobs/{posting.external_id}"
        payload, _ = await self.http.get_json(url, headers={"Accept": "application/json"})
        if not isinstance(payload, dict):
            return posting
        posting.description = self.strip_html(payload.get("content"))
        if not posting.department:
            posting.department = self._department(payload)
        posting.detail_pending = False
        return posting
