"""schema.org JobPosting extraction.

Google requires structured data for a posting to appear in Google Jobs, so
almost every careers site emits it — including sites with no usable API. That
makes JSON-LD the best fallback available before resorting to bespoke HTML
parsing, and a far more stable one: the markup is a published standard, so it
survives the redesigns that break CSS selectors.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterator, List, Optional

import structlog

from ..core.models import JobPosting, WorkMode
from ..errors import ConnectorError
from .base import Connector

log = structlog.get_logger(__name__)

_LD_BLOCK = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I,
)


def iter_jsonld_blocks(html: str) -> Iterator[Any]:
    """Yield every parsed JSON-LD block in a page."""
    for match in _LD_BLOCK.finditer(html):
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            yield json.loads(raw)
        except json.JSONDecodeError:
            # Some sites emit JS-with-comments in an ld+json tag. Salvage the
            # outermost object rather than losing the whole page.
            cleaned = re.sub(r"^\s*//.*$", "", raw, flags=re.M)
            try:
                yield json.loads(cleaned)
            except json.JSONDecodeError:
                continue


def find_job_postings(node: Any) -> Iterator[Dict[str, Any]]:
    """Walk arbitrary JSON-LD and yield every JobPosting object.

    Sites nest these differently — bare, inside ``@graph``, or wrapped in an
    ``ItemList`` — so a recursive walk beats matching any one shape.
    """
    if isinstance(node, list):
        for item in node:
            yield from find_job_postings(item)
        return
    if not isinstance(node, dict):
        return

    node_type = node.get("@type")
    types = node_type if isinstance(node_type, list) else [node_type]
    if any(isinstance(t, str) and t.lower() == "jobposting" for t in types):
        yield node
        return

    for key in ("@graph", "itemListElement", "mainEntity", "item", "hasPart"):
        if key in node:
            yield from find_job_postings(node[key])


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("name", "value", "text", "@value"):
            if value.get(key):
                return str(value[key])
    if isinstance(value, list):
        parts = [_text(v) for v in value]
        joined = ", ".join(p for p in parts if p)
        return joined or None
    return str(value)


def _location(node: Dict[str, Any]) -> Optional[str]:
    raw = node.get("jobLocation")
    entries = raw if isinstance(raw, list) else [raw]
    parts: List[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            text = _text(entry)
            if text:
                parts.append(text)
            continue
        address = entry.get("address")
        if isinstance(address, dict):
            bits = [
                address.get("addressLocality"),
                address.get("addressRegion"),
                address.get("addressCountry"),
            ]
            text = ", ".join(str(b) for b in bits if b and isinstance(b, str))
            if not text:
                text = _text(address.get("streetAddress"))
            if text:
                parts.append(text)
        elif entry.get("name"):
            parts.append(str(entry["name"]))
    if node.get("jobLocationType") == "TELECOMMUTE" and not parts:
        return "Remote"
    return ", ".join(dict.fromkeys(parts)) or None


def posting_from_jsonld(
    node: Dict[str, Any], *, company: str, slug: str, platform: str, fallback_url: str
) -> Optional[JobPosting]:
    title = _text(node.get("title"))
    if not title:
        return None

    url = _text(node.get("url")) or _text(node.get("sameAs")) or fallback_url
    identifier = node.get("identifier")
    external_id = None
    if isinstance(identifier, dict):
        external_id = _text(identifier.get("value")) or _text(identifier.get("name"))
    external_id = external_id or _text(node.get("jobPostingId")) or url

    org = node.get("hiringOrganization")
    department = _text(node.get("occupationalCategory")) or (
        _text(org.get("department")) if isinstance(org, dict) else None
    )

    posting = JobPosting(
        company=company,
        company_slug=slug,
        external_id=str(external_id),
        title=title,
        url=url,
        location=_location(node),
        department=department,
        description=Connector.strip_html(_text(node.get("description"))),
        posted_at=Connector.parse_date(node.get("datePosted")),
        updated_at=Connector.parse_date(node.get("dateModified")),
        deadline=Connector.parse_date(node.get("validThrough")),
        source_platform=platform,
        detail_pending=False,
        raw={"employment_type_raw": _text(node.get("employmentType"))},
    )
    if node.get("jobLocationType") == "TELECOMMUTE":
        posting.work_mode = WorkMode.REMOTE
    return posting


class JsonLdConnector(Connector):
    """Reads schema.org markup straight off a careers or search-results page."""

    name = "jsonld"
    platform = "jsonld"
    required_params = ("url",)

    async def fetch_listings(self) -> List[JobPosting]:
        urls = self.params.get("urls") or [self.params["url"]]
        postings: List[JobPosting] = []
        seen = set()

        for url in urls:
            html, _ = await self.http.get_text(url, headers={"Accept": "text/html"})
            for block in iter_jsonld_blocks(html):
                for node in find_job_postings(block):
                    posting = posting_from_jsonld(
                        node,
                        company=self.company.name,
                        slug=self.company.slug,
                        platform=self.platform,
                        fallback_url=url,
                    )
                    if posting and posting.job_key not in seen:
                        seen.add(posting.job_key)
                        postings.append(posting)

        if not postings:
            raise ConnectorError(
                f"no schema.org JobPosting markup found at {urls[0]}",
                company=self.company.slug,
            )
        return postings
