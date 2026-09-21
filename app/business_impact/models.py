"""Canonical contracts for Evidence, Scoring and Trusted Business Insights.

This is a downstream-only, privacy-safe contract boundary.  It consumes
compact Understanding and Execution outputs; it never carries transcripts,
source rows, raw PII, credentials, internal system names or outbound queries.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import ConfigDict, Field, model_validator

from app.execution.models import ExecutionOutput
from app.understanding.models import ContractModel, JsonValue, UnderstandingOutput


def _utc_now() -> datetime:
    """Return a timezone-aware current UTC value."""

    return datetime.now(timezone.utc)


class BusinessImpactContract(ContractModel):
    """Strict base model for this layer's serializable contracts."""

    model_config = ConfigDict(
        extra="forbid", validate_assignment=True, use_enum_values=True, str_strip_whitespace=True
    )


class BusinessImpactStatus(str, Enum):
    """Completion state of one Business Impact assessment."""

    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    PARTIAL = "partial"
    FAILED = "failed"
    INSUFFICIENT_CONTEXT = "insufficient_context"
    NOT_EVALUATED = "not_evaluated"


class InsightClaimType(str, Enum):
    FACT = "fact"
    INFERENCE = "inference"
    HYPOTHESIS = "hypothesis"
    RECOMMENDATION = "recommendation"


class LLMSelection(BusinessImpactContract):
    """Required per-run LLM selection for a complete trusted report."""

    provider: str = Field(min_length=1, max_length=64)
    model: str | None = Field(default=None, max_length=256)
    request_id: str = Field(default="1", min_length=1, max_length=128)


class BusinessImpactError(BusinessImpactContract):
    """Safe error without provider credentials, prompts or source records."""

    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=1_000)
    retryable: bool = False
    component: str = Field(min_length=1, max_length=128)


class BusinessImpactWarning(BusinessImpactContract):
    """Non-fatal diagnostic attached to a partial or completed assessment."""

    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=1_000)
    component: str = Field(min_length=1, max_length=128)


class BusinessImpactRunRequest(BusinessImpactContract):
    """Internal coordinator handoff from canonical Understanding and Execution."""

    run_id: str = Field(min_length=1, max_length=128)
    understanding: UnderstandingOutput
    execution: ExecutionOutput
    llm: LLMSelection
    business_context: "BusinessContext | None" = None
    persist_artifacts: bool = False

    @model_validator(mode="after")
    def validate_handoff(self) -> "BusinessImpactRunRequest":
        if self.understanding.run_id != self.execution.understanding_run_id:
            raise ValueError("execution output does not belong to supplied Understanding run")
        return self


class TrustedInsightClaim(BusinessImpactContract):
    """Claim from the mandatory LLM with auditable fact and citation references."""

    claim_id: str = Field(pattern=r"^claim-[a-z0-9][a-z0-9_-]{2,127}$")
    claim_type: InsightClaimType
    statement: str = Field(min_length=1, max_length=3_000)
    internal_fact_ids: list[str] = Field(default_factory=list, max_length=50)
    external_citation_ids: list[str] = Field(default_factory=list, max_length=25)
    correlation_not_causation: bool = True
    requires_human_review: bool = False

    @model_validator(mode="after")
    def validate_claim_references(self) -> "TrustedInsightClaim":
        if not self.internal_fact_ids and not self.external_citation_ids:
            raise ValueError("a trusted claim requires fact or citation support")
        if self.claim_type == InsightClaimType.FACT and not self.external_citation_ids:
            raise ValueError("external factual claims require verified citations")
        if len(set(self.internal_fact_ids)) != len(self.internal_fact_ids):
            raise ValueError("internal_fact_ids must be unique")
        if len(set(self.external_citation_ids)) != len(self.external_citation_ids):
            raise ValueError("external_citation_ids must be unique")
        return self


# Imported after base declarations to avoid circular imports in subpackages.
from app.business_impact.evidence.models import EvidenceFactPack, NormalizedEvidence  # noqa: E402
from app.business_impact.external_context.models import Citation, ExternalContextBundle  # noqa: E402
from app.business_impact.impact.models import (  # noqa: E402
    BusinessContext,
    GovernedRecommendation,
    ImpactHypothesis,
    RootCauseCandidate,
)
from app.business_impact.scoring.models import (  # noqa: E402
    AggregateQualityScore,
    AssessmentConfidence,
    NormalizedMetricScore,
    RuleQualityScore,
)


class BusinessImpactOutput(BusinessImpactContract):
    """Complete downstream result, ready for future API serialization."""

    contract_version: str = "1.0"
    run_id: str = Field(min_length=1, max_length=128)
    understanding_run_id: str = Field(min_length=1, max_length=128)
    execution_run_id: str = Field(min_length=1, max_length=128)
    status: BusinessImpactStatus
    created_at: datetime = Field(default_factory=_utc_now)
    fact_pack: EvidenceFactPack
    normalized_evidence: list[NormalizedEvidence] = Field(default_factory=list, max_length=500)
    normalized_metric_scores: list[NormalizedMetricScore] = Field(default_factory=list, max_length=1_000)
    rule_scores: list[RuleQualityScore] = Field(default_factory=list, max_length=100)
    aggregate_score: AggregateQualityScore | None = None
    assessment_confidence: AssessmentConfidence | None = None
    impact_hypotheses: list[ImpactHypothesis] = Field(default_factory=list, max_length=100)
    root_cause_candidates: list[RootCauseCandidate] = Field(default_factory=list, max_length=100)
    external_context: ExternalContextBundle | None = None
    citations: list[Citation] = Field(default_factory=list, max_length=100)
    insight_claims: list[TrustedInsightClaim] = Field(default_factory=list, max_length=100)
    recommendations: list[GovernedRecommendation] = Field(default_factory=list, max_length=50)
    llm_provider: str | None = Field(default=None, max_length=64)
    llm_model: str | None = Field(default=None, max_length=256)
    warnings: list[BusinessImpactWarning] = Field(default_factory=list, max_length=200)
    errors: list[BusinessImpactError] = Field(default_factory=list, max_length=100)
    artifact_references: dict[str, str] = Field(default_factory=dict, max_length=50)

    @model_validator(mode="after")
    def validate_output(self) -> "BusinessImpactOutput":
        complete_statuses = {
            BusinessImpactStatus.COMPLETED,
            BusinessImpactStatus.COMPLETED_WITH_WARNINGS,
        }
        if self.status in complete_statuses and not self.insight_claims:
            raise ValueError("complete Business Impact output requires trusted LLM claims")
        if self.status in complete_statuses and not self.llm_provider:
            raise ValueError("complete Business Impact output requires LLM provenance")
        if self.status == BusinessImpactStatus.FAILED and not self.errors:
            raise ValueError("failed Business Impact output requires visible errors")
        if self.status in complete_statuses and self.errors:
            raise ValueError("completed Business Impact outputs cannot contain errors")
        known_facts = {item.fact_id for item in self.fact_pack.facts}
        known_citations = {item.citation_id for item in self.citations}
        if self.external_context is not None:
            known_citations.update(item.citation_id for item in self.external_context.citations)
        for claim in self.insight_claims:
            unknown_facts = set(claim.internal_fact_ids) - known_facts
            unknown_citations = set(claim.external_citation_ids) - known_citations
            if unknown_facts or unknown_citations:
                raise ValueError("trusted claims reference unknown internal facts or citations")
        return self


BusinessImpactRunRequest.model_rebuild()
BusinessImpactOutput.model_rebuild()
