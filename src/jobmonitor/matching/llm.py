"""LLM adjudication for borderline postings.

Deliberately *not* a pipeline stage. Rules and embeddings score every posting;
the LLM is consulted only for the ones whose score lands in the ambiguous band
around the threshold, and only up to a per-cycle call cap. On a normal cycle
that is a handful of calls, not thousands — which is what keeps the feature
affordable enough to leave switched on.

Off by default. Set ``matching.llm.enabled: true`` and ``ANTHROPIC_API_KEY``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import structlog

from ..config.schema import LlmSettings
from ..core.models import JobPosting

log = structlog.get_logger(__name__)

SYSTEM_PROMPT = """\
You screen job postings for an undergraduate Mechanical Engineering student \
looking for internships, co-ops and new-grad roles.

Decide whether the posting is a realistic and relevant target for that student.

Count as relevant:
- Internships, co-ops and new-grad/entry-level roles in mechanical, design, \
manufacturing, industrial, test, validation, controls, robotics, thermal, \
aerospace, automotive or general hardware engineering.
- Rotational and engineering leadership development programmes.
- Roles whose title does not say "mechanical" but whose work is clearly \
mechanical or hardware engineering.

Do NOT count as relevant:
- Pure software, data science, IT, business, finance, marketing, sales, HR, \
legal, clinical or support roles. A hardware-adjacent role that involves some \
programming is still relevant.
- Roles requiring substantial professional experience (roughly 3+ years), a \
PhD, or a seniority level a student cannot hold (senior, staff, principal, \
lead, manager and above).

Judge the role itself, not how enthusiastically it is written. Be decisive: \
this is a screening step, and a posting that is merely adjacent should score low.
"""

# Constrains the response so it can be parsed without defensive text handling.
RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "relevant": {
            "type": "boolean",
            "description": "True if this role suits a mechanical engineering student.",
        },
        "confidence": {
            "type": "integer",
            "description": "0-100 confidence that the posting is a good match.",
        },
        "employment_type": {
            "type": "string",
            "enum": ["intern", "co-op", "new_grad", "full_time", "contract", "unknown"],
        },
        "reasons": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Up to three short phrases explaining the verdict.",
        },
        "concerns": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Disqualifying or worrying requirements, if any.",
        },
    },
    "required": ["relevant", "confidence", "employment_type", "reasons", "concerns"],
    "additionalProperties": False,
}


@dataclass
class LlmVerdict:
    relevant: bool
    confidence: int
    employment_type: str
    reasons: List[str]
    concerns: List[str]

    @property
    def score(self) -> float:
        """Map the verdict onto the same 0-100 scale as the other components."""
        return float(self.confidence) if self.relevant else float(max(0, 40 - self.confidence // 2))


class LlmAdjudicator:
    def __init__(self, settings: LlmSettings) -> None:
        self.settings = settings
        self._client: Optional[Any] = None
        self.calls_this_cycle = 0
        self.total_calls = 0
        self.failures = 0
        self.available = False
        if settings.enabled:
            self._init_client()

    def _init_client(self) -> None:
        if not self.settings.api_key:
            log.warning("llm_disabled_no_api_key", hint="set ANTHROPIC_API_KEY")
            return
        try:
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic(
                api_key=self.settings.api_key, timeout=self.settings.timeout_seconds
            )
            self.available = True
            log.info("llm_adjudicator_ready", model=self.settings.model)
        except ImportError:
            log.warning(
                "llm_disabled_missing_package", hint="pip install 'jobmonitor[llm]'"
            )

    def reset_cycle(self) -> None:
        self.calls_this_cycle = 0

    def should_adjudicate(self, score: float) -> bool:
        """Only spend a call where the rules are genuinely undecided."""
        if not self.available:
            return False
        if self.calls_this_cycle >= self.settings.max_calls_per_cycle:
            return False
        return self.settings.adjudicate_band_low <= score <= self.settings.adjudicate_band_high

    async def adjudicate(self, posting: JobPosting) -> Optional[LlmVerdict]:
        if not self.available or self._client is None:
            return None

        self.calls_this_cycle += 1
        self.total_calls += 1
        try:
            response = await self._client.messages.create(
                model=self.settings.model,
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                # Low effort: this is a short classification, not deep analysis.
                # Adaptive thinking stays on — disabling it can leak internal
                # tags into the response on current models.
                thinking={"type": "adaptive"},
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": RESPONSE_SCHEMA},
                },
                messages=[{"role": "user", "content": self._render(posting)}],
            )
        except Exception as exc:
            # A screening aid must never take the scanner down; the rule score
            # already stands on its own.
            self.failures += 1
            log.warning("llm_call_failed", company=posting.company_slug, error=str(exc)[:200])
            return None

        if getattr(response, "stop_reason", None) == "refusal":
            log.warning("llm_refused", company=posting.company_slug, title=posting.title[:60])
            return None

        text = next((b.text for b in response.content if getattr(b, "type", "") == "text"), "")
        if not text:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            log.warning("llm_bad_json", snippet=text[:160])
            return None

        return LlmVerdict(
            relevant=bool(data.get("relevant")),
            confidence=int(data.get("confidence") or 0),
            employment_type=str(data.get("employment_type") or "unknown"),
            reasons=[str(r) for r in (data.get("reasons") or [])][:3],
            concerns=[str(c) for c in (data.get("concerns") or [])][:3],
        )

    def _render(self, posting: JobPosting) -> str:
        description = (posting.description or "")[: self.settings.max_description_chars]
        parts = [
            f"Company: {posting.company}",
            f"Title: {posting.title}",
            f"Department: {posting.department or 'not stated'}",
            f"Location: {posting.location or 'not stated'}",
            "",
            "Description:",
            description or "(no description available)",
        ]
        return "\n".join(parts)

    def stats(self) -> Dict[str, Any]:
        return {
            "available": self.available,
            "calls_this_cycle": self.calls_this_cycle,
            "total_calls": self.total_calls,
            "failures": self.failures,
        }
