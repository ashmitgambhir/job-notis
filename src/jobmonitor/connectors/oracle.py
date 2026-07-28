"""Oracle Recruiting Cloud (Fusion "CX" career sites).

Used by Texas Instruments. The public REST surface is::

    https://{pod}/hcmRestApi/resources/latest/recruitingCEJobRequisitions
        ?finder=findReqs;siteNumber=CX_1001,limit=200,sortBy=POSTING_DATES_DESC

The vanity careers domain redirects to the real Fusion pod
(``careers.ti.com`` -> ``edbz.fa.us2.oraclecloud.com``), which is where the API
actually lives, so ``host`` must be the pod rather than the vanity name.
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..core.models import JobPosting
from ..errors import ConnectorError
from .base import Connector

PAGE_SIZE = 200
MAX_PAGES = 15


class OracleConnector(Connector):
    name = "oracle"
    platform = "oracle_recruiting"
    required_params = ("host", "site_number")
    # We ask for POSTING_DATES_DESC explicitly below.
    ordered_by_recency = True

    @property
    def _host(self) -> str:
        return str(self.params["host"]).replace("https://", "").strip("/")

    @property
    def _api(self) -> str:
        return f"https://{self._host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"

    async def fetch_listings(self) -> List[JobPosting]:
        site = str(self.params["site_number"])
        postings: List[JobPosting] = []
        offset = 0

        for pages_done in range(MAX_PAGES):
            finder = (
                f"findReqs;siteNumber={site},limit={PAGE_SIZE},offset={offset},"
                "sortBy=POSTING_DATES_DESC"
            )
            payload, _cached = await self.http.get_json(
                self._api,
                params={
                    "onlyData": "true",
                    "expand": "requisitionList.secondaryLocations",
                    "finder": finder,
                },
                headers={"Accept": "application/json"},
            )
            items = self._requisitions(payload)
            if not items:
                break

            for job in items:
                job_id = job.get("Id") or job.get("RequisitionId")
                if not job_id:
                    continue
                postings.append(
                    self._posting(
                        external_id=str(job_id),
                        title=job.get("Title") or "",
                        url=f"https://{self._host}/hcmUI/CandidateExperience/en/sites/"
                        f"{site}/job/{job_id}",
                        location=job.get("PrimaryLocation")
                        or self.join_locations(job.get("secondaryLocations")),
                        department=job.get("JobFunction") or job.get("Organization"),
                        posted_at=self.parse_date(job.get("PostedDate")),
                        detail_pending=True,
                        raw={"job_family": job.get("JobFamily")},
                    )
                )

            page_ids = [str(j.get("Id") or j.get("RequisitionId") or "") for j in items]
            if self.should_stop_paging([p for p in page_ids if p], pages_done):
                break

            offset += PAGE_SIZE
            if len(items) < PAGE_SIZE:
                break

        if not postings:
            raise ConnectorError(
                f"oracle site {site} on {self._host} returned no requisitions",
                company=self.company.slug,
            )
        return postings

    @staticmethod
    def _requisitions(payload: Any) -> List[Dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        items = payload.get("items") or []
        if items and isinstance(items[0], dict) and "requisitionList" in items[0]:
            return items[0].get("requisitionList") or []
        return items if isinstance(items, list) else []

    async def fetch_detail(self, posting: JobPosting) -> JobPosting:
        site = str(self.params["site_number"])
        payload, _ = await self.http.get_json(
            self._api,
            params={
                "onlyData": "true",
                "expand": "all",
                "finder": f"findReqDetailById;Id={posting.external_id},siteNumber={site}",
            },
            headers={"Accept": "application/json"},
        )
        items = (payload or {}).get("items") or []
        if items:
            detail = items[0]
            posting.description = self.strip_html(
                (detail.get("ExternalDescriptionStr") or "")
                + " "
                + (detail.get("ExternalQualificationsStr") or "")
            )
            posting.deadline = self.parse_date(detail.get("EndDate")) or posting.deadline
        posting.detail_pending = False
        return posting
