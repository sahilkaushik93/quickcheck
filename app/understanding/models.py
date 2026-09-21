"""Canonical data contracts for the transcript Understanding Layer.

The models in this module are deliberately implementation-agnostic.  They are
shared by source adapters, bounded-memory profilers, context discovery,
quality-signal detection, rule planning, optional LLM enrichment, and artifact
storage.  Source rows and transcript text must never be placed in these models.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


JsonScalar = str | int | float | bool | None
# Pydantic 2.10/2.13 compatibility: nested JSON values are required for
# per-column and per-category metrics.  ``Any`` is restricted to the children
# of JSON containers; serializers still reject non-JSON runtime objects.
JsonValue = JsonScalar | list[Any] | dict[str, Any]


def _utc_now() -> datetime:
    """Return an aware UTC timestamp for model default factories."""

    return datetime.now(timezone.utc)


class ContractModel(BaseModel):
    """Strict base class used by all Understanding Layer contracts."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        use_enum_values=True,
        str_strip_whitespace=True,
    )


class SourceType(str, Enum):
    """Source technologies supported by the source-adapter abstraction."""

    CSV = "csv"
    BIGQUERY = "bigquery"
    DATABASE = "database"
    DATAFRAME = "dataframe"


class LogicalDataType(str, Enum):
    """Normalized logical types inferred without changing source data."""

    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"
    DURATION = "duration"
    JSON = "json"
    ARRAY = "array"
    UNKNOWN = "unknown"


class ConfidenceLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ProvenanceType(str, Enum):
    DETERMINISTIC = "deterministic"
    METADATA = "metadata"
    CONFIGURATION = "configuration"
    STATISTICAL = "statistical"
    LLM_SUGGESTION = "llm_suggestion"
    USER_APPROVED = "user_approved"


class RelationshipType(str, Enum):
    IDENTIFIER = "identifier_candidate"
    CATEGORICAL_DIMENSION = "categorical_dimension"
    NUMERIC_CORRELATION = "numeric_correlation"
    TEMPORAL_ORDER = "temporal_order"
    FUNCTIONAL_DEPENDENCY = "functional_dependency_candidate"
    TRANSCRIPT_DURATION = "transcript_duration"
    SPEAKER_STRUCTURE = "speaker_structure"


class SignalStatus(str, Enum):
    OBSERVED = "observed"
    SUSPECTED = "suspected"
    RESOLVED = "resolved"
    SUPPRESSED = "suppressed"


class RuleApplicabilityStatus(str, Enum):
    APPLICABLE = "applicable"
    NOT_APPLICABLE = "not_applicable"
    NEEDS_REVIEW = "needs_review"
    DISABLED = "disabled"


class ExecutionReadiness(str, Enum):
    """Whether a selected rule can be executed by the current runtime.

    Applicability answers whether a rule is relevant to the dataset.  Readiness
    is deliberately separate: an approved rule can be applicable even while an
    execution handler is still being implemented or configured.
    """

    READY = "ready"
    IMPLEMENTATION_PENDING = "implementation_pending"
    NOT_REQUIRED = "not_required"


class PlanStatus(str, Enum):
    READY = "ready"
    PARTIAL = "partial"
    BLOCKED = "blocked"


class RequiredCheck(str, Enum):
    """Stable identifiers for checks required by the transcript MVP."""

    FILL_RATE = "fill_rate"
    TOPIC_DISTRIBUTION = "topic_distribution"
    AGENT_SPEAKER_TAG_VALIDATION = "agent_speaker_tag_validation"
    NON_ENGLISH_SPELLING = "non_english_spelling"
    MISTRANSLATED_RATE = "mistranslated_rate"
    PII_DETECTION = "pii_detection"
    SPEECH_PER_DURATION_RATE = "speech_per_duration_rate"


class ProcessingLimits(ContractModel):
    """Effective bounded-memory safeguards recorded with every run."""

    chunk_size: int = Field(ge=1)
    max_distinct_values: int = Field(ge=1)
    max_top_values: int = Field(ge=1)
    max_samples_per_column: int = Field(ge=0)
    max_evidence_items: int = Field(ge=0)
    max_relationship_pairs: int = Field(ge=0)


