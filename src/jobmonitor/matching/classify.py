"""Attribute extraction from a posting.

Everything here is cheap regex work over text we already have. It runs on every
posting before scoring, and its output feeds both the filters and the
notification body (employment type, work mode, clearance, sponsorship,
deadline).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional, Tuple

from ..core.models import EmploymentType, JobPosting, WorkMode
from .normalize import normalize

# --- Employment type ------------------------------------------------------- #

_COOP = re.compile(r"\bco[\s\-_]?op\b|\bcooperative education\b", re.I)
_INTERN = re.compile(r"\bintern(ship)?\b|\bsummer (analyst|associate|20\d\d)\b|\bstudent\b", re.I)
_NEW_GRAD = re.compile(
    r"\bnew grad(uate)?\b|\bentry[\s\-]?level\b|\bearly career\b|\buniversity (hire|grad)\b|"
    r"\bcampus hire\b|\brotational program\b|\bleadership development program\b|"
    r"\bgraduate (engineer|program|scheme)\b",
    re.I,
)
# Body-level intern detection: the posting must be describing the role itself.
_BODY_INTERN = re.compile(
    # "this internship" describes the role; "our internship programme" is
    # company boilerplate that appears on full-time postings too.
    r"\b(this|the)\s+(paid\s+)?(internship|co-?op)\b"
    r"|\bas an intern\b"
    r"|\bthe intern(s)? will\b"
    r"|\binterns?\s+(will|are expected to|report to)\b"
    r"|\b(summer|fall|spring|winter)\s+20\d\d\s+intern(ship)?\b"
    r"|\bintern(ship)? (position|role|opportunity)\b"
    r"|\bcurrently (enrolled|pursuing)\b",
    re.I,
)
_CONTRACT = re.compile(r"\bcontract(or)?\b|\btemporary\b|\bfixed[\s\-]term\b|\bc2c\b", re.I)
_PART_TIME = re.compile(r"\bpart[\s\-]?time\b", re.I)

# --- Work mode ------------------------------------------------------------- #

_REMOTE = re.compile(
    r"\b(fully |100% )?remote\b|\bwork from home\b|\bwfh\b|\btelecommut|\bvirtual position\b", re.I
)
_HYBRID = re.compile(
    r"\bhybrid\b|\d\s*days? (per week )?(in|on)[\s\-]site|\bflexible workplace\b", re.I
)
_ONSITE = re.compile(r"\bon[\s\-]?site\b|\bin[\s\-]?office\b|\bin[\s\-]?person\b", re.I)
_NOT_REMOTE = re.compile(r"\bnot (a )?remote\b|\bno remote\b|\bremote work is not\b", re.I)

# --- Clearance / sponsorship ---------------------------------------------- #

_CLEARANCE = re.compile(
    r"\b(security clearance|secret clearance|top secret|ts/sci|dod clearance|"
    r"active clearance|q clearance|l clearance|polygraph)\b",
    re.I,
)
# A clearance the employer will sponsor is not a barrier the way an existing
# active clearance is.
_CLEARANCE_OPTIONAL = re.compile(
    r"\b(ability to obtain|able to obtain|eligible to obtain|will sponsor .{0,20}clearance|"
    r"clearance is (a )?plus|preferred but not required)\b",
    re.I,
)

_NO_SPONSORSHIP = re.compile(
    r"\b(not (be )?(able|willing) to sponsor|no (visa )?sponsorship|"
    r"without (the need for )?sponsorship|"
    r"does not (offer|provide) sponsorship|unable to sponsor|will not sponsor|"
    r"not provide (immigration |visa )?sponsorship)\b",
    re.I,
)
_YES_SPONSORSHIP = re.compile(
    r"\b(will sponsor|sponsorship (is )?available|offers? (visa )?sponsorship|"
    r"visa sponsorship (is )?provided)\b",
    re.I,
)
_US_PERSON = re.compile(
    r"\b(u\.?s\.? person|us citizen(ship)?|permanent resident|green card|itar|"
    r"export control)\b",
    re.I,
)

_DEADLINE = re.compile(
    r"(?:apply(?:ing)? by|applications? (?:close|due|deadline)|closing date|deadline(?: to apply)?)"
    r"[:\s]*((?:\w{3,9}\s+\d{1,2},?\s+\d{4})|(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}))",
    re.I,
)


def classify_employment_type(posting: JobPosting) -> EmploymentType:
    """Type from the strongest available signal.

    The title dominates: a full-time posting that merely mentions the intern
    programme in its boilerplate should not be classified as an internship.
    """
    title = posting.title or ""
    raw = posting.raw or {}

    # Some platforms label this outright; trust them over any heuristic.
    if raw.get("is_intern") is True:
        return EmploymentType.INTERN
    declared = str(raw.get("employment_type") or raw.get("employment_type_raw") or "")
    if declared:
        if _INTERN.search(declared):
            return EmploymentType.INTERN
        if _PART_TIME.search(declared):
            return EmploymentType.PART_TIME
        if _CONTRACT.search(declared):
            return EmploymentType.CONTRACT

    if _COOP.search(title):
        return EmploymentType.COOP
    if _INTERN.search(title):
        return EmploymentType.INTERN
    if _NEW_GRAD.search(title):
        return EmploymentType.NEW_GRAD

    # In the body, require language that describes *this* role rather than a
    # passing mention. A full-time posting whose boilerplate advertises the
    # company's internship programme is not an internship.
    body = posting.description or ""
    if _COOP.search(body[:1500]):
        return EmploymentType.COOP
    if _BODY_INTERN.search(body[:2000]):
        return EmploymentType.INTERN
    if _NEW_GRAD.search(body[:2500]):
        return EmploymentType.NEW_GRAD
    if _CONTRACT.search(title) or _CONTRACT.search(body[:1200]):
        return EmploymentType.CONTRACT
    if _PART_TIME.search(title):
        return EmploymentType.PART_TIME

    time_type = str(raw.get("time_type") or "")
    if time_type.lower().startswith("full"):
        return EmploymentType.FULL_TIME
    if time_type.lower().startswith("part"):
        return EmploymentType.PART_TIME

    return EmploymentType.FULL_TIME if title else EmploymentType.UNKNOWN


def classify_work_mode(posting: JobPosting) -> WorkMode:
    location = posting.location or ""
    raw_remote = str((posting.raw or {}).get("remote_type") or "")
    haystack = f"{posting.title} {location} {raw_remote}"

    if _HYBRID.search(haystack):
        return WorkMode.HYBRID
    if _REMOTE.search(haystack) and not _NOT_REMOTE.search(haystack):
        return WorkMode.REMOTE

    body = (posting.description or "")[:3000]
    if _HYBRID.search(body):
        return WorkMode.HYBRID
    if _NOT_REMOTE.search(body):
        return WorkMode.ONSITE
    if _REMOTE.search(body):
        return WorkMode.REMOTE
    if _ONSITE.search(body) or location:
        return WorkMode.ONSITE
    return WorkMode.UNKNOWN


def detect_clearance(text: str) -> Optional[bool]:
    """True when an existing clearance appears to be required.

    Returns None when the posting says nothing, so "unknown" stays
    distinguishable from "explicitly not required".
    """
    if not text:
        return None
    if not _CLEARANCE.search(text):
        return None
    window = _CLEARANCE.search(text)
    start = max(0, window.start() - 220)
    context = text[start : window.end() + 220]
    if _CLEARANCE_OPTIONAL.search(context):
        return False
    return True


def detect_sponsorship(text: str) -> Optional[bool]:
    """True if sponsorship is offered, False if explicitly refused."""
    if not text:
        return None
    if _NO_SPONSORSHIP.search(text):
        return False
    if _YES_SPONSORSHIP.search(text):
        return True
    return None


def detect_us_person_requirement(text: str) -> bool:
    return bool(text) and bool(_US_PERSON.search(text))


def detect_deadline(text: str) -> Optional[datetime]:
    if not text:
        return None
    match = _DEADLINE.search(text)
    if not match:
        return None
    from dateutil import parser as date_parser

    try:
        parsed = date_parser.parse(match.group(1), fuzzy=True)
    except (ValueError, OverflowError, TypeError):
        return None
    from datetime import timezone

    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def enrich(posting: JobPosting) -> JobPosting:
    """Populate every derived attribute on a posting, in place."""
    posting.employment_type = classify_employment_type(posting)
    posting.work_mode = classify_work_mode(posting)

    body = posting.description or ""
    if body:
        posting.requires_clearance = detect_clearance(body)
        posting.offers_sponsorship = detect_sponsorship(body)
        if posting.deadline is None:
            posting.deadline = detect_deadline(body)
    return posting


def location_country(location: Optional[str]) -> Tuple[str, bool]:
    """Return ``(normalised_location, looks_like_us)``.

    Used by the location filter. US detection is deliberately generous: a
    posting listing only "Peoria, IL" should not be dropped for lacking the
    word "United States".
    """
    if not location:
        return "", False
    text = normalize(location)
    if re.search(r"\b(united states|usa|u s a|us)\b", text):
        return text, True
    if re.search(r"\bremote\b", text) and not re.search(r"\b(emea|apac|india|europe)\b", text):
        return text, True
    # Two-letter state codes, e.g. "peoria il".
    if re.search(
        r"\b(al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|ky|la|me|md|ma|mi|mn|ms|mo|mt|"
        r"ne|nv|nh|nj|nm|ny|nc|nd|oh|ok|or|pa|ri|sc|sd|tn|tx|ut|vt|va|wa|wv|wi|wy|dc)\b",
        text,
    ):
        return text, True
    return text, False
