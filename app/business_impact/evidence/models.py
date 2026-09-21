"""Privacy-safe, normalized evidence contracts for Business Impact."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import Field, model_validator

from app.business_impact.models import BusinessImpactContract, JsonValue, _utc_now
from app.execution.models import AggregationKey


class InternalFactKind(str, Enum):
    """Kinds of sanitized facts that may be passed to downstream reasoning."""

    EXECUTION_METRIC = "execution_metric"
    EXECUTION_EVIDENCE_SUMMARY = "execution_evidence_summary"
    EXECUTION_WARNING = "execution_warning"
    UNDERSTANDING_SIGNAL = "understanding_signal"
    UNDERSTANDING_PROFILE = "understanding_profile"
    DERIVED_ASSESSMENT = "derived_assessment"


class EvidenceSensitivity(str, Enum):
    """Classification of retained evidence, deliberately excluding raw data."""

    PUBLIC = "public"
    INTERNAL_AGGREGATE = "internal_aggregate"
    INTERNAL_REDACTED = "internal_redacted"


class InternalFact(BusinessImpactContract):
    """One bounded, sanitized internal observation with stable provenance."""

    fact_id: str = Field(pattern=r"^fact-[a-z0-9][a-z0-9_-]{2,127}$")
    kind: InternalFactKind
    title: str = Field(min_length=1, max_length=240)
    summary: str = Field(min_length=1, max_length=2_000)
    rule_id: str | None = Field(default=None, min_length=1, max_length=128)
    metric_name: str | None = Field(default=None, min_length=1, max_length=160)
    metric_value: JsonValue = None
    unit: str | None = Field(default=None, max_length=64)
    aggregation_key: AggregationKey | None = None
    source_run_id: str = Field(min_length=1, max_length=128)
    source_contract_version: str = Field(min_length=1, max_length=32)
    source_reference_id: str | None = Field(default=None, max_length=160)
    sensitivity: EvidenceSensitivity = EvidenceSensitivity.INTERNAL_AGGREGATE
    redacted: bool = True
    created_at: datetime = Field(default_factory=_utc_now)

    @model_validator(mode="after")
    def validate_safety(self) -> "InternalFact":
        if not self.redacted:
            raise ValueError("internal facts must be redacted or aggregate-only")
        if self.kind == InternalFactKind.EXECUTION_METRIC and not self.metric_name:
            raise ValueError("execution_metric facts require metric_name")
        return self


class EvidenceProvenance(BusinessImpactContract):
    """Traceability for fact-pack construction without source-row disclosure."""

    provenance_id: str = Field(pattern=r"^prov-[a-z0-9][a-z0-9_-]{2,127}$")
    source_layer: str = Field(pattern=r"^(understanding|execution|business_impact)$")
    source_run_id: str = Field(min_length=1, max_length=128)
    source_contract_version: str = Field(min_length=1, max_length=32)
    source_artifact_reference: str | None = Field(default=None, max_length=512)
    transformation: str = Field(min_length=1, max_length=240)
    created_at: datetime = Field(default_factory=_utc_now)


class NormalizedEvidence(BusinessImpactContract):
    """A compact fact with the provenance used to derive it."""

    evidence_id: str = Field(pattern=r"^bie-[a-z0-9][a-z0-9_-]{2,127}$")
    fact: InternalFact
    provenance: EvidenceProvenance
    supporting_fact_ids: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def validate_references(self) -> "NormalizedEvidence":
        if self.fact.fact_id in self.supporting_fact_ids:
            raise ValueError("evidence cannot list its own fact_id as supporting")
        if len(set(self.supporting_fact_ids)) != len(self.supporting_fact_ids):
            raise ValueError("supporting_fact_ids must be unique")
        return self


class EvidenceFactPack(BusinessImpactContract):
    """Bounded, LLM-safe internal pack; never contains text rows or raw PII."""

    pack_id: str = Field(pattern=r"^factpack-[a-z0-9][a-z0-9_-]{2,127}$")
    run_id: str = Field(min_length=1, max_length=128)
    facts: list[InternalFact] = Field(default_factory=list, max_length=500)
    evidence: list[NormalizedEvidence] = Field(default_factory=list, max_length=500)
    truncated: bool = False
    dropped_fact_count: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=_utc_now)

    @model_validator(mode="after")
    def validate_fact_pack(self) -> "EvidenceFactPack":
        ids = [item.fact_id for item in self.facts]
        if len(ids) != len(set(ids)):
            raise ValueError("fact-pack fact_id values must be unique")
        if self.dropped_fact_count and not self.truncated:
            raise ValueError("dropped facts require truncated=true")
        return self