class SourceMetadata(ContractModel):
    """Non-sensitive source identity and run-level source characteristics."""

    source_id: str = Field(min_length=1, description="Stable caller-provided source identifier.")
    source_type: SourceType
    display_name: str = Field(min_length=1)
    location_hint: str | None = Field(
        default=None,
        description="Redacted logical reference; never store credentials or signed URLs.",
    )
    format: str | None = None
    encoding: str | None = None
    delimiter: str | None = Field(default=None, max_length=4)
    declared_size_bytes: int | None = Field(default=None, ge=0)
    metadata_dictionary_name: str | None = None
    source_version: str | None = None
    fingerprint: str | None = Field(
        default=None,
        description="Non-reversible source/configuration fingerprint used for traceability.",
    )


class InputAssetProvenance(ContractModel):
    """Non-sensitive identity for one uploaded input asset."""

    filename: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class RuleRegistryProvenance(ContractModel):
    """Traceability for the exact uploaded rule set used by a run."""

    file_count: int = Field(ge=1)
    filenames: list[str] = Field(min_length=1)
    upload_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    canonical_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class UnderstandingInputProvenance(ContractModel):
    """Safe input manifest; contains no source rows or filesystem paths."""

    sample_csv: InputAssetProvenance
    metadata_dictionary: InputAssetProvenance
    rule_registry: RuleRegistryProvenance


class ValueFrequency(ContractModel):
    """A bounded, non-sensitive frequency observation."""

    value: JsonValue
    count: int = Field(ge=0)
    percentage: float = Field(ge=0.0, le=100.0)
    redacted: bool = False


class NumericStatistics(ContractModel):
    count: int = Field(ge=0)
    minimum: float | None = None
    maximum: float | None = None
    mean: float | None = None
    standard_deviation: float | None = Field(default=None, ge=0.0)
    quantiles: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_range(self) -> "NumericStatistics":
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum cannot be greater than maximum")
        return self


class StringStatistics(ContractModel):
    count: int = Field(ge=0)
    minimum_length: int | None = Field(default=None, ge=0)
    maximum_length: int | None = Field(default=None, ge=0)
    average_length: float | None = Field(default=None, ge=0.0)
    blank_count: int = Field(default=0, ge=0)
    whitespace_only_count: int = Field(default=0, ge=0)


class DateTimeStatistics(ContractModel):
    count: int = Field(ge=0)
    minimum: datetime | None = None
    maximum: datetime | None = None

    @model_validator(mode="after")
    def validate_range(self) -> "DateTimeStatistics":
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum cannot be greater than maximum")
        return self


class BoundedSampleSummary(ContractModel):
    """Safe sample metadata; values must be redacted, hashed, or synthetic."""

    strategy: Literal["none", "redacted", "hashed", "synthetic"] = "none"
    values: list[JsonValue] = Field(default_factory=list)
    observed_count: int = Field(default=0, ge=0)
    retained_count: int = Field(default=0, ge=0)
    truncated: bool = False

    @model_validator(mode="after")
    def validate_sample(self) -> "BoundedSampleSummary":
        if self.retained_count != len(self.values):
            raise ValueError("retained_count must equal the number of retained values")
        if self.strategy == "none" and self.values:
            raise ValueError("strategy='none' cannot contain sample values")
        return self


