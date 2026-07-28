"""Connector contract.

A connector's only job is to turn one employer's careers platform into
:class:`JobPosting` objects. Everything else — dedupe, scoring, notifying — is
someone else's problem, which is what keeps adding a new platform cheap.

Two-phase fetching is deliberate:

``fetch_listings`` must be cheap and is called every cycle. ``fetch_detail`` is
called only for postings that survived the title prefilter, because pulling
2,000 full descriptions every five minutes is the obvious way to get blocked.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Set

import structlog
from dateutil import parser as date_parser

from ..config.schema import CompanyConfig
from ..core.models import JobPosting, utcnow
from ..errors import ConnectorError
from ..net.client import HttpClient

log = structlog.get_logger(__name__)


class Connector(ABC):
    """Base class for every careers-platform integration."""

    #: Registry key, matched against ``connector:`` in companies.yaml.
    name: str = "base"
    #: Human-facing platform label recorded on each posting.
    platform: str = "unknown"
    #: Params that must be present before the connector can run.
    required_params: tuple = ()
    #: True when the platform returns newest postings first. Verified per
    #: platform — Workday and Oracle both do. It licenses early-stop paging,
    #: which is the difference between 41 requests per cycle and 2.
    ordered_by_recency: bool = False

    def __init__(self, company: CompanyConfig, http: HttpClient) -> None:
        self.company = company
        self.http = http
        self.params: Dict[str, Any] = dict(company.params or {})
        #: External IDs already in the database, injected by the orchestrator
        #: before each scan. Empty on the seeding pass, which is exactly when
        #: we do want the whole board.
        self.known_ids: Set[str] = set()
        self._validate()

    def should_stop_paging(self, page_ids: Sequence[str], pages_done: int) -> bool:
        """True once a page contains nothing we have not already seen.

        Only safe on recency-ordered platforms: everything past the first
        all-known page is necessarily older, so it cannot contain a new
        posting. Never triggers on the first cycle, when ``known_ids`` is
        empty and we genuinely want the full board.
        """
        if not self.ordered_by_recency or not self.known_ids or pages_done < 1:
            return False
        return bool(page_ids) and all(str(pid) in self.known_ids for pid in page_ids)

    def _validate(self) -> None:
        missing = [p for p in self.required_params if not self.params.get(p)]
        if missing:
            raise ConnectorError(
                f"{self.name} connector for '{self.company.slug}' is missing "
                f"required params: {', '.join(missing)}",
                company=self.company.slug,
            )

    @abstractmethod
    async def fetch_listings(self) -> List[JobPosting]:
        """Return every currently-open posting. Cheap; runs every cycle."""

    async def fetch_detail(self, posting: JobPosting) -> JobPosting:
        """Enrich one posting with its full description.

        The default assumes the listing endpoint already returned everything,
        which is true for Greenhouse and Lever.
        """
        return posting

    # ------------------------------------------------------------------ #
    # Helpers shared by concrete connectors
    # ------------------------------------------------------------------ #

    def _posting(self, *, external_id: str, title: str, url: str, **kwargs: Any) -> JobPosting:
        return JobPosting(
            company=self.company.name,
            company_slug=self.company.slug,
            external_id=str(external_id),
            title=title,
            url=url,
            source_platform=self.platform,
            **kwargs,
        )

    @staticmethod
    def parse_date(value: Any) -> Optional[datetime]:
        """Best-effort date parsing across the many formats ATS vendors use."""
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if isinstance(value, (int, float)):
            # Lever and a few others use epoch milliseconds.
            seconds = value / 1000.0 if value > 10_000_000_000 else float(value)
            try:
                return datetime.fromtimestamp(seconds, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            if text.isdigit():
                return Connector.parse_date(int(text))
            relative = Connector.parse_relative_date(text)
            if relative is not None:
                return relative
            try:
                parsed = date_parser.parse(text)
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except (ValueError, OverflowError, TypeError):
                return None
        return None

    # Workday and several others only ever expose a fuzzy string.
    _REL_TODAY = re.compile(r"posted\s+today|just posted|today", re.I)
    _REL_YESTERDAY = re.compile(r"posted\s+yesterday|yesterday", re.I)
    _REL_N = re.compile(
        r"(\d+)\+?\s*(day|days|hour|hours|minute|minutes|week|weeks|month|months)", re.I
    )

    @staticmethod
    def parse_relative_date(text: str) -> Optional[datetime]:
        """Turn "Posted 30+ Days Ago" into a timestamp.

        Approximate by construction. It is only used for the age filter and for
        display, never for dedupe, so the imprecision is harmless.
        """
        if Connector._REL_TODAY.search(text):
            return utcnow()
        if Connector._REL_YESTERDAY.search(text):
            return utcnow() - timedelta(days=1)
        match = Connector._REL_N.search(text)
        if not match:
            return None
        amount = int(match.group(1))
        unit = match.group(2).lower()
        deltas = {
            "minute": timedelta(minutes=amount),
            "minutes": timedelta(minutes=amount),
            "hour": timedelta(hours=amount),
            "hours": timedelta(hours=amount),
            "day": timedelta(days=amount),
            "days": timedelta(days=amount),
            "week": timedelta(weeks=amount),
            "weeks": timedelta(weeks=amount),
            "month": timedelta(days=amount * 30),
            "months": timedelta(days=amount * 30),
        }
        delta = deltas.get(unit)
        return utcnow() - delta if delta else None

    @staticmethod
    def strip_html(html: Optional[str]) -> str:
        """Cheap tag stripping.

        BeautifulSoup is only worth its cost when we need structure; for a
        description we just want words, and this runs on every posting.
        """
        if not html:
            return ""
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
        text = re.sub(r"<br\s*/?>|</p>|</li>|</div>", "\n", text, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        replacements = {
            "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
            "&quot;": '"', "&#39;": "'", "&rsquo;": "'", "&ldquo;": '"', "&rdquo;": '"',
        }
        for entity, char in replacements.items():
            text = text.replace(entity, char)
        text = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), text)
        return re.sub(r"[ \t]+", " ", text).strip()

    @staticmethod
    def join_locations(values: Any) -> Optional[str]:
        """Flatten the many shapes ATS vendors use for locations."""
        if values is None:
            return None
        if isinstance(values, str):
            return values or None
        if isinstance(values, dict):
            for key in ("name", "text", "label", "displayName", "formattedAddress"):
                if values.get(key):
                    return str(values[key])
            return None
        if isinstance(values, (list, tuple)):
            parts = [Connector.join_locations(v) for v in values]
            joined = ", ".join(p for p in parts if p)
            return joined or None
        return str(values)
