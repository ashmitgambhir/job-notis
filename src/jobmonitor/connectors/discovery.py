"""Automatic careers-platform detection.

Hand-maintaining endpoints for 500 companies does not scale, and vendors get
swapped without warning. Discovery makes the endpoint a derived value instead
of a hand-written one.

Three stages, cheapest first:

1. **Fingerprint** — fetch the careers page, follow redirects, and look for
   vendor markers in the final URL and the HTML. This is how ``careers.ti.com``
   reveals itself as Oracle (it redirects to a ``*.oraclecloud.com`` pod) and
   how ``jobs.abbott`` reveals its Workday tenant (the page embeds
   ``abbott.wd5.myworkdayjobs.com/abbottcareers/...`` links).
2. **Guess** — for board-token platforms, try slug variants of the company name.
3. **Verify** — actually run the candidate connector. A candidate only counts
   if it returns real postings.

Stage 3 is what makes the result trustworthy: SmartRecruiters answers ``200``
with an empty list for *any* slug, and Lever does the same for some, so status
codes alone would produce confident nonsense.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import structlog

from ..config.schema import CompanyConfig
from ..errors import JobMonitorError
from ..net.client import HttpClient
from .registry import get_connector_class

log = structlog.get_logger(__name__)


@dataclass
class Candidate:
    connector: str
    params: Dict[str, Any]
    # Higher wins ties. Markers found in the page outrank slug guesses.
    confidence: int = 50
    source: str = "fingerprint"


@dataclass
class DiscoveryResult:
    slug: str
    connector: Optional[str] = None
    params: Dict[str, Any] = field(default_factory=dict)
    jobs_found: int = 0
    tried: List[Tuple[str, str]] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.connector is not None and self.jobs_found > 0


# Minimum postings a candidate must return before it is believed, keyed by how
# the candidate was found.
#
# "Returns postings" is not sufficient evidence on its own. A guessed board
# token can land on a completely different employer that happens to share a
# name — `blue` on Lever is a data consultancy, not Blue Origin; `parker` on
# Ashby is a fintech, not Parker Hannifin — or on a real but abandoned stub
# account, as `caterpillarinc` on SmartRecruiters is, with one admin job
# standing in for a board of thousands. Both failures look like success and
# then silently monitor the wrong thing forever.
#
# Fingerprinted candidates need no such bar: the URL came from the company's
# own careers page, so identity is already established and a small board is
# simply a small board.
MIN_POSTINGS_BY_SOURCE = {
    "fingerprint": 1,
    "fallback": 5,
    "guess": 15,
}

# --- Fingerprint patterns -------------------------------------------------- #

_WORKDAY_URL = re.compile(
    r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)",
    re.I,
)
_GREENHOUSE_URL = re.compile(
    r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)", re.I
)
_LEVER_URL = re.compile(r"jobs\.lever\.co/([a-z0-9_-]+)", re.I)
_ASHBY_URL = re.compile(r"jobs\.ashbyhq\.com/([a-z0-9_-]+)", re.I)
_SMARTRECRUITERS_URL = re.compile(r"jobs\.smartrecruiters\.com/([A-Za-z0-9_-]+)", re.I)
_ICIMS_URL = re.compile(r"https?://([a-z0-9-]+)\.icims\.com", re.I)
_ORACLE_URL = re.compile(r"https?://([a-z0-9-]+\.fa\.[a-z0-9]+\.oraclecloud\.com)", re.I)
_ORACLE_SITE = re.compile(r"siteNumber=(CX_\d+)", re.I)
_JOBVITE_URL = re.compile(r"jobs\.jobvite\.com/([a-z0-9_-]+)", re.I)
_TALEO_URL = re.compile(r"https?://([a-z0-9.-]+)/careersection", re.I)

# Weak markers: the vendor is present but parameters must come from elsewhere.
_MARKERS = {
    "phenom": re.compile(r"phenompeople|phApp|ph_id|phenom\.com", re.I),
    "eightfold": re.compile(r"eightfold\.ai", re.I),
    "successfactors": re.compile(r"successfactors|sfcareers|jobs\.sap\.com", re.I),
    "avature": re.compile(r"avature", re.I),
    "icims": re.compile(r"icims", re.I),
}


def _slug_variants(name: str) -> List[str]:
    """Board-token guesses derived from the company name."""
    lowered = re.sub(r"[^a-z0-9 ]", "", name.lower())
    words = lowered.split()
    joined = "".join(words)
    variants = [
        joined,
        "-".join(words),
        words[0] if words else joined,
        joined + "industries",
        joined + "inc",
        joined.replace("the", "", 1) if joined.startswith("the") else joined,
    ]
    # Drop stop-words like "industries"/"corporation" and try the remainder.
    trimmed = [w for w in words if w not in {"the", "inc", "corporation", "corp", "company", "co"}]
    if trimmed and trimmed != words:
        variants.append("".join(trimmed))
    return [v for v in dict.fromkeys(variants) if len(v) >= 2]


class Discovery:
    def __init__(self, http: HttpClient) -> None:
        self.http = http

    async def discover(self, company: CompanyConfig) -> DiscoveryResult:
        result = DiscoveryResult(slug=company.slug)
        candidates = await self._collect_candidates(company)
        if not candidates:
            result.error = "no platform markers found and no board token matched"
            return result

        candidates.sort(key=lambda c: -c.confidence)
        for candidate in candidates:
            ok, count, error = await self._verify(company, candidate)

            # A candidate that answers but returns an implausibly small board
            # is almost certainly the wrong employer, not a quiet one.
            minimum = MIN_POSTINGS_BY_SOURCE.get(candidate.source, 1)
            if ok and count < minimum:
                ok = False
                error = (
                    f"only {count} postings from a {candidate.source} candidate "
                    f"(needs {minimum}+) — probably a different employer"
                )
                log.info(
                    "discovery_rejected_implausible",
                    company=company.slug,
                    connector=candidate.connector,
                    params=candidate.params,
                    postings=count,
                )

            result.tried.append((candidate.connector, "ok" if ok else (error or "no jobs")))
            if ok:
                result.connector = candidate.connector
                result.params = candidate.params
                result.jobs_found = count
                log.info(
                    "discovery_hit",
                    company=company.slug,
                    connector=candidate.connector,
                    jobs=count,
                    source=candidate.source,
                )
                return result

        result.error = "candidates found but none returned postings"
        return result

    # ------------------------------------------------------------------ #

    async def _collect_candidates(self, company: CompanyConfig) -> List[Candidate]:
        candidates: List[Candidate] = []
        html = ""
        final_url = company.careers_url or ""

        if company.careers_url:
            try:
                response, _ = await self.http.request(
                    "GET", company.careers_url, headers={"Accept": "text/html"}, use_cache=False
                )
                html = response.text
                final_url = str(response.url)
            except JobMonitorError as exc:
                # A blocked careers page is common (Tesla, Caterpillar). Slug
                # guessing may still find the board.
                log.debug("discovery_fetch_failed", company=company.slug, error=str(exc))

        haystack = f"{final_url}\n{html}"
        candidates.extend(self._from_fingerprint(haystack, final_url))
        candidates.extend(self._from_slug_guesses(company))
        return self._dedupe(candidates)

    def _from_fingerprint(self, haystack: str, final_url: str) -> List[Candidate]:
        found: List[Candidate] = []

        match = _WORKDAY_URL.search(haystack)
        if match:
            found.append(
                Candidate(
                    "workday",
                    {"tenant": match.group(1), "pod": match.group(2), "site": match.group(3)},
                    confidence=95,
                )
            )

        for pattern, connector, key in (
            (_GREENHOUSE_URL, "greenhouse", "board_token"),
            (_LEVER_URL, "lever", "board_token"),
            (_ASHBY_URL, "ashby", "board_token"),
            (_SMARTRECRUITERS_URL, "smartrecruiters", "company_id"),
            (_JOBVITE_URL, "jobvite", "company_id"),
        ):
            m = pattern.search(haystack)
            if m:
                found.append(Candidate(connector, {key: m.group(1)}, confidence=95))

        m = _ICIMS_URL.search(haystack)
        if m and m.group(1) not in {"www", "careers-api"}:
            found.append(Candidate("icims", {"tenant": m.group(1)}, confidence=90))

        m = _ORACLE_URL.search(haystack)
        if m:
            site_match = _ORACLE_SITE.search(haystack)
            found.append(
                Candidate(
                    "oracle",
                    {
                        "host": m.group(1),
                        "site_number": site_match.group(1) if site_match else "CX_1001",
                    },
                    confidence=85,
                )
            )

        m = _TALEO_URL.search(haystack)
        if m:
            found.append(Candidate("taleo", {"host": m.group(1)}, confidence=80))

        host = urlparse(final_url).netloc
        if host:
            domain = ".".join(host.split(".")[-2:])
            if _MARKERS["eightfold"].search(haystack):
                found.append(
                    Candidate("eightfold", {"host": host, "domain": domain}, confidence=75)
                )
            if _MARKERS["phenom"].search(haystack):
                locale = self._phenom_locale(final_url)
                found.append(Candidate("phenom", {"host": host, "locale": locale}, confidence=70))
            if _MARKERS["successfactors"].search(haystack):
                found.append(Candidate("successfactors", {"host": host}, confidence=65))
            if _MARKERS["avature"].search(haystack):
                found.append(Candidate("avature", {"host": host}, confidence=60))

            # Structured data beats bespoke HTML, so always offer it before
            # falling back to link scraping.
            if final_url:
                found.append(
                    Candidate("jsonld", {"url": final_url}, confidence=40, source="fallback")
                )
                found.append(
                    Candidate("html", {"url": final_url}, confidence=20, source="fallback")
                )

        return found

    @staticmethod
    def _phenom_locale(url: str) -> str:
        path = urlparse(url).path.strip("/")
        parts = path.split("/")
        if len(parts) >= 2 and len(parts[1]) == 2:
            return f"{parts[0]}/{parts[1]}"
        return "us/en"

    def _from_slug_guesses(self, company: CompanyConfig) -> List[Candidate]:
        """Try board tokens derived from the company name.

        Only for platforms with a flat, guessable namespace. Verification
        catches wrong guesses, so a broad net here is cheap.
        """
        candidates = []
        for variant in _slug_variants(company.name)[:6]:
            candidates.append(
                Candidate("greenhouse", {"board_token": variant}, confidence=50, source="guess")
            )
            candidates.append(
                Candidate("lever", {"board_token": variant}, confidence=45, source="guess")
            )
            candidates.append(
                Candidate("ashby", {"board_token": variant}, confidence=40, source="guess")
            )
            candidates.append(
                Candidate(
                    "smartrecruiters", {"company_id": variant}, confidence=35, source="guess"
                )
            )
        return candidates

    @staticmethod
    def _dedupe(candidates: List[Candidate]) -> List[Candidate]:
        seen = set()
        unique = []
        for candidate in candidates:
            key = (candidate.connector, tuple(sorted(candidate.params.items())))
            if key not in seen:
                seen.add(key)
                unique.append(candidate)
        return unique

    async def _verify(
        self, company: CompanyConfig, candidate: Candidate
    ) -> Tuple[bool, int, Optional[str]]:
        """Run the candidate for real. Only postings prove it works."""
        probe = company.model_copy(
            update={"connector": candidate.connector, "params": candidate.params}
        )
        try:
            connector_cls = get_connector_class(candidate.connector)
            connector = connector_cls(probe, self.http)
            postings = await connector.fetch_listings()
        except JobMonitorError as exc:
            return False, 0, str(exc)[:200]
        except Exception as exc:  # a malformed payload should not abort discovery
            return False, 0, f"{type(exc).__name__}: {exc}"[:200]

        if not postings:
            return False, 0, "returned zero postings"
        return True, len(postings), None