class ColumnProfile(ContractModel):
    """Mergeable, bounded profile for one source column."""

    column_name: str = Field(min_length=1)
    source_data_type: str | None = None
    inferred_data_type: LogicalDataType = LogicalDataType.UNKNOWN
    inferred_type_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    row_count: int = Field(ge=0)
    non_null_count: int = Field(ge=0)
    null_count: int = Field(ge=0)
    fill_rate: float = Field(ge=0.0, le=1.0)
    parse_success_count: int = Field(default=0, ge=0)
    parse_failure_count: int = Field(default=0, ge=0)
    distinct_count: int | None = Field(default=None, ge=0)
    distinct_count_mode: Literal["exact", "capped", "estimated", "not_computed"] = "not_computed"
    distinct_count_lower_bound: int | None = Field(default=None, ge=0)
    top_values: list[ValueFrequency] = Field(default_factory=list)
    numeric_statistics: NumericStatistics | None = None
    string_statistics: StringStatistics | None = None
    datetime_statistics: DateTimeStatistics | None = None
    safe_samples: BoundedSampleSummary = Field(default_factory=BoundedSampleSummary)
    detected_patterns: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_counts(self) -> "ColumnProfile":
        if self.non_null_count + self.null_count != self.row_count:
            raise ValueError("non_null_count + null_count must equal row_count")
        expected = 0.0 if self.row_count == 0 else self.non_null_count / self.row_count
        if abs(self.fill_rate - expected) > 1e-6:
            raise ValueError("fill_rate must equal non_null_count / row_count")
        if self.distinct_count_mode == "exact" and self.distinct_count is None:
            raise ValueError("exact distinct counting requires distinct_count")
        return self


class DatasetProfile(ContractModel):
    """Complete deterministic profile created by a single chunked source pass."""

    profile_version: str = "1.0"
    row_count: int = Field(ge=0)
    column_count: int = Field(ge=0)
    chunks_processed: int = Field(ge=0)
    bytes_processed: int | None = Field(default=None, ge=0)
    started_at: datetime
    completed_at: datetime
    elapsed_seconds: float = Field(ge=0.0)
    limits: ProcessingLimits
    columns: list[ColumnProfile]
    duplicate_row_count: int | None = Field(
        default=None,
        ge=0,
        description="Only populated when a bounded or source-native strategy supports it.",
    )
    duplicate_count_mode: Literal["exact", "estimated", "not_computed"] = "not_computed"
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_dataset(self) -> "DatasetProfile":
        if self.completed_at < self.started_at:
            raise ValueError("completed_at cannot precede started_at")
        if self.column_count != len(self.columns):
            raise ValueError("column_count must equal the number of column profiles")
        names = [column.column_name.casefold() for column in self.columns]
        if len(names) != len(set(names)):
            raise ValueError("column names must be unique, ignoring case")
        if any(column.row_count != self.row_count for column in self.columns):
            raise ValueError("every column profile must use the dataset row_count")
        return self


class EvidenceReference(ContractModel):
    """Bounded evidence without raw row or transcript content."""

    evidence_type: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    metric_name: str | None = None
    metric_value: JsonValue = None
    column_names: list[str] = Field(default_factory=list)
    sample_count: int | None = Field(default=None, ge=0)
    artifact_reference: str | None = None
    redacted: bool = True


class DomainAssignment(ContractModel):
    """Explainable semantic-domain classification for a source column."""

    column_name: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    subdomain: str | None = None
    semantic_type: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    confidence_level: ConfidenceLevel
    provenance: list[ProvenanceType] = Field(min_length=1)
    matched_features: list[str] = Field(default_factory=list)
    rationale: str = Field(min_length=1)
    suggested_by_llm: bool = False
    requires_review: bool = False

    @model_validator(mode="after")
    def validate_llm_classification(self) -> "DomainAssignment":
        if self.suggested_by_llm:
            if ProvenanceType.LLM_SUGGESTION.value not in self.provenance:
                raise ValueError("LLM suggestions require llm_suggestion provenance")
            if not self.requires_review:
                raise ValueError("LLM-suggested classifications must require review")
        return self


class RelationshipCandidate(ContractModel):
    """Explainable, non-authoritative relationship discovered from bounded data."""

    relationship_id: str = Field(min_length=1)
    relationship_type: RelationshipType
    source_columns: list[str] = Field(min_length=1)
    target_columns: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    method: str = Field(min_length=1)
    evidence: list[EvidenceReference] = Field(default_factory=list)
    rationale: str = Field(min_length=1)
    requires_review: bool = True

    @field_validator("source_columns", "target_columns")
    @classmethod
    def unique_columns(cls, value: list[str]) -> list[str]:
        if len({item.casefold() for item in value}) != len(value):
            raise ValueError("relationship columns must be unique")
        return value


