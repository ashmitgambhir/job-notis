"""Deterministic scoring components.

These run on every posting and produce most of the final score. They are fast,
explainable and free — the semantic and LLM stages exist only to rescue
relevant postings whose wording these rules would miss.

Each function returns ``(score_0_to_100, reasons)`` so the notification can say
*why* something matched rather than just how much.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from ..config.schema import FiltersConfig, RolesConfig
from ..core.models import JobPosting
from .normalize import contains_phrase, normalize

Scored = Tuple[float, List[str]]


class RuleEngine:
    """Pre-compiles every pattern once; scoring is then pure lookup."""

    def __init__(self, roles: RolesConfig, filters: FiltersConfig) -> None:
        self.roles = roles
        self.filters = filters

        self._exclusions = [
            (pattern, re.compile(pattern)) for pattern in filters.exclude_title_patterns
        ]
        self._penalties = [
            (re.compile(pattern), points) for pattern, points in filters.penalty_patterns.items()
        ]
        self._seniority = [
            (rule, [re.compile(p, re.I) for p in rule.patterns]) for rule in roles.seniority
        ]
        self._excluded_departments = {normalize(d) for d in filters.exclude_departments}
        self._degrees = [normalize(d) for d in roles.degrees]
        self._skills = [normalize(s) for s in roles.skills]
        # Longest titles first so "Mechanical Design Engineer" is credited
        # before the shorter "Design Engineer" can claim it.
        self._families = [
            (
                family,
                sorted((normalize(t) for t in family.titles), key=len, reverse=True),
                [normalize(k) for k in family.keywords],
            )
            for family in roles.families
        ]

    # -------------------------------------------------------- exclusions --

    def hard_exclusion(self, posting: JobPosting) -> Optional[str]:
        """Return the reason this posting should be dropped before scoring."""
        title = posting.title or ""
        for raw_pattern, compiled in self._exclusions:
            if compiled.search(title):
                return f"title matched exclusion: {self._describe(raw_pattern)}"

        department = normalize(posting.department or "")
        if department and department in self._excluded_departments:
            return f"excluded department: {posting.department}"

        # Employment type is only trustworthy once the description has been
        # read. Judged on a title alone, an internship whose title does not say
        # "intern" — Deere's "Part Time Student - Product Engineering", say —
        # classifies as full time and would be thrown away before anything
        # fetched its description. So while the detail is still pending, defer:
        # let the posting through the prefilter and apply this rule on the
        # second pass, when the classifier has the full text to work with.
        if (
            self.filters.employment_types
            and not posting.detail_pending
            and posting.employment_type.value != "unknown"
        ):
            allowed = {t.lower() for t in self.filters.employment_types}
            if posting.employment_type.value not in allowed:
                return f"employment type not wanted: {posting.employment_type.value}"

        if self.filters.exclude_requiring_clearance and posting.requires_clearance:
            return "requires an active security clearance"

        if self.filters.exclude_no_sponsorship and posting.offers_sponsorship is False:
            return "explicitly offers no visa sponsorship"

        return self._location_exclusion(posting)

    def _location_exclusion(self, posting: JobPosting) -> Optional[str]:
        from .classify import location_country

        location_filter = self.filters.location
        if not posting.location:
            return None
        text, looks_us = location_country(posting.location)

        for denied in location_filter.deny:
            if contains_phrase(text, denied):
                return f"location on deny list: {denied}"

        if location_filter.allow:
            if not any(contains_phrase(text, allowed) for allowed in location_filter.allow):
                return f"location not on allow list: {posting.location}"

        if location_filter.countries:
            wants_us = any(
                c.strip().lower() in {"united states", "usa", "us"}
                for c in location_filter.countries
            )
            if wants_us and not looks_us:
                matched = any(contains_phrase(text, c) for c in location_filter.countries)
                if not matched:
                    return f"location outside target countries: {posting.location}"

        if not location_filter.allow_remote and "remote" in text:
            return "remote roles excluded"

        return None

    @staticmethod
    def _describe(pattern: str) -> str:
        """Turn a regex into something a human can read in an SMS."""
        words = re.findall(r"[a-z][a-z ]{3,}", pattern.lower())
        cleaned = [w.strip() for w in words if len(w.strip()) > 3][:3]
        return ", ".join(cleaned) if cleaned else "excluded category"

    # ------------------------------------------------------------ title --

    def score_title(self, posting: JobPosting) -> Scored:
        title = normalize(posting.title or "")
        if not title:
            return 0.0, []

        best = 0.0
        reasons: List[str] = []
        for family, titles, keywords in self._families:
            for candidate in titles:
                if contains_phrase(title, candidate):
                    # Exact title equality is a stronger signal than the title
                    # merely containing the phrase.
                    weight = float(family.weight) if title == candidate else family.weight * 0.94
                    if weight > best:
                        best = weight
                        reasons = [f"Title matches {family.name.replace('_', ' ')}"]
                    break
            else:
                hits = [k for k in keywords if contains_phrase(title, k)]
                if hits:
                    weight = family.weight * 0.55
                    if weight > best:
                        best = weight
                        reasons = [f"Title keyword: {hits[0]}"]

        if best == 0.0 and re.search(r"\bengineer(ing)?\b", title):
            # Generic engineering titles still deserve a floor; the semantic
            # stage decides whether they are actually relevant.
            best = 45.0
            reasons = ["Generic engineering title"]

        return min(best, 100.0), reasons

    # -------------------------------------------------------- seniority --

    def score_seniority(self, posting: JobPosting) -> Scored:
        haystack = f"{posting.title} {(posting.description or '')[:2500]}"
        title_only = posting.title or ""

        # Within the title, the *most senior* marker wins. Titles routinely
        # carry two ("Senior Mechanical Engineer I" matches both `senior` and
        # `engineer i"), and taking the highest weight would read that as a
        # new-grad role. Taking the lowest is the safe direction: a seniority
        # marker disqualifies, it does not average out.
        best_weight: Optional[int] = None
        best_name = ""
        for rule, patterns in self._seniority:
            for pattern in patterns:
                if pattern.search(title_only):
                    if best_weight is None or rule.weight < best_weight:
                        best_weight, best_name = rule.weight, rule.name
                    break

        # Only if the title says nothing do we fall back to the description,
        # where the highest marker wins — boilerplate mentioning senior
        # colleagues should not sink an internship.
        if best_weight is None:
            for rule, patterns in self._seniority:
                for pattern in patterns:
                    if pattern.search(haystack):
                        if best_weight is None or rule.weight > best_weight:
                            best_weight, best_name = rule.weight, rule.name
                        break

        if best_weight is None:
            return 50.0, []
        # Map the configured -100..100 weight onto 0..100.
        normalised = max(0.0, min(100.0, (best_weight + 100) / 2.0))
        label = best_name.replace("_", " ")
        return normalised, [f"Seniority: {label}"] if best_weight >= 70 else []

    # ------------------------------------------------------- department --

    def score_department(self, posting: JobPosting) -> Scored:
        if not posting.department:
            return 50.0, []
        department = normalize(posting.department)
        for family, _titles, keywords in self._families:
            if any(contains_phrase(department, k) for k in keywords):
                return float(family.weight), [f"Department: {posting.department}"]
        if re.search(r"\bengineer(ing)?\b|\bmanufactur|\boperations\b|\bproduct\b|\bresearch\b",
                     department):
            return 75.0, [f"Department: {posting.department}"]
        return 35.0, []

    # ----------------------------------------------------------- skills --

    def score_skills(self, posting: JobPosting) -> Scored:
        text = normalize(posting.searchable_text())
        if not text:
            return 0.0, []
        hits = [s for s in self._skills if contains_phrase(text, s)]
        if not hits:
            return 0.0, []
        # Six relevant skills is already a confident match; more adds nothing.
        score = min(100.0, len(hits) * 100.0 / 6.0)
        return score, [f"Skills: {', '.join(hits[:4])}"]

    # ----------------------------------------------------------- degree --

    def score_degree(self, posting: JobPosting) -> Scored:
        text = normalize(posting.searchable_text())
        if not text:
            return 50.0, []
        hits = [d for d in self._degrees if contains_phrase(text, d)]
        if not hits:
            # Neutral, not negative. Plenty of legitimate intern postings never
            # name a degree, and penalising silence pushed real matches under
            # the threshold.
            return 50.0, []
        preferred = [h for h in hits if h != "engineering"]
        if preferred:
            return 100.0, [f"Degree: {preferred[0]}"]
        return 70.0, []

    # ------------------------------------------------------ description --

    def score_description(self, posting: JobPosting) -> Scored:
        body = posting.description or ""
        if not body:
            return 50.0, []
        normalised = normalize(body[:6000])
        signals = 0
        for _family, _titles, keywords in self._families:
            if any(contains_phrase(normalised, k) for k in keywords):
                signals += 1
        return min(100.0, signals * 25.0), []

    # --------------------------------------------------------- penalties --

    def penalties(self, posting: JobPosting) -> Tuple[int, List[str]]:
        text = f"{posting.title}\n{posting.description or ''}"
        total = 0
        notes: List[str] = []
        for pattern, points in self._penalties:
            if pattern.search(text):
                total += points
                notes.append(f"-{points}: {self._describe(pattern.pattern)}")
        return total, notes

    def bonuses(self, posting: JobPosting) -> Tuple[int, List[str]]:
        location_filter = self.filters.location
        if not posting.location or not location_filter.preferred:
            return 0, []
        text = normalize(posting.location)
        for preferred in location_filter.preferred:
            if contains_phrase(text, preferred):
                return location_filter.preferred_bonus, [f"Preferred location: {preferred}"]
        return 0, []

    def components(self, posting: JobPosting) -> Tuple[Dict[str, float], List[str]]:
        """Every rule-based component in one pass."""
        results = {
            "title": self.score_title(posting),
            "seniority": self.score_seniority(posting),
            "department": self.score_department(posting),
            "skills": self.score_skills(posting),
            "degree": self.score_degree(posting),
            "description": self.score_description(posting),
        }
        scores = {key: value[0] for key, value in results.items()}
        reasons: List[str] = []
        for value in results.values():
            reasons.extend(value[1])
        return scores, reasons
