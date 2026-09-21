"""Map evaluated technical observations to approved non-causal impact hypotheses."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

from app.business_impact.impact.context_validator import BusinessContextValidationResult
from app.business_impact.impact.models import GovernedRecommendation, ImpactHypothesis, ReviewDisposition
from app.business_impact.scoring.models import RawMetricObservation
from app.execution.models import MetricOutcome


@dataclass(frozen=True, slots=True)
class ImpactMappingResult:
    """Mapping output is deliberately technical and non-financial."""

    hypotheses: tuple[ImpactHypothesis, ...]
    recommendations: tuple[GovernedRecommendation, ...]
    insufficient_context: bool
    warnings: tuple[str, ...]


class ImpactMapper:
    """Applies only the versioned approved mapping and recommendation catalogues."""

    def __init__(self, mapping: Mapping[str, Any], recommendations: Mapping[str, Any]) -> None:
        self._conditions = tuple(mapping.get("conditions", ()))
        self._recommendations = {str(item["condition_id"]): item for item in recommendations.get("recommendations", ()) if isinstance(item, Mapping) and "condition_id" in item}
        self._mapping_version = str(mapping.get("mapping_version", "unknown"))
        if not self._conditions:
            raise ValueError("BUSINESS_IMPACT_MAPPING_INVALID")

    @classmethod
    def from_files(cls, mapping_path: str | Path, recommendations_path: str | Path) -> "ImpactMapper":
        try:
            mapping = json.loads(Path(mapping_path).read_text(encoding="utf-8")); recommendations = json.loads(Path(recommendations_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("IMPACT_MAPPING_POLICY_LOAD_FAILED") from exc
        if not isinstance(mapping, Mapping) or not isinstance(recommendations, Mapping):
            raise ValueError("IMPACT_MAPPING_POLICY_INVALID")
        return cls(mapping, recommendations)

    def map(self, observations: tuple[RawMetricObservation, ...], context: BusinessContextValidationResult) -> ImpactMappingResult:
        """Create only approved hypotheses; never infer financial/regulatory outcomes."""

        if not context.sufficient:
            return ImpactMappingResult((), (), True, ("Contextual business-impact mapping was not performed because governed business context is insufficient.",))
        hypotheses: list[ImpactHypothesis] = []; recommendations: list[GovernedRecommendation] = []
        for observation in sorted(observations, key=lambda item: item.observation_id):
            for condition in self._conditions:
                if not self._matches(condition, observation):
                    continue
                condition_id = str(condition["condition_id"])
                for dimension in sorted(condition.get("impact_dimensions", ())):
                    seed = f"{condition_id}|{dimension}|{observation.source_fact_id}"
                    review = ReviewDisposition.REQUIRED if bool(condition.get("requires_human_review")) else ReviewDisposition.NOT_REQUIRED
                    hypotheses.append(ImpactHypothesis(hypothesis_id="impact-" + _digest(seed), category=str(dimension), statement=str(condition["hypothesis_template"]), internal_fact_ids=[observation.source_fact_id], deterministic_classification=str(condition.get("deterministic_classification") or ""), correlation_not_causation=True, review_disposition=review))
                catalogue = self._recommendations.get(condition_id)
                if catalogue:
                    review = ReviewDisposition.REQUIRED if bool(catalogue.get("requires_human_review")) else ReviewDisposition.NOT_REQUIRED
                    recommendations.append(GovernedRecommendation(recommendation_id="rec-" + _digest(condition_id + "|" + observation.source_fact_id), catalogue_id=str(catalogue["catalogue_id"]), title=str(catalogue["title"]), action=str(catalogue["action"]), priority=str(catalogue["priority"]), supporting_fact_ids=[observation.source_fact_id], generated_by_llm=False, review_disposition=review))
        return ImpactMappingResult(tuple(hypotheses), tuple(recommendations), False, ())

    @staticmethod
    def _matches(condition: Mapping[str, Any], observation: RawMetricObservation) -> bool:
        when = condition.get("when", {})
        if not isinstance(when, Mapping): return False
        return str(condition.get("canonical_rule_id")) == observation.rule_id and str(when.get("primary_metric", "")).casefold() == observation.metric_name.casefold() and observation.outcome in set(when.get("outcomes", ())) and observation.outcome != MetricOutcome.NOT_EVALUATED


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()[:24]
