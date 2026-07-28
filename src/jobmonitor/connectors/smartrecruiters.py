"""SmartRecruiters public postings API.

Note for anyone extending discovery: this API answers ``200`` with
``totalFound: 0`` for *any* company slug, valid or not. A 200 therefore proves
nothing — only a non-zero ``totalFound`` does. The probe in
:mod:`jobmonitor.connectors.discovery` relies on that distinction.
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..core.models import JobPosting
from ..errors import ConnectorError
from .base import Connector

API_ROOT = "https://api.smartrecruiters.com/v1/companies"
PAGE_SIZE = 100
MAX_PAGES = 30


class SmartRecruitersConnector(Connector):
    name = "smartrecruiters"
    platform = "smartrecruiters"
    required_params = ("company_id",)

    async def fetch_listings(self) -> List[JobPosting]:
        company_id = str(self.params["company_id"]).strip("/")
        postings: List[JobPosting] = []
        offset = 0

        for _ in range(MAX_PAGES):
            payload, _cached = await self.http.get_json(
                f"{API_ROOT}/{company_id}/postings",
                params={"limit": PAGE_SIZE, "offset": offset},
                headers={"Accept": "application/json"},
            )
            if not isinstance(payload, dict):
                raise ConnectorError(
                    f"smartrecruiters '{company_id}' returned {type(payload).__name__}",
                    company=self.company.slug,
                )
            content = payload.get("content") or []
            if not content:
                break

            for job in content:
                job_id = job.get("id")
                if not job_id:
                    continue
                postings.append(
                    self._posting(
                        external_id=str(job_id),
                        title=job.get("name") or "",
                        url=job.get("applyUrl")
                        or f"https://jobs.smartrecruiters.com/{company_id}/{job_id}",
                        location=self._location(job.get("location")),
                        department=(job.get("department") or {}).get("label"),
                        posted_at=self.parse_date(job.get("releasedDate")),
                        detail_pending=True,
                        raw={"ref": job.get("ref"), "type": (job.get("typeOfEmployment") or {})},
                    )
                )

            offset += PAGE_SIZE
            if offset >= int(payload.get("totalFound") or 0):
                break

        return postings

    @staticmethod
    def _location(loc: Any) -> Any:
        if not isinstance(loc, dict):
            return Connector.join_locations(loc)
        parts = [loc.get("city"), loc.get("region"), loc.get("country")]
        text = ", ".join(str(p) for p in parts if p)
        if loc.get("remote"):
            text = f"{text} (Remote)" if text else "Remote"
        return text or None

    async def fetch_detail(self, posting: JobPosting) -> JobPosting:
        ref = posting.raw.get("ref")
        if not ref:
            posting.detail_pending = False
            return posting
        payload, _ = await self.http.get_json(ref, headers={"Accept": "application/json"})
        job_ad: Dict[str, Any] = (payload or {}).get("jobAd") or {}
        sections = (job_ad.get("sections") or {}) if isinstance(job_ad, dict) else {}
        chunks = []
        sections_wanted = (
            "companyDescription",
            "jobDescription",
            "qualifications",
            "additionalInformation",
        )
        for key in sections_wanted:
            section = sections.get(key) or {}
            if isinstance(section, dict) and section.get("text"):
                chunks.append(self.strip_html(section["text"]))
        posting.description = "\n".join(c for c in chunks if c)
        posting.detail_pending = False
        return posting
