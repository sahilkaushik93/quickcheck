"""Runtime policy and resolution context for bounded grouped execution.

``app.execution.models`` owns the canonical API/run contracts.  This module
deliberately imports and re-exports those types instead of redefining them, then
adds configuration models used by the aggregation resolver.  Keeping one
canonical ``AggregationRequest`` and ``AggregationKey`` prevents subtle API,
handler, and artifact incompatibilities.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import Field, field_validator, model_validator

from app.execution.models import (
    AggregationKey,
    AggregationRequest,
    DimensionSpecification,
    FilterOperator,
    FilterSpecification,
    HighCardinalityBehavior,
    InvalidTimestampPolicy,
    NullBucketPolicy,
    ResolvedAggregationSpecification,
    ResolvedDimension,
    TimeAggregationRequest,
    TimeGrain,
)
from app.understanding.models import ContractModel, LogicalDataType


class AggregationConfigurationError(RuntimeError):
    """Safe error raised when aggregation policy cannot be constructed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


class TimePolicy(ContractModel):
    """Configuration governing time-column discovery and bucketing."""

    allowed_grains: list[TimeGrain] = Field(min_length=1)
    default_grain: TimeGrain
    default_timezone: str = Field(min_length=1)
    candidate_columns: list[str] = Field(default_factory=list)
    accepted_formats: list[str] = Field(default_factory=list)
    invalid_timestamp_policy: InvalidTimestampPolicy
    unknown_bucket_label: str = Field(min_length=1, max_length=100)
    week_starts_on: str = Field(default="monday", pattern=r"^(monday|sunday)$")
    fiscal_year_start_month: int = Field(default=1, ge=1, le=12)
    unknown_range_bucket_estimates: dict[TimeGrain, int] = Field(
        default_factory=lambda: {
            TimeGrain.DAY: 366,
            TimeGrain.WEEK: 104,
            TimeGrain.MONTH: 24,
            TimeGrain.QUARTER: 8,
            TimeGrain.YEAR: 5,
        }
    )

    @model_validator(mode="after")
    def validate_time_policy(self) -> "TimePolicy":
        if self.default_grain not in self.allowed_grains:
            raise ValueError("default_grain must be included in allowed_grains")
        names = [name.casefold() for name in self.candidate_columns]
        if len(names) != len(set(names)):
            raise ValueError("candidate time columns must be unique")
        if any(value < 1 for value in self.unknown_range_bucket_estimates.values()):
            raise ValueError("unknown time-range bucket estimates must be positive")
        return self


class DimensionPolicy(ContractModel):
    """Safety limits and semantic exclusions for grouping dimensions."""

    maximum_dimensions: int = Field(ge=0, le=20)
    maximum_groups: int = Field(ge=1)
    maximum_cardinality_per_dimension: int = Field(ge=1)
    high_cardinality_behavior: HighCardinalityBehavior
    default_null_policy: NullBucketPolicy
    null_bucket_label: str = Field(min_length=1, max_length=100)
    other_bucket_label: str = Field(min_length=1, max_length=100)
    role_aliases: dict[str, list[str]] = Field(default_factory=dict)
    allowed_semantic_types: list[str] = Field(default_factory=list)
    forbidden_semantic_types: list[str] = Field(default_factory=list)
    forbidden_name_patterns: list[str] = Field(default_factory=list)
    sensitive_columns_allowed: bool = False
    free_text_columns_allowed: bool = False

    @field_validator("forbidden_name_patterns")
    @classmethod
    def validate_patterns(cls, values: list[str]) -> list[str]:
        for value in values:
            re.compile(value)
        return values

    @model_validator(mode="after")
    def validate_dimension_policy(self) -> "DimensionPolicy":
        overlap = {
            value.casefold() for value in self.allowed_semantic_types
        } & {value.casefold() for value in self.forbidden_semantic_types}
        if overlap:
            raise ValueError(
                f"semantic types cannot be both allowed and forbidden: {sorted(overlap)}"
            )
        for role, aliases in self.role_aliases.items():
            if not role.strip() or not aliases:
                raise ValueError("each dimension role requires at least one alias")
            normalized = [value.casefold() for value in aliases]
            if len(normalized) != len(set(normalized)):
                raise ValueError(f"duplicate aliases configured for role {role!r}")
        return self


class FilterPolicy(ContractModel):
    """Limits for declarative filters evaluated during chunk processing."""

    allowed_operators: list[FilterOperator] = Field(min_length=1)
    maximum_filters: int = Field(ge=0)
    maximum_values_per_filter: int = Field(ge=1)
    case_sensitive_strings: bool = False
    allow_filters_on_sensitive_columns: bool = False
    allow_filters_on_free_text_columns: bool = False

    @model_validator(mode="after")
    def validate_operators(self) -> "FilterPolicy":
        if len(self.allowed_operators) != len(set(self.allowed_operators)):
            raise ValueError("allowed filter operators must be unique")
        return self


class AggregationPolicy(ContractModel):
    """Complete validated policy loaded from ``aggregation_policy.json``."""

    schema_version: str = "1.0"
    config_id: str = Field(min_length=1)
    time: TimePolicy
    dimensions: DimensionPolicy
    filters: FilterPolicy
    include_overall_by_default: bool = True
    include_empty_groups_by_default: bool = False

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> "AggregationPolicy":
        """Validate a decoded JSON mapping and expose a safe configuration error."""

        try:
            return cls.model_validate(values)
        except Exception as exc:
            raise AggregationConfigurationError(
                "INVALID_AGGREGATION_POLICY",
                f"Aggregation policy is invalid ({type(exc).__name__}).",
            ) from exc


class AggregationColumnDescriptor(ContractModel):
    """Compact source/profile facts used to approve an aggregation column."""

    column_name: str = Field(min_length=1)
    logical_type: LogicalDataType = LogicalDataType.UNKNOWN
    semantic_type: str | None = None
    domain: str | None = None
    distinct_count: int | None = Field(default=None, ge=0)
    distinct_count_is_lower_bound: bool = False
    sensitive: bool = False
    free_text: bool = False


class CardinalityEstimate(ContractModel):
    """Bounded estimate recorded before creating grouped accumulator state."""

    per_dimension: dict[str, int] = Field(default_factory=dict)
    time_bucket_count: int = Field(default=1, ge=1)
    estimated_group_count: int = Field(ge=1)
    maximum_groups: int = Field(ge=1)
    exact: bool = False
    safe: bool
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_estimate(self) -> "CardinalityEstimate":
        if any(value < 0 for value in self.per_dimension.values()):
            raise ValueError("dimension cardinality estimates cannot be negative")
        if self.safe != (self.estimated_group_count <= self.maximum_groups):
            raise ValueError("safe must reflect estimated_group_count and maximum_groups")
        return self


__all__ = [
    "AggregationColumnDescriptor",
    "AggregationConfigurationError",
    "AggregationKey",
    "AggregationPolicy",
    "AggregationRequest",
    "CardinalityEstimate",
    "DimensionPolicy",
    "DimensionSpecification",
    "FilterOperator",
    "FilterPolicy",
    "FilterSpecification",
    "HighCardinalityBehavior",
    "InvalidTimestampPolicy",
    "NullBucketPolicy",
    "ResolvedAggregationSpecification",
    "ResolvedDimension",
    "TimeAggregationRequest",
    "TimeGrain",
    "TimePolicy",
]
