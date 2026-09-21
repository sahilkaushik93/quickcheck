"""Compute deterministic assessment confidence from documented completeness factors."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Mapping

from app.business_impact.evidence.models import NormalizedEvidence
from app.business_impact.scoring.models import AssessmentConfidence, AssessmentConfidenceFactor, NormalizedMetricScore, RuleQualityScore, ScoreAvailability
from app.execution.models import ExecutionOutput, RuleExecutionStatus
from app.understanding.models import UnderstandingOutput


class ConfidencePolicyError(ValueError):
    """Safe error for malformed deterministic confidence policy."""


@dataclass(frozen=True, slots=True)
class ConfidenceConfig:
    policy_version: str
    formula_id: str
    minimum_factor_weight_coverage: float
    round_decimal_places: int
    factor_weights: Mapping[str, float]
    evidence_minimum_collections: int
    evidence_truncation_penalty: float
    minimum_verified_citations: int

    @classmethod
    def from_file(cls, path: str | Path) -> "ConfidenceConfig":
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8")); formula = data["formula"]; factors = data["factors"]
            config = cls(str(data["policy_version"]), str(formula["formula_id"]), float(formula["minimum_factor_weight_coverage"]), int(formula["round_decimal_places"]), {str(name): float(item["weight"]) for name, item in factors.items()}, int(factors["evidence_coverage"]["parameters"]["minimum_supporting_evidence_collections"]), float(factors["evidence_coverage"]["parameters"]["truncation_penalty"]), int(factors["external_context_coverage"]["parameters"]["minimum_verified_citations_for_complete_report"]))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ConfidencePolicyError("Invalid confidence policy.") from exc
        if abs(sum(config.factor_weights.values()) - 1.0) > 0.000001 or not 0 <= config.minimum_factor_weight_coverage <= 1 or config.round_decimal_places < 0:
            raise ConfidencePolicyError("Confidence policy has invalid factor weights or limits.")
        return config


class AssessmentConfidenceCalculator:
    """Calculates completeness/provenance confidence, never LLM confidence."""

    def __init__(self, config: ConfidenceConfig, rule_weights: Mapping[str, float]) -> None:
        self._config = config
        self._rule_weights = dict(rule_weights)

    def calculate(self, *, understanding: UnderstandingOutput, execution: ExecutionOutput, normalized_scores: Iterable[NormalizedMetricScore], rule_scores: Iterable[RuleQualityScore], normalized_evidence: Iterable[NormalizedEvidence], assessment_seed: str, external_context_required: bool | None = None, verified_citation_count: int = 0) -> AssessmentConfidence:
        """Return a bounded confidence with unavailable factors excluded per policy."""

        metrics = tuple(normalized_scores); rules = tuple(rule_scores); evidence = tuple(normalized_evidence)
        selected = {item.rule_id for item in rules if item.rule_id in self._rule_weights}
        factors: list[AssessmentConfidenceFactor] = []
        rule_factor = self._rule_coverage(execution, selected); metric_factor = self._metric_coverage(metrics, selected)
        factors.extend(item for item in (rule_factor, metric_factor) if item is not None)
        evidence_factor = self._evidence_coverage(evidence, execution, selected)
        if evidence_factor is not None: factors.append(evidence_factor)
        provenance = self._provenance(understanding, execution)
        factors.append(provenance)
        external = self._external_context(external_context_required, verified_citation_count)
        if external is not None: factors.append(external)
        contributing_weight = sum(item.weight for item in factors)
        if contributing_weight < self._config.minimum_factor_weight_coverage:
            return AssessmentConfidence(confidence_id="confidence-" + sha256(assessment_seed.encode()).hexdigest()[:24], value=None, availability=ScoreAvailability.NOT_EVALUATED, factors=factors, formula_id=self._config.formula_id, policy_version=self._config.policy_version, rationale="No confidence value: available deterministic factor weight is below the configured minimum.")
        value = round(sum(item.value * item.weight for item in factors) / contributing_weight, self._config.round_decimal_places)
        return AssessmentConfidence(confidence_id="confidence-" + sha256(assessment_seed.encode()).hexdigest()[:24], value=value, availability=ScoreAvailability.AVAILABLE, factors=factors, formula_id=self._config.formula_id, policy_version=self._config.policy_version, rationale="Confidence reflects deterministic assessment coverage and provenance only; it is not LLM confidence or statistical certainty.")

    def _rule_coverage(self, execution: ExecutionOutput, selected: set[str]) -> AssessmentConfidenceFactor | None:
        if not selected: return None
        complete = {item.rule_id for item in execution.rule_results if item.status in {RuleExecutionStatus.COMPLETED, RuleExecutionStatus.COMPLETED_WITH_WARNINGS}}
        total = sum(self._rule_weights[item] for item in selected); value = sum(self._rule_weights[item] for item in selected & complete) / total
        return self._factor("rule_coverage", value, "Configured-weight coverage of completed selected rules.")

    def _metric_coverage(self, metrics: tuple[NormalizedMetricScore, ...], selected: set[str]) -> AssessmentConfidenceFactor | None:
        if not selected: return None
        total = sum(self._rule_weights[item] for item in selected); available = {item.rule_id for item in metrics if item.rule_id in selected and item.availability == ScoreAvailability.AVAILABLE and item.score is not None}
        return self._factor("metric_coverage", sum(self._rule_weights[item] for item in available) / total, "Configured-weight coverage of available approved primary metrics.")

    def _evidence_coverage(self, evidence: tuple[NormalizedEvidence, ...], execution: ExecutionOutput, selected: set[str]) -> AssessmentConfidenceFactor | None:
        if not selected: return None
        retained = len(evidence); value = min(1.0, retained / self._config.evidence_minimum_collections)
        if execution.summary.evidence_observed > execution.summary.evidence_retained or any(item.fact.metric_value.get("source_truncated", False) for item in evidence if isinstance(item.fact.metric_value, dict)):
            value = max(0.0, value - self._config.evidence_truncation_penalty)
        return self._factor("evidence_coverage", value, "Bounded normalized evidence completeness, with policy truncation penalty where applicable.")

    def _provenance(self, understanding: UnderstandingOutput, execution: ExecutionOutput) -> AssessmentConfidenceFactor:
        source_match = bool(understanding.source.fingerprint and understanding.source.fingerprint == execution.source.fingerprint)
        complete = source_match and understanding.run_id == execution.understanding_run_id and bool(execution.registry_fingerprint) and bool(understanding.contract_version) and bool(execution.contract_version)
        return self._factor("source_provenance", 1.0 if complete else 0.0, "Understanding/Execution source, linkage, registry and contract provenance validation.")

    def _external_context(self, required: bool | None, citations: int) -> AssessmentConfidenceFactor | None:
        if required is None: return None
        value = min(1.0, citations / self._config.minimum_verified_citations) if required else 1.0
        return self._factor("external_context_coverage", value, "Verified citation coverage supplied by the trusted external-context pipeline.")

    def _factor(self, factor_id: str, value: float, rationale: str) -> AssessmentConfidenceFactor:
        return AssessmentConfidenceFactor(factor_id=factor_id, name=factor_id.replace("_", " ").title(), value=max(0.0, min(1.0, value)), weight=self._config.factor_weights[factor_id], method="Configured deterministic completeness calculation.", rationale=rationale)
