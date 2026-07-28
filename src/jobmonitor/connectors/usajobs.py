"""USAJOBS API — the official feed for NASA and other federal agencies.

Requires a free API key from https://developer.usajobs.gov/apirequest/, set as
``USAJOBS_API_KEY`` and ``USAJOBS_EMAIL``. This is a genuine public API with
documented rate limits, so it is preferred over scraping nasa.gov.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

from ..core.models import JobPosting
from ..errors import ConnectorError
from .base import Connector

API_URL = "https://data.usajobs.gov/api/search"
PAGE_SIZE = 250
MAX_PAGES = 8


class UsaJobsConnector(Connector):
    name = "usajobs"
    platform = "usajobs"

    def _headers(self) -> Dict[str, str]:
        key = self.params.get("api_key") or os.environ.get("USAJOBS_API_KEY")
        email = self.params.get("email") or os.environ.get("USAJOBS_EMAIL")
        if not key or not email:
            raise ConnectorError(
                "usajobs connector needs USAJOBS_API_KEY and USAJOBS_EMAIL "
                "(free key from https://developer.usajobs.gov/apirequest/)",
                company=self.company.slug,
            )
        return {
            "Host": "data.usajobs.gov",
            "User-Agent": email,
            "Authorization-Key": key,
            "Accept": "application/json",
        }

    async def fetch_listings(self) -> List[JobPosting]:
        headers = self._headers()
        postings: List[JobPosting] = []

        for page in range(1, MAX_PAGES + 1):
            params: Dict[str, Any] = {
                "ResultsPerPage": PAGE_SIZE,
                "Page": page,
                "SortField": "opendate",
                "SortDirection": "desc",
            }
            if self.params.get("organization"):
                params["Organization"] = self.params["organization"]
            if self.params.get("keyword"):
                params["Keyword"] = self.params["keyword"]
            if self.params.get("job_category_code"):
                params["JobCategoryCode"] = self.params["job_category_code"]

            payload, _cached = await self.http.get_json(API_URL, params=params, headers=headers)
            result = (payload or {}).get("SearchResult") or {}
            items = result.get("SearchResultItems") or []
            if not items:
                break

            for item in items:
                descriptor = item.get("MatchedObjectDescriptor") or {}
                job_id = item.get("MatchedObjectId") or descriptor.get("PositionID")
                if not job_id:
                    continue
                postings.append(
                    self._posting(
                        external_id=str(job_id),
                        title=descriptor.get("PositionTitle") or "",
                        url=descriptor.get("PositionURI") or "",
                        location=self._locations(descriptor),
                        department=descriptor.get("OrganizationName")
                        or descriptor.get("DepartmentName"),
                        posted_at=self.parse_date(descriptor.get("PublicationStartDate")),
                        deadline=self.parse_date(descriptor.get("ApplicationCloseDate")),
                        description=self._description(descriptor),
                        detail_pending=False,
                        raw={"grade": descriptor.get("JobGrade")},
                    )
                )

            if len(items) < PAGE_SIZE:
                break

        return postings

    @staticmethod
    def _locations(descriptor: Dict[str, Any]) -> Any:
        locations = descriptor.get("PositionLocation") or []
        names = [loc.get("LocationName") for loc in locations if isinstance(loc, dict)]
        return ", ".join(n for n in names if n) or descriptor.get("PositionLocationDisplay")

    def _description(self, descriptor: Dict[str, Any]) -> str:
        parts = [descriptor.get("QualificationSummary") or ""]
        user_area = (descriptor.get("UserArea") or {}).get("Details") or {}
        for key in ("JobSummary", "MajorDuties", "Requirements", "Education"):
            value = user_area.get(key)
            if isinstance(value, list):
                parts.extend(str(v) for v in value)
            elif value:
                parts.append(str(value))
        return self.strip_html("\n".join(p for p in parts if p))
