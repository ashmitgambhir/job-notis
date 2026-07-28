"""Text normalisation shared by every matching stage.

Job titles are written inconsistently ("Sr.", "Senior", "Engineer II",
"Engineer 2", "Co-Op", "Coop"), so all comparisons run against a normalised
form rather than the raw string.
"""

from __future__ import annotations

import re
import unicodedata
from typing import List, Set

# Applied before tokenising so that variants collapse onto one spelling.
_SYNONYMS = [
    (re.compile(r"\bsr\.?\b", re.I), "senior"),
    (re.compile(r"\bjr\.?\b", re.I), "junior"),
    (re.compile(r"\bmech\b", re.I), "mechanical"),
    (re.compile(r"\bmfg\b", re.I), "manufacturing"),
    (re.compile(r"\beng\.?\b", re.I), "engineer"),
    (re.compile(r"\bengr\.?\b", re.I), "engineer"),
    # Collapse the noun/gerund split so "Robotics Engineering Intern" matches
    # the configured "Robotics Engineer". Applied to both the posting and the
    # config, so the comparison stays symmetric.
    (re.compile(r"\bengineering\b", re.I), "engineer"),
    (re.compile(r"\bco[\s\-_]?op\b", re.I), "coop"),
    (re.compile(r"\bintern(ship)?\b", re.I), "intern"),
    (re.compile(r"\br\s*&\s*d\b", re.I), "research development"),
    (re.compile(r"\bnew[\s\-]?grad(uate)?\b", re.I), "newgrad"),
    (re.compile(r"\bentry[\s\-]?level\b", re.I), "entrylevel"),
    (re.compile(r"\bu\.?s\.?\b", re.I), "us"),
    # Roman numerals in job levels; "Engineer II" and "Engineer 2" are the same.
    (re.compile(r"\bengineer\s+i\b", re.I), "engineer 1"),
    (re.compile(r"\bengineer\s+ii\b", re.I), "engineer 2"),
    (re.compile(r"\bengineer\s+iii\b", re.I), "engineer 3"),
]

_PUNCT = re.compile(r"[^\w\s&+#/-]")
_WS = re.compile(r"\s+")

STOPWORDS: Set[str] = {
    "a", "an", "the", "and", "or", "of", "for", "to", "in", "on", "at", "with",
    "by", "from", "as", "is", "are", "be", "will", "we", "you", "our", "your",
    "this", "that", "it", "its", "their", "they", "have", "has", "can", "all",
    "job", "role", "position", "opportunity", "team", "work", "working",
}


def normalize(text: str) -> str:
    """Lowercase, de-accent, expand synonyms, collapse whitespace."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    for pattern, replacement in _SYNONYMS:
        text = pattern.sub(replacement, text)
    text = _PUNCT.sub(" ", text)
    return _WS.sub(" ", text).strip()


def tokenize(text: str, *, drop_stopwords: bool = True) -> List[str]:
    tokens = normalize(text).split()
    if drop_stopwords:
        return [t for t in tokens if t not in STOPWORDS and len(t) > 1]
    return tokens


def token_set(text: str) -> Set[str]:
    return set(tokenize(text))


def contains_phrase(haystack_normalized: str, phrase: str) -> bool:
    """Whole-phrase containment on already-normalised text.

    Word-boundary anchored so "design" does not match "redesigned".
    """
    needle = normalize(phrase)
    if not needle:
        return False
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack_normalized) is not None
