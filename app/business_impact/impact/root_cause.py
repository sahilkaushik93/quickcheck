"""Produce bounded non-causal root-cause candidates from aggregate patterns."""

from __future__ import annotations

from hashlib import sha256

from app.business_impact.evidence.correlator import AggregateEvidenceCorrelation
from app.business_impact.impact.models import RootCauseCandidate


class RootCauseAnalyzer:
    """Transforms aggregate co-occurrence into review candidates, never causality."""

    def __init__(self, maximum_candidates: int = 10) -> None:
        if maximum_candidates < 1: raise ValueError("maximum_candidates must be positive")
        self._maximum_candidates = maximum_candidates

    def analyze(self, correlations: tuple[AggregateEvidenceCorrelation, ...]) -> tuple[RootCauseCandidate, ...]:
        """Return deterministic candidates based solely on anonymous aggregate evidence."""

        candidates: list[RootCauseCandidate] = []
        for item in sorted(correlations, key=lambda value: value.correlation_id)[: self._maximum_candidates]:
            facts = [f"fact-correlation-{item.correlation_id.removeprefix('corr-')}"]
            category = "repeated_technical_pattern" if item.correlation_type == "repeated_period_pattern" else "aggregate_technical_cooccurrence"
            candidates.append(RootCauseCandidate(candidate_id="root-cause-" + sha256(item.correlation_id.encode()).hexdigest()[:24], category=category, statement=(item.statement + " Review approved upstream transformations, configuration changes, and source-coverage changes as possible explanations; no causal conclusion is made."), supporting_fact_ids=facts, confidence=None, requires_review=True, causal_claim=False))
        return tuple(candidates)
