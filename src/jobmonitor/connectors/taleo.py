"""Oracle Taleo career sections.

Legacy but still common in aerospace and government-adjacent employers. The
job board exposes an unauthenticated JSON search at::

    https://{host}/careersection/rest/jobboard/searchjobs?lang=en&portal={portal}

Taleo returns values as a positional ``contents`` array whose meaning is
defined by a separate field list, so column indices are resolved by name at
runtime rather than hard-coded.
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..core.models import JobPosting
from ..errors import ConnectorError
from .base import Connector

PAGE_SIZE = 100
MAX_PAGES = 15


class TaleoConnector(Connector):
    name = "taleo"
    platform = "taleo"
    required_params = ("host",)

    @property
    def _base(self) -> str:
        return f"https://{str(self.params['host']).replace('https://', '').strip('/')}"

    async def fetch_listings(self) -> List[JobPosting]:
        portal = self.params.get("portal")
        postings: List[JobPosting] = []

        for page in range(1, MAX_PAGES + 1):
            body: Dict[str, Any] = {
                "multilineEnabled": False,
                "sortingSelection": {"sortBySelectionParam": "3", "ascendingSortingOrder": "false"},
                "fieldData": {
                    "fields": {"KEYWORD": self.params.get("query", "")},
                    "valid": True,
                },
                "filterSelectionParam": {"searchFilterSelections": []},
                "advancedSearchFiltersSelectionParam": {"searchFilterSelections": []},
                "pageNo": page,
            }
            params = {"lang": "en", "pageSize": PAGE_SIZE}
            if portal:
                params["portal"] = portal

            payload, _cached = await self.http.post_json(
                f"{self._base}/careersection/rest/jobboard/searchjobs",
                body,
                params=params,
                headers={"Accept": "application/json"},
            )
            if not isinstance(payload, dict):
                raise ConnectorError(
                    f"taleo {self._base} returned {type(payload).__name__}",
                    company=self.company.slug,
                )
            requisitions = payload.get("requisitionList") or []
            if not requisitions:
                break

            columns = payload.get("fieldNames") or payload.get("columns") or []
            index = {str(name).lower(): i for i, name in enumerate(columns)}

            for item in requisitions:
                job_id = item.get("jobId") or item.get("contestNo")
                if not job_id:
                    continue
                contents = item.get("column") or item.get("contents") or []
                postings.append(
                    self._posting(
                        external_id=str(job_id),
                        title=self._field(contents, index, "title") or item.get("title") or "",
                        url=f"{self._base}/careersection/jobdetail.ftl?job={job_id}",
                        location=self._field(contents, index, "location"),
                        department=self._field(contents, index, "department")
                        or self._field(contents, index, "organization"),
                        posted_at=self.parse_date(self._field(contents, index, "postedDate")),
                        detail_pending=True,
                    )
                )

            if len(requisitions) < PAGE_SIZE:
                break

        if not postings:
            raise ConnectorError(
                f"taleo {self._base} returned no requisitions", company=self.company.slug
            )
        return postings

    @staticmethod
    def _field(contents: List[Any], index: Dict[str, int], name: str) -> Any:
        position = index.get(name.lower())
        if position is None or position >= len(contents):
            return None
        return contents[position]