class RecommendedAction(ContractModel):
    """Policy-derived remediation advice attached to a DQ signal."""

    action_code: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    owner_role: str | None = None
    priority: Severity
    source: ProvenanceType = ProvenanceType.CONFIGURATION
    requires_approval: bool = False


class DQSignal(ContractModel):
    """A ranked observation suggesting a potential data-quality issue."""

    signal_id: str = Field(min_length=1)
    signal_type: str = Field(min_length=1)
    dimension: str = Field(min_length=1)
    status: SignalStatus = SignalStatus.OBSERVED
    severity: Severity
    confidence: float = Field(ge=0.0, le=1.0)
    affected_columns: list[str] = Field(default_factory=list)
    observed_value: JsonValue = None
    threshold: JsonValue = None
    message: str = Field(min_length=1)
    evidence: list[EvidenceReference] = Field(default_factory=list)
    recommended_actions: list[RecommendedAction] = Field(default_factory=list)
    policy_id: str | None = None
    provenance: list[ProvenanceType] = Field(min_length=1)


class RuleParameterBinding(ContractModel):
    """Resolved parameter value supplied to an applicable registered rule."""

    name: str = Field(min_length=1)
    value: JsonValue
    source: Literal["rule_default", "configuration", "metadata", "inference", "user"]


class RuleApplicability(ContractModel):
    """Decision connecting a versioned registry rule to this dataset."""

    rule_id: str = Field(min_length=1)
    rule_version: str = Field(min_length=1)
    check_name: RequiredCheck | str
    status: RuleApplicabilityStatus
    execution_readiness: ExecutionReadiness = ExecutionReadiness.READY
    target_columns: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=1)
    matched_domains: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    missing_capabilities: list[str] = Field(default_factory=list)
    parameters: list[RuleParameterBinding] = Field(default_factory=list)
    provenance: list[ProvenanceType] = Field(min_length=1)
    suggested_by_llm: bool = False
    approved: bool = False

    @model_validator(mode="after")
    def validate_decision(self) -> "RuleApplicability":
        if self.execution_readiness == ExecutionReadiness.READY.value and self.missing_capabilities:
            raise ValueError("an execution-ready rule cannot have missing capabilities")
        if (
            self.execution_readiness == ExecutionReadiness.IMPLEMENTATION_PENDING.value
            and not self.missing_capabilities
        ):
            raise ValueError("implementation_pending requires at least one missing capability")
        if (
            self.status != RuleApplicabilityStatus.APPLICABLE.value
            and self.execution_readiness != ExecutionReadiness.NOT_REQUIRED.value
        ):
            raise ValueError("non-applicable rules must use execution_readiness='not_required'")
        if self.suggested_by_llm and self.approved:
            raise ValueError("LLM-suggested rules cannot be automatically approved")
        if self.suggested_by_llm and ProvenanceType.LLM_SUGGESTION.value not in self.provenance:
            raise ValueError("LLM-suggested rules require llm_suggestion provenance")
        return self


class RuleExecutionStep(ContractModel):
    """One serializable unit of work for the downstream Execution Layer."""

    step_id: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    rule_version: str = Field(min_length=1)
    check_name: RequiredCheck | str
    target_columns: list[str] = Field(default_factory=list)
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    dependencies: list[str] = Field(default_factory=list)
    priority: int = Field(default=100, ge=0)
    parallelizable: bool = True
    timeout_seconds: int | None = Field(default=None, ge=1)


