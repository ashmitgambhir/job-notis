"""The scoring pipeline.

Ordered cheapest-first so that most postings are decided without ever touching
an embedding model or an API:

1. **Enrich** — classify employment type, work mode, clearance, sponsorship.
2. **Hard exclusions** — drop software/finance/senior/etc. outright. Roughly
   two thirds of a large board dies here, for free.
3. **Rule score** — weighted title / seniority / department / skills / degree.
4. **Semantic score** — embedding similarity against the role profile. This is
   what catches relevant postings whose wording the rules would miss.
5. **LLM adjudication** — only for scores inside the ambiguous band.

Two entry points: :meth:`score_title_only` for the prefilter (before a detail
fetch) and :meth:`score` for the full posting.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

import structlog

from ..config.schema import AppConfig
from ..core.models import JobPosting, MatchResult
from .classify import enrich
from .embeddings import SemanticMatcher
from .llm import LlmAdjudicator
from .rules import RuleEngine

log = structlog.get_logger(__name__)


class Scorer:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.rules = RuleEngine(config.roles, config.filters)
        self.semantic = SemanticMatcher(
            config.roles.semantic_profile,
            config.settings.matching.embeddings.model,
            enabled=config.settings.matching.embeddings.enabled,
        )
        self.llm = LlmAdjudicator(config.settings.matching.llm)
        self.weights = self._effective_weights(config.roles.weights)

    def _effective_weights(self, configured: dict) -> dict:
        """Drop the semantic component when only the lexical fallback is available.

        Lexical cosine and embedding cosine are not on the same scale — bag-of-words
        overlap between a posting and a profile sentence lands far lower than a
        sentence-transformer's similarity for the same pair. Feeding it into the
        same weighted average would systematically deduct ~15 points from every
        genuine match. Better to redistribute the weight across the components
        that *are* calibrated, and treat lexical overlap as a bonus instead.
        """
        if self.semantic.backend == "sentence-transformers":
            return dict(configured)
        remaining = {k: v for k, v in configured.items() if k != "semantic"}
        total = sum(remaining.values()) or 1.0
        log.info(
            "semantic_weight_redistributed",
            backend=self.semantic.backend,
            hint="install jobmonitor[embeddings] to enable the semantic component",
        )
        return {k: v / total for k, v in remaining.items()}

    # ------------------------------------------------------------------ #

    def score_title_only(self, posting: JobPosting) -> Tuple[float, Optional[str]]:
        """Cheap upper-bound estimate from the title alone.

        Used to decide whether a detail fetch is worth making. It must not
        under-estimate, or a real match gets discarded before its description
        is ever read — so the missing components are credited at a neutral
        value rather than zero.
        """
        posting.employment_type = enrich(posting).employment_type
        excluded = self.rules.hard_exclusion(posting)
        if excluded:
            return 0.0, excluded

        title_score, _ = self.rules.score_title(posting)
        seniority_score, _ = self.rules.score_seniority(posting)
        semantic_score = self.semantic.similarity(posting.title, cache_key=f"t:{posting.title}")

        # Assume a neutral 60 for everything a description would tell us.
        neutral = 60.0
        components = {
            "title": title_score,
            "seniority": seniority_score,
            "semantic": semantic_score,
            "department": neutral,
            "skills": neutral,
            "degree": neutral,
            "description": neutral,
        }
        estimate = sum(components[k] * self.weights.get(k, 0.0) for k in components)
        return estimate, None

    def worth_detail_fetch(self, posting: JobPosting) -> bool:
        """Would this posting still have a chance after a full read?"""
        estimate, excluded = self.score_title_only(posting)
        if excluded:
            return False
        # Slack for what a description can add over the neutral assumption.
        margin = 18.0
        return estimate + margin >= self.config.settings.matching.review_threshold

    # ------------------------------------------------------------------ #

    async def score(self, posting: JobPosting) -> MatchResult:
        """Full score for a posting whose description has been fetched."""
        enrich(posting)

        excluded = self.rules.hard_exclusion(posting)
        if excluded:
            return MatchResult(score=0, matched=False, excluded_by=excluded)

        components, reasons = self.rules.components(posting)
        semantic = self.semantic.similarity(
            posting.searchable_text(), cache_key=posting.content_hash
        )
        components["semantic"] = semantic
        if semantic >= 70:
            reasons.append(f"Semantically similar to target roles ({semantic:.0f}%)")

        base = sum(components.get(k, 0.0) * w for k, w in self.weights.items())
        if "semantic" not in self.weights and semantic >= 25:
            # Lexical fallback: a clear overlap is still evidence, just not
            # calibrated enough to carry a full weighted share.
            base += min(6.0, semantic / 8.0)

        penalty, penalty_notes = self.rules.penalties(posting)
        bonus, bonus_notes = self.rules.bonuses(posting)
        score = base - penalty + bonus + self.company_bonus(posting)

        # An early-career role in a relevant discipline is the exact thing this
        # system exists to catch. The weighted average alone under-rates it,
        # because short student postings score low on skills and degree — the
        # components most likely to be missing from a two-paragraph listing.
        if posting.employment_type.value in {"intern", "co-op", "new_grad"}:
            title_component = components.get("title", 0.0)
            if title_component >= 70:
                score += 6.0
            elif title_component >= 45:
                score += 3.0

        llm_used = False
        llm_verdict: Optional[str] = None
        if self.llm.should_adjudicate(score):
            verdict = await self.llm.adjudicate(posting)
            if verdict is not None:
                llm_used = True
                llm_verdict = "relevant" if verdict.relevant else "not relevant"
                # Blend rather than replace: the rules encode the user's own
                # configuration, which the model has not seen.
                score = 0.55 * score + 0.45 * verdict.score
                reasons.extend(verdict.reasons)
                penalty_notes.extend(verdict.concerns)
                components["llm"] = verdict.score

        score = max(0.0, min(100.0, score))
        threshold = self.config.settings.matching.threshold
        final = int(round(score))

        result = MatchResult(
            score=final,
            matched=final >= threshold,
            reasons=self._polish(reasons, posting),
            components={k: round(v, 1) for k, v in components.items()},
            llm_used=llm_used,
            llm_verdict=llm_verdict,
        )
        if penalty_notes:
            result.components["penalty"] = float(penalty)
        if bonus_notes:
            reasons.extend(bonus_notes)
        return result

    def company_bonus(self, posting: JobPosting) -> float:
        for company in self.config.companies.companies:
            if company.slug == posting.company_slug:
                return float(company.score_bonus)
        return 0.0

    @staticmethod
    def _polish(reasons: List[str], posting: JobPosting) -> List[str]:
        """De-duplicate and front-load the reasons shown in a notification."""
        seen = set()
        ordered: List[str] = []
        employment = posting.employment_type.value.replace("_", " ").title()
        if posting.employment_type.value in {"intern", "co-op", "new_grad"}:
            ordered.append(employment)
            seen.add(employment)
        for reason in reasons:
            key = reason.lower()
            if key not in seen:
                seen.add(key)
                ordered.append(reason)
        return ordered[:6]

    def stats(self) -> dict:
        return {
            "semantic_backend": self.semantic.backend,
            "llm": self.llm.stats(),
        }
