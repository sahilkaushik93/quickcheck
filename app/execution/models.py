"""Canonical contracts for deterministic transcript DQ execution.

The models in this module form the stable boundary between the Understanding
execution plan, aggregation resolution, rule handlers, evidence collection,
the execution coordinator, and public API adapters.  They intentionally carry
only compact aggregate state and redacted references: source rows, transcript
text, raw PII matches, credentials, and local filesystem paths do not belong in
these contracts.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pathlib import PurePosixPath
from typing import Literal

from pydantic import Field, field_validator, model_validator

from app.understanding.models import (
    ContractModel,
    ExecutionPlan,
    JsonScalar,
    JsonValue,
    RequiredCheck,
    Severity,
    SourceType,
)


def _utc_now() -> datetime:
    """Return an aware UTC timestamp for default factories."""

    return datetime.now(timezone.utc)


class TimeGrain(str, Enum):
    """Supported deterministic time-bucketing grains."""

    NONE = "none"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"


class FilterOperator(str, Enum):
    """Safe filter operations supported by the execution resolver."""

    EQUALS = "equals"
    NOT_EQUALS = "not_equals"
    IN = "in"
    NOT_IN = "not_in"
    GREATER_THAN = "greater_than"
    GREATER_THAN_OR_EQUAL = "greater_than_or_equal"
    LESS_THAN = "less_than"
    LESS_THAN_OR_EQUAL = "less_than_or_equal"
    BETWEEN = "between"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"


class NullBucketPolicy(str, Enum):
    """How null dimension values are represented during grouping."""

    INCLUDE = "include"
    EXCLUDE = "exclude"
    FAIL = "fail"


class HighCardinalityBehavior(str, Enum):
    """Policy applied when grouping would exceed configured limits."""

    REJECT = "reject"
    OTHER_BUCKET = "other_bucket"


class InvalidTimestampPolicy(str, Enum):
    """Handling policy for timestamps that cannot be parsed safely."""

    EXCLUDE = "exclude"
    UNKNOWN_BUCKET = "unknown_bucket"
    FAIL = "fail"


class ExecutionStatus(str, Enum):
    """Lifecycle result for a complete Execution run."""

    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    PARTIAL = "partial"
    FAILED = "failed"


class RuleExecutionStatus(str, Enum):
    """Run-specific status of one planned rule."""

    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    SKIPPED = "skipped"
    IMPLEMENTATION_PENDING = "implementation_pending"
    FAILED = "failed"


class MetricOutcome(str, Enum):
    """Threshold evaluation outcome for a finalized metric."""

    PASSED = "passed"
    FAILED = "failed"
    WARNING = "warning"
    NOT_EVALUATED = "not_evaluated"


class MetricAvailabilityStatus(str, Enum):
    """Whether a metric could be calculated for the current source."""

    AVAILABLE = "available"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class ExecutionSourceIdentity(ContractModel):
    """Non-sensitive identity of the source scanned during execution."""

    source_id: str = Field(min_length=1)
    source_type: SourceType
    display_name: str = Field(min_length=1)
    fingerprint: str = Field(
        pattern=r"^[a-f0-9]{64}$",
        description="SHA-256 of the exact source bytes or canonical source snapshot.",
    )
    declared_size_bytes: int | None = Field(default=None, ge=0)
    source_version: str | None = None


class ExecutionOptions(ContractModel):
    """Bounded runtime controls supplied to an Execution run."""

    chunk_size: int = Field(default=10_000, ge=1)
    parallel_workers: int = Field(default=1, ge=1, le=64)
    max_in_flight_chunks: int = Field(default=1, ge=1, le=128)
    max_evidence_per_rule_group: int = Field(default=5, ge=0, le=1_000)
    max_total_evidence: int = Field(default=1_000, ge=0, le=100_000)
    fail_fast: bool = False
    persist_artifacts: bool = False

    @model_validator(mode="after")
    def validate_parallel_bounds(self) -> "ExecutionOptions":
        if self.parallel_workers == 1 and self.max_in_flight_chunks != 1:
            raise ValueError(
                "max_in_flight_chunks must be 1 when parallel_workers is 1"
            )
        return self


class TimeAggregationRequest(ContractModel):
    """Caller-requested time bucket before schema-aware resolution."""

    column: str | None = None
    grain: TimeGrain = TimeGrain.NONE
    timezone: str = Field(default="UTC", min_length=1)
    invalid_timestamp_policy: InvalidTimestampPolicy = InvalidTimestampPolicy.UNKNOWN_BUCKET
    unknown_bucket_label: str = Field(default="__UNKNOWN_TIME__", min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_time_request(self) -> "TimeAggregationRequest":
        if self.grain == TimeGrain.NONE.value and self.column is not None:
            raise ValueError("time column must be omitted when grain='none'")
        return self


class DimensionSpecification(ContractModel):
    """One caller-selected low-cardinality grouping dimension."""

    column: str = Field(min_length=1)
    alias: str | None = Field(default=None, min_length=1)
    null_policy: NullBucketPolicy = NullBucketPolicy.INCLUDE
    null_bucket_label: str = Field(default="__UNKNOWN__", min_length=1, max_length=100)
    maximum_cardinality: int | None = Field(default=None, ge=1)
    other_bucket_label: str = Field(default="__OTHER__", min_length=1, max_length=100)

    @property
    def output_name(self) -> str:
        """Return the stable name used in output aggregation keys."""

        return self.alias or self.column


class FilterSpecification(ContractModel):
    """A validated, declarative source filter with no executable expressions."""

    column: str = Field(min_length=1)
    operator: FilterOperator
    values: list[JsonScalar] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_operand_count(self) -> "FilterSpecification":
        count = len(self.values)
        operator = str(self.operator)
        if operator in {FilterOperator.IS_NULL.value, FilterOperator.IS_NOT_NULL.value}:
            if count:
                raise ValueError(f"operator {operator!r} does not accept values")
        elif operator == FilterOperator.BETWEEN.value:
            if count != 2:
                raise ValueError("operator 'between' requires exactly two values")
        elif operator in {FilterOperator.IN.value, FilterOperator.NOT_IN.value}:
            if count < 1:
                raise ValueError(f"operator {operator!r} requires at least one value")
        elif count != 1:
            raise ValueError(f"operator {operator!r} requires exactly one value")
        return self


class AggregationRequest(ContractModel):
    """User controls for time and low-cardinality grouped results."""

    time: TimeAggregationRequest = Field(default_factory=TimeAggregationRequest)
    dimensions: list[DimensionSpecification] = Field(default_factory=list)
    filters: list[FilterSpecification] = Field(default_factory=list)
    include_overall: bool = True
    include_empty_groups: bool = False
    maximum_groups: int | None = Field(default=None, ge=1)
    high_cardinality_behavior: HighCardinalityBehavior = HighCardinalityBehavior.REJECT

    @model_validator(mode="after")
    def validate_unique_dimensions(self) -> "AggregationRequest":
        columns = [item.column.casefold() for item in self.dimensions]
        if len(columns) != len(set(columns)):
            raise ValueError("aggregation dimension columns must be unique")
        output_names = [item.output_name.casefold() for item in self.dimensions]
        if len(output_names) != len(set(output_names)):
            raise ValueError("aggregation dimension aliases must be unique")
        return self


class ResolvedDimension(ContractModel):
    """Schema-validated dimension used by the streaming coordinator."""

    column: str = Field(min_length=1)
    output_name: str = Field(min_length=1)
    null_policy: NullBucketPolicy
    null_bucket_label: str = Field(min_length=1)
    cardinality_limit: int = Field(ge=1)
    observed_or_estimated_cardinality: int | None = Field(default=None, ge=0)
    other_bucket_label: str = Field(default="__OTHER__", min_length=1)


class ResolvedAggregationSpecification(ContractModel):
    """Deterministic aggregation plan after source/profile validation."""

    time_column: str | None = None
    time_grain: TimeGrain = TimeGrain.NONE
    timezone: str = Field(default="UTC", min_length=1)
    invalid_timestamp_policy: InvalidTimestampPolicy = InvalidTimestampPolicy.UNKNOWN_BUCKET
    unknown_time_bucket_label: str = Field(default="__UNKNOWN_TIME__", min_length=1)
    dimensions: list[ResolvedDimension] = Field(default_factory=list)
    filters: list[FilterSpecification] = Field(default_factory=list)
    include_overall: bool = True
    include_empty_groups: bool = False
    maximum_groups: int = Field(ge=1)
    estimated_group_count: int | None = Field(default=None, ge=0)
    high_cardinality_behavior: HighCardinalityBehavior = HighCardinalityBehavior.REJECT

    @model_validator(mode="after")
    def validate_resolved_time(self) -> "ResolvedAggregationSpecification":
        if self.time_grain == TimeGrain.NONE.value and self.time_column is not None:
            raise ValueError("resolved time column must be omitted for grain='none'")
        if self.time_grain != TimeGrain.NONE.value and not self.time_column:
            raise ValueError("resolved time column is required for a time grain")
        if (
            self.estimated_group_count is not None
            and self.estimated_group_count > self.maximum_groups
            and self.high_cardinality_behavior == HighCardinalityBehavior.REJECT.value
        ):
            raise ValueError("estimated group count exceeds maximum_groups")
        return self


class AggregationKey(ContractModel):
    """Canonical key identifying one finalized aggregate group."""

    time_period: str | None = None
    dimensions: dict[str, JsonScalar] = Field(default_factory=dict)
    overall: bool = False

    @model_validator(mode="after")
    def validate_key_shape(self) -> "AggregationKey":
        if self.overall and (self.time_period is not None or self.dimensions):
            raise ValueError("an overall aggregation key cannot contain group values")
        if not self.overall and self.time_period is None and not self.dimensions:
            raise ValueError("a non-overall aggregation key requires a time or dimension value")
        return self


class MetricAvailability(ContractModel):
    """Availability and coverage of one execution-time metric."""

    rule_id: str = Field(min_length=1)
    metric_name: str = Field(min_length=1)
    status: MetricAvailabilityStatus
    reason: str | None = None
    eligible_count: int = Field(default=0, ge=0)
    unavailable_count: int = Field(default=0, ge=0)
    coverage: float = Field(default=0.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_availability(self) -> "MetricAvailability":
        if self.status == MetricAvailabilityStatus.UNAVAILABLE.value and not self.reason:
            raise ValueError("an unavailable metric requires a reason")
        return self


class RuleMetric(ContractModel):
    """One compact observed metric and its threshold evaluation."""

    name: str = Field(min_length=1)
    value: JsonValue
    unit: str | None = None
    numerator: int | float | None = None
    denominator: int | float | None = None
    threshold: JsonValue = None
    outcome: MetricOutcome = MetricOutcome.NOT_EVALUATED

    @model_validator(mode="after")
    def validate_ratio_inputs(self) -> "RuleMetric":
        if self.denominator is not None and self.denominator < 0:
            raise ValueError("metric denominator cannot be negative")
        if self.numerator is not None and self.numerator < 0:
            raise ValueError("metric numerator cannot be negative")
        return self


class GroupedRuleMetrics(ContractModel):
    """Finalized counts and metrics for one rule and aggregation group."""

    aggregation_key: AggregationKey
    row_count: int = Field(ge=0)
    eligible_count: int = Field(ge=0)
    passed_count: int = Field(default=0, ge=0)
    failed_count: int = Field(default=0, ge=0)
    skipped_count: int = Field(default=0, ge=0)
    metrics: list[RuleMetric] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_counts(self) -> "GroupedRuleMetrics":
        if self.eligible_count > self.row_count:
            raise ValueError("eligible_count cannot exceed row_count")
        if self.passed_count + self.failed_count > self.eligible_count:
            raise ValueError("passed_count plus failed_count cannot exceed eligible_count")
        if self.skipped_count > self.row_count:
            raise ValueError("skipped_count cannot exceed row_count")
        metric_names = [item.name.casefold() for item in self.metrics]
        if len(metric_names) != len(set(metric_names)):
            raise ValueError("group metric names must be unique")
        return self


class RuleExecutionResult(ContractModel):
    """Complete observed result for one versioned execution-plan step."""

    step_id: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    rule_version: str = Field(min_length=1)
    check_name: RequiredCheck | str
    status: RuleExecutionStatus
    target_columns: list[str] = Field(default_factory=list)
    groups: list[GroupedRuleMetrics] = Field(default_factory=list)
    elapsed_seconds: float = Field(default=0.0, ge=0.0)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_result_status(self) -> "RuleExecutionResult":
        if self.status == RuleExecutionStatus.FAILED.value and not self.errors:
            raise ValueError("a failed rule execution requires at least one error")
        if self.status != RuleExecutionStatus.FAILED.value and self.errors:
            raise ValueError("only failed rule executions may contain errors")
        if self.status == RuleExecutionStatus.SKIPPED.value and self.groups:
            raise ValueError("a skipped rule execution cannot contain grouped metrics")
        return self


class RedactedEvidence(ContractModel):
    """One bounded failure reference containing no source or matched value."""

    evidence_id: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    step_id: str = Field(min_length=1)
    aggregation_key: AggregationKey
    row_fingerprint: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    failure_type: str = Field(min_length=1)
    category: str | None = None
    occurrence_count: int = Field(default=1, ge=1)
    metadata: dict[str, JsonScalar] = Field(default_factory=dict)
    redacted: Literal[True] = True


class EvidenceCollection(ContractModel):
    """Bounded evidence retained for one rule/group pair."""

    rule_id: str = Field(min_length=1)
    aggregation_key: AggregationKey
    observed_failure_count: int = Field(ge=0)
    retained_count: int = Field(ge=0)
    truncated: bool = False
    items: list[RedactedEvidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_evidence_counts(self) -> "EvidenceCollection":
        if self.retained_count != len(self.items):
            raise ValueError("retained_count must equal the number of evidence items")
        if self.retained_count > self.observed_failure_count:
            raise ValueError("retained evidence cannot exceed observed failures")
        if self.truncated != (self.observed_failure_count > self.retained_count):
            raise ValueError("truncated must reflect omitted evidence")
        if any(item.rule_id != self.rule_id for item in self.items):
            raise ValueError("all evidence items must belong to the collection rule")
        if any(item.aggregation_key != self.aggregation_key for item in self.items):
            raise ValueError("all evidence items must belong to the collection group")
        return self


class ExecutionWarning(ContractModel):
    """Structured non-fatal execution diagnostic."""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    severity: Severity = Severity.MEDIUM
    rule_id: str | None = None
    column_name: str | None = None
    aggregation_key: AggregationKey | None = None


class ExecutionSummary(ContractModel):
    """Compact operational summary of one complete source scan."""

    rows_read: int = Field(ge=0)
    rows_after_filters: int = Field(ge=0)
    chunks_processed: int = Field(ge=0)
    groups_created: int = Field(ge=0)
    rules_requested: int = Field(ge=0)
    rules_executed: int = Field(ge=0)
    rules_completed: int = Field(ge=0)
    rules_skipped: int = Field(ge=0)
    rules_failed: int = Field(ge=0)
    evidence_observed: int = Field(default=0, ge=0)
    evidence_retained: int = Field(default=0, ge=0)
    elapsed_seconds: float = Field(ge=0.0)

    @model_validator(mode="after")
    def validate_summary(self) -> "ExecutionSummary":
        if self.rows_after_filters > self.rows_read:
            raise ValueError("rows_after_filters cannot exceed rows_read")
        if self.rules_executed > self.rules_requested:
            raise ValueError("rules_executed cannot exceed rules_requested")
        if self.rules_completed + self.rules_skipped + self.rules_failed > self.rules_requested:
            raise ValueError("final rule counts cannot exceed rules_requested")
        if self.evidence_retained > self.evidence_observed:
            raise ValueError("retained evidence cannot exceed observed evidence")
        return self


class ArtifactReference(ContractModel):
    """Safe reference to a compact artifact produced after successful execution."""

    artifact_type: str = Field(min_length=1)
    relative_name: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=0)
    media_type: str = Field(default="application/json", min_length=1)

    @field_validator("relative_name")
    @classmethod
    def validate_relative_name(cls, value: str) -> str:
        candidate = PurePosixPath(value)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("artifact relative_name must be a safe relative path")
        return value


class ExecutionRunRequest(ContractModel):
    """Source-independent request accepted by the Execution coordinator."""

    run_id: str | None = Field(default=None, min_length=1)
    understanding_run_id: str = Field(min_length=1)
    source: ExecutionSourceIdentity
    execution_plan: ExecutionPlan
    aggregation: AggregationRequest = Field(default_factory=AggregationRequest)
    options: ExecutionOptions = Field(default_factory=ExecutionOptions)


class ExecutionOutput(ContractModel):
    """Canonical aggregate-only output consumed by Business Impact."""

    contract_version: str = "1.0"
    run_id: str = Field(min_length=1)
    understanding_run_id: str = Field(min_length=1)
    status: ExecutionStatus
    created_at: datetime = Field(default_factory=_utc_now)
    source: ExecutionSourceIdentity
    execution_plan_id: str = Field(min_length=1)
    registry_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    aggregation: ResolvedAggregationSpecification
    summary: ExecutionSummary
    metric_availability: list[MetricAvailability] = Field(default_factory=list)
    rule_results: list[RuleExecutionResult] = Field(default_factory=list)
    evidence: list[EvidenceCollection] = Field(default_factory=list)
    warnings: list[ExecutionWarning] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    artifact_references: list[ArtifactReference] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_output(self) -> "ExecutionOutput":
        if self.status == ExecutionStatus.FAILED.value and not self.errors:
            raise ValueError("a failed Execution output must contain at least one error")
        if self.status != ExecutionStatus.FAILED.value and self.errors:
            raise ValueError("non-failed Execution output cannot contain run-level errors")

        result_steps = [item.step_id for item in self.rule_results]
        if len(result_steps) != len(set(result_steps)):
            raise ValueError("rule_results step_id values must be unique")

        retained = sum(item.retained_count for item in self.evidence)
        if retained != self.summary.evidence_retained:
            raise ValueError("summary evidence_retained does not match evidence collections")
        if self.summary.rules_requested < len(self.rule_results):
            raise ValueError("rule_results cannot exceed summary rules_requested")
        return self


ExecutionOutput.model_rebuild()
