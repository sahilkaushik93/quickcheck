"""Governed business-context and impact-hypothesis contracts."""

from __future__ import annotations

from enum import Enum

from pydantic import Field, field_validator, model_validator

from app.business_impact.models import BusinessImpactContract


class ImpactStatementType(str, Enum):
    FACT = "fact"
    INFERENCE = "inference"
    HYPOTHESIS = "hypothesis"
    RECOMMENDATION = "recommendation"


class ReviewDisposition(str, Enum):
    NOT_REQUIRED = "not_required"
    REQUIRED = "required"
    APPROVED = "approved"
    REJECTED = "rejected"


class BusinessContext(BusinessImpactContract):
    """Validated governed context, never a vehicle for sensitive source data."""

    context_id: str = Field(pattern=r"^context-[a-z0-9][a-z0-9_-]{2,127}$")
    context_schema_version: str = Field(min_length=1, max_length=64)
    business_capabilities: list[str] = Field(default_factory=list, max_length=50)
    service_channels: list[str] = Field(default_factory=list, max_length=25)
    regulated_domains: list[str] = Field(default_factory=list, max_length=25)
    approved_impact_categories: list[str] = Field(default_factory=list, max_length=50)
    supplied_by: str = Field(default="request", max_length=128)

    @field_validator(
        "business_capabilities", "service_channels", "regulated_domains", "approved_impact_categories"
    )
    @classmethod
    def unique_values(cls, values: list[str]) -> list[str]:
        if len({value.casefold() for value in values}) != len(values):
            raise ValueError("business-context values must be unique")
        return values


class ImpactHypothesis(BusinessImpactContract):
    """A non-causal, governed statement connecting DQ conditions to a capability."""

    hypothesis_id: str = Field(pattern=r"^impact-[a-z0-9][a-z0-9_-]{2,127}$")
    category: str = Field(min_length=1, max_length=128)
    statement_type: ImpactStatementType = ImpactStatementType.HYPOTHESIS
    statement: str = Field(min_length=1, max_length=2_000)
    internal_fact_ids: list[str] = Field(min_length=1, max_length=50)
    external_citation_ids: list[str] = Field(default_factory=list, max_length=25)
    deterministic_classification: str | None = Field(default=None, max_length=128)
    correlation_not_causation: bool = True
    review_disposition: ReviewDisposition = ReviewDisposition.NOT_REQUIRED

    @model_validator(mode="after")
    def validate_hypothesis(self) -> "ImpactHypothesis":
        if len(set(self.internal_fact_ids)) != len(self.internal_fact_ids):
            raise ValueError("internal_fact_ids must be unique")
        if len(set(self.external_citation_ids)) != len(self.external_citation_ids):
            raise ValueError("external_citation_ids must be unique")
        if self.statement_type == ImpactStatementType.FACT and not self.external_citation_ids:
            raise ValueError("external business facts require verified citation IDs")
        return self


class RootCauseCandidate(BusinessImpactContract):
    """Technical root-cause candidate; not a definitive causal conclusion."""

    candidate_id: str = Field(pattern=r"^root-cause-[a-z0-9][a-z0-9_-]{2,127}$")
    category: str = Field(min_length=1, max_length=128)
    statement: str = Field(min_length=1, max_length=1_500)
    supporting_fact_ids: list[str] = Field(min_length=1, max_length=50)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    requires_review: bool = True
    causal_claim: bool = False

    @model_validator(mode="after")
    def validate_root_cause(self) -> "RootCauseCandidate":
        if self.causal_claim:
            raise ValueError("root-cause candidates must not assert causality")
        return self


class GovernedRecommendation(BusinessImpactContract):
    """A catalogue-backed action, optionally supplemented by an LLM suggestion."""

    recommendation_id: str = Field(pattern=r"^rec-[a-z0-9][a-z0-9_-]{2,127}$")
    catalogue_id: str | None = Field(default=None, max_length=128)
    title: str = Field(min_length=1, max_length=240)
    action: str = Field(min_length=1, max_length=2_000)
    priority: str = Field(min_length=1, max_length=64)
    supporting_fact_ids: list[str] = Field(min_length=1, max_length=50)
    generated_by_llm: bool = False
    review_disposition: ReviewDisposition = ReviewDisposition.NOT_REQUIRED

    @model_validator(mode="after")
    def validate_governance(self) -> "GovernedRecommendation":
        if self.generated_by_llm and self.catalogue_id is None and self.review_disposition != ReviewDisposition.REQUIRED:
            raise ValueError("non-catalogue LLM recommendations require human review")
        return self
