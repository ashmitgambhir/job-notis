"""Semantic similarity between a posting and the target role profile.

This is what catches relevant postings whose titles contain none of the
configured words — "Propulsion Development Rotational Program", say.

Two backends:

* **sentence-transformers** when the optional extra is installed. Runs locally,
  no API cost, ~80MB model.
* **lexical cosine** otherwise. Weaker, but it keeps the system functional on a
  bare install instead of silently dropping a scoring component.

Embeddings are cached by content hash because the same posting is re-scored on
every cycle until it closes.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Dict, List, Optional, Sequence

import structlog

from .normalize import tokenize

log = structlog.get_logger(__name__)


class SemanticMatcher:
    """Cosine similarity against a set of profile sentences."""

    def __init__(self, profile: Sequence[str], model_name: str, enabled: bool = True) -> None:
        self.profile = list(profile)
        self.model_name = model_name
        self.enabled = enabled and bool(profile)
        self._model = None
        self._profile_vectors = None
        self._cache: Dict[str, float] = {}
        self.backend = "disabled"
        if self.enabled:
            self._init_backend()

    def _init_backend(self) -> None:
        try:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name)
            self._profile_vectors = self._model.encode(
                self.profile, normalize_embeddings=True, show_progress_bar=False
            )
            self.backend = "sentence-transformers"
            log.info("semantic_backend_ready", backend=self.backend, model=self.model_name)
        except ImportError:
            self.backend = "lexical"
            log.info(
                "semantic_backend_fallback",
                backend="lexical",
                hint="pip install 'jobmonitor[embeddings]' for true semantic matching",
            )
        except Exception as exc:
            # A corrupt model cache or offline first-run should degrade, not
            # take the scanner down.
            self.backend = "lexical"
            log.warning("semantic_model_failed", error=str(exc), fallback="lexical")

    def similarity(self, text: str, cache_key: Optional[str] = None) -> float:
        """Return 0..100 similarity to the closest profile sentence."""
        if not self.enabled or not text:
            return 0.0
        if cache_key and cache_key in self._cache:
            return self._cache[cache_key]

        score = (
            self._transformer_similarity(text)
            if self.backend == "sentence-transformers"
            else self._lexical_similarity(text)
        )
        if cache_key:
            # Unbounded growth would matter at 500 companies; clear wholesale
            # rather than tracking per-entry recency for a cheap value.
            if len(self._cache) > 50_000:
                self._cache.clear()
            self._cache[cache_key] = score
        return score

    def _transformer_similarity(self, text: str) -> float:
        assert self._model is not None
        # Truncate: the model has a token limit and the first part of a
        # posting carries the signal anyway.
        vector = self._model.encode(
            text[:2000], normalize_embeddings=True, show_progress_bar=False
        )
        best = max(float(vector @ profile) for profile in self._profile_vectors)
        # Cosine over normalised vectors is in [-1, 1]; map to [0, 100].
        return max(0.0, min(100.0, (best + 1.0) * 50.0))

    def _lexical_similarity(self, text: str) -> float:
        target = Counter(tokenize(text[:4000]))
        if not target:
            return 0.0
        best = 0.0
        for sentence in self.profile:
            best = max(best, _cosine(target, Counter(tokenize(sentence))))
        return max(0.0, min(100.0, best * 100.0))


def _cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    shared = set(a) & set(b)
    if not shared:
        return 0.0
    numerator = sum(a[t] * b[t] for t in shared)
    norm_a = math.sqrt(sum(v * v for v in a.values()))
    norm_b = math.sqrt(sum(v * v for v in b.values()))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return numerator / (norm_a * norm_b)


_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def summarize(text: str, max_sentences: int = 3) -> str:
    """Trim a description down for LLM input or digest display."""
    if not text:
        return ""
    sentences: List[str] = [s.strip() for s in _SENTENCE.split(text) if s.strip()]
    return " ".join(sentences[:max_sentences])
