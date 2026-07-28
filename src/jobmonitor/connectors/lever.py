"""Lever postings API.

Verified against Shield AI.

Lever returns the plain-text description inline, so a single request per cycle
covers both phases and ``fetch_detail`` has nothing left to do.
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..core.models import JobPosting
from ..errors import ConnectorError
from .base import Connector

API_ROOT = "https://api.lever.co/v0/postings"


class LeverConnector(Connector):
    name = "lever"
    platform = "lever"
    required_params = ("board_token",)

    @property
    def _token(self) -> str:
        return str(self.params["board_token"]).strip("/")

    async def fetch_listings(self) -> List[JobPosting]:
        url = f"{API_ROOT}/{self._token}"
        payload, _ = await self.http.get_json(
            url, params={"mode": "json"}, headers={"Accept": "application/json"}
        )
        if not isinstance(payload, list):
            raise ConnectorError(
                f"lever board '{self._token}' returned {type(payload).__name__}, expected a list",
                company=self.company.slug,
            )

        postings: List[JobPosting] = []
        for job in payload:
            job_id = job.get("id")
            if not job_id:
                continue
            categories: Dict[str, Any] = job.get("categories") or {}
            postings.append(
                self._posting(
                    external_id=str(job_id),
                    title=job.get("text") or "",
                    url=job.get("hostedUrl") or job.get("applyUrl") or "",
                    location=categories.get("location")
                    or self.join_locations(job.get("workplaceType")),
                    department=categories.get("department") or categories.get("team"),
                    posted_at=self.parse_date(job.get("createdAt")),
                    updated_at=self.parse_date(job.get("updatedAt")),
                    description=self._description(job),
                    detail_pending=False,
                    raw={
                        "commitment": categories.get("commitment"),
                        "workplace_type": job.get("workplaceType"),
                    },
                )
            )
        return postings

    def _description(self, job: Dict[str, Any]) -> str:
        parts = [job.get("descriptionPlain") or self.strip_html(job.get("description"))]
        # `lists` holds the requirements/responsibilities bullets, which are
        # where degree and skill signals actually live.
        for block in job.get("lists") or []:
            if isinstance(block, dict):
                parts.append(block.get("text") or "")
                parts.append(self.strip_html(block.get("content")))
        parts.append(job.get("additionalPlain") or "")
        return "\n".join(p for p in parts if p).strip()
