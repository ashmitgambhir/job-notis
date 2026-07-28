"""Eightfold AI talent platform.

Used by John Deere and Northrop Grumman among others. The API lives at
``/api/apply/v2/jobs`` and is served both from ``app.eightfold.ai`` and from
the customer's own careers host.

Which host works varies by tenant, and ``app.eightfold.ai`` rejects
unrecognised domains with ``403 Not authorized for PCSX``. Rather than pinning
one, the connector tries the configured host first and falls back, then caches
whichever answered.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import structlog

from ..core.models import JobPosting
from ..errors import ConnectorError, PermanentError, TransientError
from .base import Connector

log = structlog.get_logger(__name__)
PAGE_SIZE = 100
MAX_PAGES = 20


class EightfoldConnector(Connector):
    name = "eightfold"
    platform = "eightfold"
    required_params = ("domain",)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._working_host: Optional[str] = None

    def _candidate_hosts(self) -> List[str]:
        hosts = []
        configured = self.params.get("host")
        if configured:
            hosts.append(str(configured).replace("https://", "").strip("/"))
        hosts.append("app.eightfold.ai")
        # De-duplicate while preserving priority order.
        return list(dict.fromkeys(hosts))

    async def fetch_listings(self) -> List[JobPosting]:
        domain = str(self.params["domain"])
        hosts = [self._working_host] if self._working_host else self._candidate_hosts()

        last_error: Optional[Exception] = None
        for host in hosts:
            try:
                postings = await self._fetch_from(host, domain)
                self._working_host = host
                return postings
            except (PermanentError, TransientError, ConnectorError) as exc:
                last_error = exc
                # A host that rejects us is not worth retrying this cycle.
                self._working_host = None
                log.debug("eightfold_host_failed", host=host, error=str(exc))
                continue

        raise ConnectorError(
            f"no eightfold host answered for domain '{domain}': {last_error}",
            company=self.company.slug,
        )

    async def _fetch_from(self, host: str, domain: str) -> List[JobPosting]:
        postings: List[JobPosting] = []
        start = 0
        for _ in range(MAX_PAGES):
            payload, _cached = await self.http.get_json(
                f"https://{host}/api/apply/v2/jobs",
                params={
                    "domain": domain,
                    "start": start,
                    "num": PAGE_SIZE,
                    "query": self.params.get("query", ""),
                    "sort_by": "timestamp",
                },
                headers={"Accept": "application/json"},
            )
            if not isinstance(payload, dict) or "positions" not in payload:
                raise ConnectorError(
                    f"eightfold host {host} returned no 'positions'", company=self.company.slug
                )
            positions = payload.get("positions") or []
            if not positions:
                break

            for job in positions:
                job_id = job.get("id") or job.get("display_job_id")
                if not job_id:
                    continue
                postings.append(
                    self._posting(
                        external_id=str(job_id),
                        title=job.get("name") or "",
                        url=job.get("canonicalPositionUrl")
                        or f"https://{host}/careers/job/{job_id}",
                        location=job.get("location") or self.join_locations(job.get("locations")),
                        department=job.get("department") or job.get("business_unit"),
                        posted_at=self.parse_date(
                            job.get("t_create") or job.get("t_update") or job.get("posted_date")
                        ),
                        description=self.strip_html(job.get("job_description")),
                        detail_pending=not job.get("job_description"),
                        raw={"work_location_option": job.get("work_location_option")},
                    )
                )

            start += PAGE_SIZE
            if start >= int(payload.get("count") or 0):
                break
        return postings

    async def fetch_detail(self, posting: JobPosting) -> JobPosting:
        host = self._working_host or self._candidate_hosts()[0]
        payload, _ = await self.http.get_json(
            f"https://{host}/api/apply/v2/jobs/{posting.external_id}",
            params={"domain": str(self.params["domain"])},
            headers={"Accept": "application/json"},
        )
        data: Dict[str, Any] = payload if isinstance(payload, dict) else {}
        posting.description = self.strip_html(data.get("job_description")) or posting.description
        posting.detail_pending = False
        return posting
