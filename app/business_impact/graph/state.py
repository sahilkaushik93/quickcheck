"""Typed, bounded and sanitized state for the trusted-insight graph.

The state intentionally contains contracts produced by prior Business Impact
steps rather than source files, rows, transcripts, raw PII, credentials or
HTTP request headers.  It can therefore be safely checkpointed once the graph
builder adds a configured checkpoint implementation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import Field, field_validator, model_validator

from app.business_impact.evidence.models import EvidenceFactPack, NormalizedEvidence
from app.business_impact.external_context.models import (
    Citation,
    ExternalContextBundle,
    PublicResearchTopic,
)
from app.business_impact.impact.models import (
    GovernedRecommendation,
    ImpactHypothesis,
    RootCauseCandidate,
)
from app.business_impact.models import (
    BusinessImpactContract,
    BusinessImpactError,
    BusinessImpactRunRequest,
    BusinessImpactStatus,
    BusinessImpactWarning,
    TrustedInsightClaim,
)
from app.business_impact.scoring.models import (
    AggregateQualityScore,
    AssessmentConfidence,
    NormalizedMetricScore,
    RuleQualityScore,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class GraphNodeStatus(str, Enum):
    """Outcome of an individual deterministic or LLM graph node."""

    NOT_STARTED = "not_started"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    INTERRUPTED = "interrupted"


class GraphNodeTrace(BusinessImpactContract):
    """Bounded, secret-free trace entry for one graph node attempt."""

    node_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,127}$")
    status: GraphNodeStatus
    attempt: int = Field(ge=0, le=10)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_code: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_times(self) -> "GraphNodeTrace":
        if self.completed_at and self.started_at and self.completed_at < self.started_at:
            raise ValueError("graph node completion cannot precede its start")
        if self.status == GraphNodeStatus.FAILED and not self.error_code:
            raise ValueError("failed graph node traces require error_code")
        return self


class BusinessImpactGraphState(BusinessImpactContract):
    """Complete request-scoped state passed only between trusted graph nodes.

    ``run_request`` is kept for handoff validation and is never supplied to a
    connector.  Connector nodes receive only ``research_topics`` and their
    configured source identifiers.  The final LLM node receives an explicitly
    selected fact pack and verified public context, not this state wholesale.
    """

    state_version: str = "1.0"
    run_request: BusinessImpactRunRequest
    status: BusinessImpactStatus = BusinessImpactStatus.NOT_EVALUATED
    current_node: str = Field(default="validate_handoff", pattern=r"^[a-z][a-z0-9_]{2,127}$")
    started_at: datetime = Field(default_factory=_utc_now)
    completed_at: datetime | None = None
    node_traces: list[GraphNodeTrace] = Field(default_factory=list, max_length=40)
    errors: list[BusinessImpactError] = Field(default_factory=list, max_length=100)
    warnings: list[BusinessImpactWarning] = Field(default_factory=list, max_length=200)
    fact_pack: EvidenceFactPack | None = None
    normalized_evidence: list[NormalizedEvidence] = Field(default_factory=list, max_length=200)
    normalized_metric_scores: list[NormalizedMetricScore] = Field(default_factory=list, max_length=200)
    rule_scores: list[RuleQualityScore] = Field(default_factory=list, max_length=100)
    aggregate_score: AggregateQualityScore | None = None
    assessment_confidence: AssessmentConfidence | None = None
    impact_hypotheses: list[ImpactHypothesis] = Field(default_factory=list, max_length=100)
    root_cause_candidates: list[RootCauseCandidate] = Field(default_factory=list, max_length=100)
    recommendations: list[GovernedRecommendation] = Field(default_factory=list, max_length=50)
    research_topics: list[PublicResearchTopic] = Field(default_factory=list, max_length=10)
    external_context: ExternalContextBundle | None = None
    citations: list[Citation] = Field(default_factory=list, max_length=40)
    draft_claims: list[TrustedInsightClaim] = Field(default_factory=list, max_length=20)
    final_claims: list[TrustedInsightClaim] = Field(default_factory=list, max_length=20)
    requires_human_review: bool = False
    human_review_reason_codes: list[str] = Field(default_factory=list, max_length=25)
    retrieval_replay_keys: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("retrieval_replay_keys")
    @classmethod
    def validate_replay_keys(cls, values: list[str]) -> list[str]:
        if any(not value or len(value) > 128 for value in values):
            raise ValueError("replay keys must be bounded non-empty identifiers")
        if len(set(values)) != len(values):
            raise ValueError("replay keys must be unique")
        return values

    @model_validator(mode="after")
    def validate_state(self) -> "BusinessImpactGraphState":
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError("graph completion cannot precede graph start")
        if self.status == BusinessImpactStatus.FAILED and not self.errors:
            raise ValueError("failed graph state requires visible errors")
        if self.status == BusinessImpactStatus.COMPLETED and not self.final_claims:
            raise ValueError("completed graph state requires validated final claims")
        if len({item.citation_id for item in self.citations}) != len(self.citations):
            raise ValueError("graph citations must be unique")
        if self.external_context is not None:
            context_ids = {item.citation_id for item in self.external_context.citations}
            if not {item.citation_id for item in self.citations}.issubset(context_ids):
                raise ValueError("state citations must originate from verified external context")
        return self


__all__ = ["BusinessImpactGraphState", "GraphNodeStatus", "GraphNodeTrace"]