class ExecutionPlan(ContractModel):
    """Deterministic hand-off from Understanding to Execution."""

    plan_id: str = Field(min_length=1)
    plan_version: str = "1.0"
    status: PlanStatus
    created_at: datetime = Field(default_factory=_utc_now)
    steps: list[RuleExecutionStep] = Field(default_factory=list)
    selected_rules: list[str] = Field(default_factory=list)
    implementation_pending_rules: list[str] = Field(default_factory=list)
    blocked_rules: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    registry_fingerprint: str | None = None

    @model_validator(mode="after")
    def validate_steps(self) -> "ExecutionPlan":
        step_ids = [step.step_id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("execution plan step_id values must be unique")
        known = set(step_ids)
        for step in self.steps:
            unknown = set(step.dependencies) - known
            if unknown:
                raise ValueError(f"step {step.step_id!r} has unknown dependencies: {sorted(unknown)}")
            if step.step_id in step.dependencies:
                raise ValueError(f"step {step.step_id!r} cannot depend on itself")
        runnable_rules = {step.rule_id for step in self.steps}
        selected_rules = set(self.selected_rules)
        pending_rules = set(self.implementation_pending_rules)
        if selected_rules and not runnable_rules.issubset(selected_rules):
            raise ValueError("every runnable rule must be present in selected_rules")
        if selected_rules and not pending_rules.issubset(selected_rules):
            raise ValueError("every pending rule must be present in selected_rules")
        if runnable_rules & pending_rules:
            raise ValueError("a rule cannot be runnable and implementation_pending")
        if set(self.blocked_rules) != pending_rules:
            raise ValueError("blocked_rules must remain a compatibility alias of implementation_pending_rules")
        return self


class LLMInsight(ContractModel):
    """Optional post-profiling interpretation; never an approved rule or fact."""

    insight_id: str = Field(min_length=1)
    insight_type: Literal[
        "business_interpretation",
        "signal_explanation",
        "domain_suggestion",
        "rule_suggestion",
        "recommended_action",
    ]
    title: str = Field(min_length=1)
    narrative: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    supporting_signal_ids: list[str] = Field(default_factory=list)
    supporting_columns: list[str] = Field(default_factory=list)
    provider: str = Field(min_length=1)
    model: str | None = None
    request_id: str | None = None
    generated_at: datetime = Field(default_factory=_utc_now)
    provenance: Literal["llm_suggestion"] = "llm_suggestion"
    requires_review: Literal[True] = True
    approved: Literal[False] = False


class MetadataKnowledgeBase(ContractModel):
    """Run-scoped context assembled from metadata and deterministic inference."""

    dictionary_name: str | None = None
    dictionary_version: str | None = None
    column_descriptions: dict[str, str] = Field(default_factory=dict)
    declared_types: dict[str, str] = Field(default_factory=dict)
    business_terms: dict[str, list[str]] = Field(default_factory=dict)
    unmatched_source_columns: list[str] = Field(default_factory=list)
    dictionary_only_columns: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class UnderstandingOutput(ContractModel):
    """Canonical result consumed by Execution and persisted as compact artifacts."""

    contract_version: str = "1.0"
    run_id: str = Field(min_length=1)
    status: Literal["completed", "completed_with_warnings", "failed"]
    created_at: datetime = Field(default_factory=_utc_now)
    source: SourceMetadata
    input_provenance: UnderstandingInputProvenance | None = None
    profile: DatasetProfile
    metadata_knowledge_base: MetadataKnowledgeBase
    domains: list[DomainAssignment] = Field(default_factory=list)
    relationships: list[RelationshipCandidate] = Field(default_factory=list)
    dq_signals: list[DQSignal] = Field(default_factory=list)
    rule_applicability: list[RuleApplicability] = Field(default_factory=list)
    execution_plan: ExecutionPlan
    llm_insights: list[LLMInsight] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    artifact_references: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_output(self) -> "UnderstandingOutput":
        if self.status == "failed" and not self.errors:
            raise ValueError("a failed Understanding output must contain at least one error")
        if self.status != "failed" and self.errors:
            raise ValueError("non-failed Understanding output cannot contain errors")
        column_names = {column.column_name.casefold() for column in self.profile.columns}
        referenced = {
            assignment.column_name.casefold() for assignment in self.domains
        }
        referenced.update(
            column.casefold()
            for relationship in self.relationships
            for column in relationship.source_columns + relationship.target_columns
        )
        referenced.update(
            column.casefold() for signal in self.dq_signals for column in signal.affected_columns
        )
        unknown = referenced - column_names
        if unknown:
            raise ValueError(f"context objects reference unknown columns: {sorted(unknown)}")
        return self


UnderstandingOutput.model_rebuild()
