"""Build bounded factual statements before the final trusted-insight LLM call."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from app.business_impact.impact.models import GovernedRecommendation, ImpactHypothesis
from app.business_impact.scoring.models import RawMetricObservation, RuleQualityScore


@dataclass(frozen=True, slots=True)
class DeterministicInsightStatement:
    statement_id: str
    statement: str
    internal_fact_ids: tuple[str, ...]
    statement_type: str = "fact"


@dataclass(frozen=True, slots=True)
class DeterministicInsightResult:
    statements: tuple[DeterministicInsightStatement, ...]
    recommendations: tuple[GovernedRecommendation, ...]


class DeterministicInsightBuilder:
    """Creates non-speculative internal statements from computed technical results."""

    def __init__(self, maximum_statements: int = 20, maximum_recommendations: int = 10) -> None:
        if min(maximum_statements, maximum_recommendations) < 1: raise ValueError("configured insight limits must be positive")
        self._maximum_statements = maximum_statements; self._maximum_recommendations = maximum_recommendations

    def build(self, observations: tuple[RawMetricObservation, ...], rule_scores: tuple[RuleQualityScore, ...], hypotheses: tuple[ImpactHypothesis, ...], recommendations: tuple[GovernedRecommendation, ...]) -> DeterministicInsightResult:
        """Create only source-fact-linked technical facts and catalogue actions."""

        score_by_rule = {item.rule_id: item for item in rule_scores}
        statements: list[DeterministicInsightStatement] = []
        for observation in sorted(observations, key=lambda item: item.observation_id):
            score = score_by_rule.get(observation.rule_id)
            status = "not evaluated" if score is None or score.score is None else f"scored {score.score:.2f} on the governed technical DQ scale"
            text = f"The approved {observation.rule_id} primary metric {observation.metric_name} was {observation.outcome}; the corresponding rule was {status}."
            statements.append(DeterministicInsightStatement("statement-" + sha256(observation.source_fact_id.encode()).hexdigest()[:24], text, (observation.source_fact_id,)))
        for hypothesis in sorted(hypotheses, key=lambda item: item.hypothesis_id):
            statements.append(DeterministicInsightStatement("statement-" + sha256(hypothesis.hypothesis_id.encode()).hexdigest()[:24], hypothesis.statement, tuple(hypothesis.internal_fact_ids), "hypothesis"))
        unique: dict[str, DeterministicInsightStatement] = {item.statement_id: item for item in statements}
        return DeterministicInsightResult(tuple(sorted(unique.values(), key=lambda item: item.statement_id)[: self._maximum_statements]), tuple(sorted(recommendations, key=lambda item: item.recommendation_id)[: self._maximum_recommendations]))
