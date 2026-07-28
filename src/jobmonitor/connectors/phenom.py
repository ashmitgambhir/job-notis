"""Phenom People career sites.

Used by RTX, United Airlines, ITW's front end and many others; recognisable by
the ``/us/en`` or ``/global/en`` locale path.

Phenom renders search results client-side from an XHR that returns
``eagerLoadRefineSearch.data.jobs``. Requesting that endpoint with
``X-Requested-With: XMLHttpRequest`` yields JSON directly. Tenants differ in
which path they expose, so several are tried before falling back to the
JSON-LD embedded in the server-rendered page.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import structlog

from ..core.models import JobPosting
from ..errors import ConnectorError, PermanentError, TransientError
from .base import Connector
from .jsonld import find_job_postings, iter_jsonld_blocks, posting_from_jsonld

log = structlog.get_logger(__name__)
PAGE_SIZE = 100
MAX_PAGES = 20


class PhenomConnector(Connector):
    name = "phenom"
    platform = "phenom"
    required_params = ("host",)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._working_path: Optional[str] = None

    @property
    def _host(self) -> str:
        return str(self.params["host"]).replace("https://", "").strip("/")

    @property
    def _locale(self) -> str:
        return str(self.params.get("locale", "us/en")).strip("/")

    def _candidate_paths(self) -> List[str]:
        return [
            f"https://{self._host}/{self._locale}/search-results",
            f"https://{self._host}/search-results",
            f"https://{self._host}/{self._locale}/jobs",
        ]

    async def fetch_listings(self) -> List[JobPosting]:
        paths = [self._working_path] if self._working_path else self._candidate_paths()
        last_error: Optional[Exception] = None

        for path in paths:
            try:
                postings = await self._fetch_xhr(path)
                if postings:
                    self._working_path = path
                    return postings
            except (PermanentError, TransientError, ConnectorError) as exc:
                last_error = exc
                log.debug("phenom_path_failed", path=path, error=str(exc))

        # Server-rendered JSON-LD is the safety net when the XHR shape changes.
        postings = await self._fetch_jsonld()
        if postings:
            return postings

        raise ConnectorError(
            f"phenom site {self._host} yielded no postings: {last_error}",
            company=self.company.slug,
        )

    async def _fetch_xhr(self, path: str) -> List[JobPosting]:
        postings: List[JobPosting] = []
        offset = 0
        for _ in range(MAX_PAGES):
            payload, _cached = await self.http.get_json(
                path,
                params={
                    "from": offset,
                    "s": 1,
                    "rk": "l-search-results",
                    "keyword": self.params.get("query", ""),
                },
                headers={
                    "Accept": "application/json",
                    # Without this header Phenom serves the HTML shell instead.
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
            jobs = self._extract_jobs(payload)
            if not jobs:
                break
            for job in jobs:
                posting = self._to_posting(job)
                if posting:
                    postings.append(posting)
            offset += len(jobs)
            total = self._extract_total(payload)
            if total and offset >= total:
                break
            if len(jobs) < 10:
                break
        return postings

    @staticmethod
    def _extract_jobs(payload: Any) -> List[Dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        eager = payload.get("eagerLoadRefineSearch") or payload.get("refineSearch") or {}
        data = eager.get("data") if isinstance(eager, dict) else None
        if isinstance(data, dict):
            jobs = data.get("jobs")
            if isinstance(jobs, list):
                return jobs
        for key in ("jobs", "results", "hits"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return []

    @staticmethod
    def _extract_total(payload: Any) -> int:
        if not isinstance(payload, dict):
            return 0
        eager = payload.get("eagerLoadRefineSearch") or {}
        data = eager.get("data") if isinstance(eager, dict) else {}
        if isinstance(data, dict):
            try:
                return int(data.get("totalHits") or data.get("total") or 0)
            except (TypeError, ValueError):
                return 0
        return 0

    def _to_posting(self, job: Dict[str, Any]) -> Optional[JobPosting]:
        job_id = job.get("jobId") or job.get("jobSeqNo") or job.get("id")
        title = job.get("title") or job.get("jobTitle")
        if not job_id or not title:
            return None
        location = job.get("location") or ", ".join(
            str(p) for p in [job.get("cityStateCountry"), job.get("city"), job.get("state")] if p
        )
        return self._posting(
            external_id=str(job_id),
            title=title,
            url=job.get("applyUrl") or job.get("jobUrl") or f"https://{self._host}",
            location=location or None,
            department=job.get("department") or job.get("category") or job.get("ml_job_parser"),
            posted_at=self.parse_date(job.get("postedDate") or job.get("dateCreated")),
            description=self.strip_html(job.get("descriptionTeaser") or job.get("description")),
            detail_pending=not job.get("description"),
            raw={"type": job.get("type"), "job_seq_no": job.get("jobSeqNo")},
        )

    async def _fetch_jsonld(self) -> List[JobPosting]:
        url = f"https://{self._host}/{self._locale}/search-results"
        try:
            html, _ = await self.http.get_text(url, headers={"Accept": "text/html"})
        except (PermanentError, TransientError):
            return []
        postings: List[JobPosting] = []
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
        return postings

    async def fetch_detail(self, posting: JobPosting) -> JobPosting:
        if not posting.url or not posting.detail_pending:
            posting.detail_pending = False
            return posting
        try:
            html, _ = await self.http.get_text(posting.url, headers={"Accept": "text/html"})
        except (PermanentError, TransientError):
            posting.detail_pending = False
            return posting
        for block in iter_jsonld_blocks(html):
            for node in find_job_postings(block):
                description = node.get("description")
                if description:
                    posting.description = self.strip_html(str(description))
                    posting.detail_pending = False
                    return posting
        posting.detail_pending = False
        return posting
