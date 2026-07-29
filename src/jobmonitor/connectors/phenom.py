"""Phenom People career sites.

Used by United Airlines, RTX, Abbott's front end and many others; recognisable
by the ``/us/en`` or ``/global/en`` locale path.

Phenom renders search results client-side, but it also **embeds the same data
in the page** as a ``window.phApp.ddo = {...}`` blob containing
``eagerLoadRefineSearch.data.jobs``. Reading that is far more reliable than
guessing at the XHR: the plausible-looking ``/widgets`` and ``/api/jobs`` paths
return 404 and 500 on live tenants, whereas the embedded payload is what
actually renders the page and carries full records — title, requisition id,
category, department, posting date and apply URL.

Verified against United Airlines: 10 jobs per page, paginated with ``?from=N``.

Some tenants (RTX) sit behind a bot-protection interstitial that answers 403 to
any non-browser client. Nothing can be done from here; those need the browser
connector.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Set

import structlog

from ..core.models import JobPosting
from ..errors import ConnectorError, PermanentError, TransientError
from .base import Connector
from .jsonld import find_job_postings, iter_jsonld_blocks, posting_from_jsonld

log = structlog.get_logger(__name__)

PAGE_SIZE = 10          # Phenom's own page size; not configurable per request.
MAX_PAGES = 60
_DDO = re.compile(r"phApp\.ddo\s*=\s*(\{)")


def extract_ddo(html: str) -> Optional[Dict[str, Any]]:
    """Pull the ``phApp.ddo`` object out of a Phenom page.

    It is a JavaScript assignment rather than a script tag with a clean
    boundary, so the closing brace is found by counting depth — a lazy regex
    would stop at the first nested object. String contents are skipped so a
    brace inside a job description cannot throw the count off.
    """
    match = _DDO.search(html)
    if not match:
        return None
    start = match.start(1)
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(html)):
        ch = html[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


class PhenomConnector(Connector):
    name = "phenom"
    platform = "phenom"
    required_params = ("host",)

    @property
    def _host(self) -> str:
        return str(self.params["host"]).replace("https://", "").strip("/")

    @property
    def _locale(self) -> str:
        return str(self.params.get("locale", "us/en")).strip("/")

    @property
    def _search_url(self) -> str:
        return f"https://{self._host}/{self._locale}/search-results"

    async def fetch_listings(self) -> List[JobPosting]:
        postings: List[JobPosting] = []
        seen: Set[str] = set()
        query = self.params.get("query", "")

        for page in range(MAX_PAGES):
            params: Dict[str, Any] = {"from": page * PAGE_SIZE, "s": 1}
            if query:
                params["keyword"] = query
            try:
                html, _ = await self.http.get_text(
                    self._search_url, params=params, headers={"Accept": "text/html"}
                )
            except (PermanentError, TransientError) as exc:
                if page == 0:
                    raise ConnectorError(
                        f"phenom site {self._host} is unreachable ({exc}). A tenant "
                        "behind a bot-protection interstitial needs the browser "
                        "connector instead.",
                        company=self.company.slug,
                    ) from exc
                break

            jobs = self._jobs_from(html)
            if not jobs:
                if page == 0:
                    # No embedded payload — try the server-rendered schema.org
                    # markup before giving up on the tenant entirely.
                    fallback = self._from_jsonld(html)
                    if fallback:
                        return fallback
                break

            fresh = 0
            for job in jobs:
                posting = self._to_posting(job)
                if posting and posting.job_key not in seen:
                    seen.add(posting.job_key)
                    postings.append(posting)
                    fresh += 1
            # Past the last page Phenom repeats the final one rather than
            # returning empty, so a page with nothing new means we are done.
            if fresh == 0:
                break

        if not postings:
            raise ConnectorError(
                f"phenom site {self._host} exposed no postings", company=self.company.slug
            )
        return postings

    @staticmethod
    def _jobs_from(html: str) -> List[Dict[str, Any]]:
        ddo = extract_ddo(html)
        if not ddo:
            return []
        eager = ddo.get("eagerLoadRefineSearch") or ddo.get("refineSearch") or {}
        data = eager.get("data") if isinstance(eager, dict) else None
        if isinstance(data, dict):
            jobs = data.get("jobs")
            if isinstance(jobs, list):
                return jobs
        return []

    def _to_posting(self, job: Dict[str, Any]) -> Optional[JobPosting]:
        job_id = job.get("jobId") or job.get("jobSeqNo")
        title = job.get("title")
        if not job_id or not title:
            return None
        return self._posting(
            external_id=str(job_id),
            title=title,
            url=job.get("applyUrl") or f"https://{self._host}",
            location=job.get("cityStateCountry") or job.get("cityState") or job.get("city"),
            department=job.get("department") or job.get("category"),
            posted_at=self.parse_date(job.get("postedDate") or job.get("dateCreated")),
            description=self.strip_html(job.get("descriptionTeaser")),
            # The teaser is a summary; full text needs the detail page.
            detail_pending=True,
            raw={
                "job_seq_no": job.get("jobSeqNo"),
                "job_level": job.get("jobLevel"),
                "division": job.get("division"),
            },
        )

    def _from_jsonld(self, html: str) -> List[JobPosting]:
        postings = []
        for block in iter_jsonld_blocks(html):
            for node in find_job_postings(block):
                posting = posting_from_jsonld(
                    node,
                    company=self.company.name,
                    slug=self.company.slug,
                    platform=self.platform,
                    fallback_url=self._search_url,
                )
                if posting:
                    postings.append(posting)
        return postings

    async def fetch_detail(self, posting: JobPosting) -> JobPosting:
        if not posting.url:
            posting.detail_pending = False
            return posting
        try:
            html, _ = await self.http.get_text(posting.url, headers={"Accept": "text/html"})
        except (PermanentError, TransientError):
            posting.detail_pending = False
            return posting

        # The detail page carries the same blob, with the full description.
        ddo = extract_ddo(html) or {}
        detail = ddo.get("jobDetail") or {}
        data = detail.get("data") if isinstance(detail, dict) else None
        job = (data or {}).get("job") if isinstance(data, dict) else None
        if isinstance(job, dict) and job.get("description"):
            posting.description = self.strip_html(job["description"])
            posting.detail_pending = False
            return posting

        for block in iter_jsonld_blocks(html):
            for node in find_job_postings(block):
                if node.get("description"):
                    posting.description = self.strip_html(str(node["description"]))
                    posting.detail_pending = False
                    return posting

        posting.detail_pending = False
        return posting
