"""Typed deterministic scoring contracts, separate from business impact."""

from __future__ import annotations

from enum import Enum

from pydantic import Field, model_validator

from app.business_impact.models import BusinessImpactContract, JsonValue
from app.execution.models import AggregationKey, MetricOutcome


class ScoreAvailability(str, Enum):
    AVAILABLE = "available"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    NOT_EVALUATED = "not_evaluated"


class ScoreBand(str, Enum):
    EXCELLENT = "excellent"
    ACCEPTABLE = "acceptable"
    WATCH = "watch"
    POOR = "poor"
    CRITICAL = "critical"
    NOT_EVALUATED = "not_evaluated"


class RawMetricObservation(BusinessImpactContract):
    """Verbatim-safe representation of a metric from Execution."""

    observation_id: str = Field(pattern=r"^metric-[a-z0-9][a-z0-9_-]{2,127}$")
    rule_id: str = Field(min_length=1, max_length=128)
    rule_version: str = Field(min_length=1, max_length=64)
    metric_name: str = Field(min_length=1, max_length=160)
    value: JsonValue
    unit: str | None = Field(default=None, max_length=64)
    numerator: int | float | None = None
    denominator: int | float | None = Field(default=None, ge=0)
    threshold: JsonValue = None
    outcome: MetricOutcome = MetricOutcome.NOT_EVALUATED
    aggregation_key: AggregationKey | None = None
    period: str | None = Field(default=None, max_length=64)
    dimensions: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    availability: ScoreAvailability = ScoreAvailability.AVAILABLE
    source_fact_id: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_ratio(self) -> "RawMetricObservation":
        if self.numerator is not None and self.numerator < 0:
            raise ValueError("numerator cannot be negative")
        if self.denominator is not None and self.numerator is not None and self.numerator > self.denominator:
            raise ValueError("numerator cannot exceed denominator")
        return self


class NormalizedMetricScore(BusinessImpactContract):
    """A policy-derived 0-100 technical quality score for one observation."""

    score_id: str = Field(pattern=r"^score-[a-z0-9][a-z0-9_-]{2,127}$")
    observation_id: str = Field(min_length=1, max_length=128)
    rule_id: str = Field(min_length=1, max_length=128)
    metric_name: str = Field(min_length=1, max_length=160)
    score: float | None = Field(default=None, ge=0.0, le=100.0)
    band: ScoreBand = ScoreBand.NOT_EVALUATED
    availability: ScoreAvailability
    formula_id: str = Field(min_length=1, max_length=128)
    policy_version: str = Field(min_length=1, max_length=64)
    weight: float | None = Field(default=None, ge=0.0, le=1.0)
    rationale: str = Field(min_length=1, max_length=1_000)
    aggregation_key: AggregationKey | None = None

    @model_validator(mode="after")
    def validate_score_presence(self) -> "NormalizedMetricScore":
        if self.availability == ScoreAvailability.AVAILABLE and self.score is None:
            raise ValueError("available normalized metrics require a score")
        if self.availability != ScoreAvailability.AVAILABLE and self.score is not None:
            raise ValueError("unavailable/partial metrics must not claim a final score")
        return self


class RuleQualityScore(BusinessImpactContract):
    """Policy-weighted technical score for one canonical DQ rule."""

    rule_score_id: str = Field(pattern=r"^rule-score-[a-z0-9][a-z0-9_-]{2,127}$")
    rule_id: str = Field(min_length=1, max_length=128)
    rule_version: str = Field(min_length=1, max_length=64)
    score: float | None = Field(default=None, ge=0.0, le=100.0)
    band: ScoreBand = ScoreBand.NOT_EVALUATED
    availability: ScoreAvailability
    metric_score_ids: list[str] = Field(default_factory=list, max_length=100)
    effective_weight: float | None = Field(default=None, ge=0.0, le=1.0)
    formula_id: str = Field(min_length=1, max_length=128)
    policy_version: str = Field(min_length=1, max_length=64)
    records_evaluated: int = Field(default=0, ge=0)
    records_affected: int = Field(default=0, ge=0)
    rationale: str = Field(min_length=1, max_length=1_000)


class AggregateQualityScore(BusinessImpactContract):
    """Overall DQ score, never a business-loss or risk score."""

    assessment_id: str = Field(pattern=r"^dq-assessment-[a-z0-9][a-z0-9_-]{2,127}$")
    score: float | None = Field(default=None, ge=0.0, le=100.0)
    band: ScoreBand = ScoreBand.NOT_EVALUATED
    availability: ScoreAvailability
    contributing_rule_score_ids: list[str] = Field(default_factory=list, max_length=100)
    excluded_rule_ids: list[str] = Field(default_factory=list, max_length=100)
    coverage_ratio: float = Field(ge=0.0, le=1.0)
    minimum_coverage_ratio: float = Field(ge=0.0, le=1.0)
    formula_id: str = Field(min_length=1, max_length=128)
    policy_version: str = Field(min_length=1, max_length=64)
    rationale: str = Field(min_length=1, max_length=1_500)

    @model_validator(mode="after")
    def validate_aggregate(self) -> "AggregateQualityScore":
        if self.coverage_ratio < self.minimum_coverage_ratio and self.score is not None:
            raise ValueError("aggregate score must be null below minimum coverage")
        if self.score is None and self.band != ScoreBand.NOT_EVALUATED:
            raise ValueError("null aggregate score requires not_evaluated band")
        return self


class AssessmentConfidenceFactor(BusinessImpactContract):
    """A documented, deterministic contributor to assessment confidence."""

    factor_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=160)
    value: float = Field(ge=0.0, le=1.0)
    weight: float = Field(ge=0.0, le=1.0)
    method: str = Field(min_length=1, max_length=240)
    rationale: str = Field(min_length=1, max_length=1_000)


class AssessmentConfidence(BusinessImpactContract):
    """Deterministic assessment confidence; explicitly not LLM confidence."""

    confidence_id: str = Field(pattern=r"^confidence-[a-z0-9][a-z0-9_-]{2,127}$")
    value: float | None = Field(default=None, ge=0.0, le=1.0)
    availability: ScoreAvailability
    factors: list[AssessmentConfidenceFactor] = Field(default_factory=list, max_length=25)
    formula_id: str = Field(min_length=1, max_length=128)
    policy_version: str = Field(min_length=1, max_length=64)
    rationale: str = Field(min_length=1, max_length=1_000)
