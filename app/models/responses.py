"""Public API contracts for the layered UnDQ transcript service."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.execution.models import AggregationRequest, ExecutionOptions, ExecutionOutput
from app.business_impact.impact.models import BusinessContext
from app.business_impact.models import BusinessImpactOutput
from app.understanding.models import (
    DQSignal,
    DomainAssignment,
    ExecutionPlan,
    LLMInsight,
    MetadataKnowledgeBase,
    RelationshipCandidate,
    RuleApplicability,
    UnderstandingInputProvenance,
)


class PublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ServiceResult(PublicModel):
    """Backward-compatible component response used by existing consumers."""

    status: str = "working"
    message: str
    input_file: str
    details: dict[str, Any] = Field(default_factory=dict)


class UnderstandingResult(PublicModel):
    """Extended Understanding response preserving legacy Profile and Rules."""

    Profile: ServiceResult
    Rules: ServiceResult
    run_id: str | None = None
    status: str = "completed"
    input_provenance: UnderstandingInputProvenance | None = None
    metadata_knowledge_base: MetadataKnowledgeBase | None = None
    domains: list[DomainAssignment] = Field(default_factory=list)
    relationships: list[RelationshipCandidate] = Field(default_factory=list)
    dq_signals: list[DQSignal] = Field(default_factory=list)
    rule_applicability: list[RuleApplicability] = Field(default_factory=list)
    execution_plan: ExecutionPlan | None = None
    llm_insights: list[LLMInsight] = Field(default_factory=list)
    artifact_references: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    # Optional during migration.  Business Impact can only run when the
    # canonical, provenance-complete result is retained by Understanding.
    output: "UnderstandingOutput | None" = None


class UnderstandingEnvelope(PublicModel):
    understanding: UnderstandingResult


class LLMOptions(PublicModel):
    """Per-request provider selection; Understanding enrichment remains opt-in."""

    enabled: bool = False
    provider: str = "launchpad"
    model: str | None = None
    request_id: str = "1"
    failure_mode: Literal["skip", "raise"] = "skip"


class ExecutionResult(PublicModel):
    """Execution response with compatibility and canonical metric views."""

    Observed_Results: ServiceResult
    output: ExecutionOutput


class ExecutionRequest(PublicModel):
    """Typed non-multipart representation retained for SDK consumers."""

    understanding: UnderstandingResult
    aggregation: AggregationRequest = Field(default_factory=AggregationRequest)
    options: ExecutionOptions = Field(default_factory=ExecutionOptions)
    column_overrides: dict[str, str] = Field(default_factory=dict)


class ExecutionEnvelope(PublicModel):
    understanding: UnderstandingResult
    execution: ExecutionResult


class BusinessImpactOptions(PublicModel):
    """Caller-controlled persistence and public-context requirements."""

    persist_artifacts: bool = False
    external_context_required: bool = True


class BusinessImpactLLMOptions(PublicModel):
    """A provider is mandatory for a report that reaches completed status."""

    provider: str = Field(min_length=1, max_length=64)
    model: str | None = Field(default=None, max_length=256)
    request_id: str = Field(min_length=1, max_length=128)
    failure_mode: Literal["partial"] = "partial"


class BusinessImpactRequest(PublicModel):
    """Canonical downstream-only handoff; no CSV or source reference is accepted."""

    understanding: "UnderstandingOutput"
    execution: ExecutionOutput
    business_context: BusinessContext | None = None
    options: BusinessImpactOptions = Field(default_factory=BusinessImpactOptions)
    llm: BusinessImpactLLMOptions


class BusinessImpactEnvelope(PublicModel):
    request_id: str
    status: str
    business_impact: BusinessImpactOutput


class EngineResponse(PublicModel):
    """Understanding → Execution, with an opt-in isolated Business Impact branch."""

    request_id: str
    status: str
    understanding: UnderstandingResult
    execution: ExecutionResult
    business_impact: BusinessImpactOutput | None = None
    business_impact_error: ServiceResult | None = None


class HealthResponse(PublicModel):
    status: str
    service: str
    version: str
    checks: dict[str, Any] = Field(default_factory=dict)


class ComponentStatusResponse(PublicModel):
    status: str = "healthy"
    component: str
    message: str
    processing_method: str
    documentation: str = "/docs"


from app.understanding.models import UnderstandingOutput  # noqa: E402

BusinessImpactRequest.model_rebuild()
