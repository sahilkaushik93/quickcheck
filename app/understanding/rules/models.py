"""Persistent, versioned DQ rule-definition contracts.

These models describe approved configuration stored in the rule registry. They
are intentionally separate from run-specific ``RuleApplicability`` and
``RuleExecutionStep`` models in ``app.understanding.models``.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.understanding.models import JsonValue, RequiredCheck, Severity


class RuleContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        use_enum_values=True,
        str_strip_whitespace=True,
    )


class RuleLifecycle(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    DEPRECATED = "deprecated"


class RuleLevel(str, Enum):
    COLUMN = "column"
    DATASET = "dataset"
    RELATIONSHIP = "relationship"


class ExecutionStrategy(str, Enum):
    PROFILE_METRIC = "profile_metric"
    AGGREGATE_DISTRIBUTION = "aggregate_distribution"
    STREAMING_ROW = "streaming_row"
    STREAMING_PAIR = "streaming_pair"


class ParameterType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    STRING_LIST = "string_list"


class RuleParameterDefinition(RuleContract):
    """Schema and default for one configurable execution parameter."""

    parameter_type: ParameterType
    description: str = Field(min_length=1)
    required: bool = False
    default: JsonValue = None
    minimum: float | None = None
    maximum: float | None = None
    allowed_values: list[JsonValue] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_bounds(self) -> "RuleParameterDefinition":
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("parameter minimum cannot exceed maximum")
        if self.required and self.default is None:
            raise ValueError("required parameters must define an MVP default")
        if self.default is not None:
            self._validate_value(self.default)
        return self

    def _validate_value(self, value: JsonValue) -> None:
        kind = str(self.parameter_type)
        valid = {
            ParameterType.STRING.value: isinstance(value, str),
            ParameterType.INTEGER.value: isinstance(value, int) and not isinstance(value, bool),
            ParameterType.NUMBER.value: isinstance(value, (int, float)) and not isinstance(value, bool),
            ParameterType.BOOLEAN.value: isinstance(value, bool),
            ParameterType.STRING_LIST.value: isinstance(value, list) and all(isinstance(item, str) for item in value),
        }.get(kind, False)
        if not valid:
            raise ValueError(f"default does not match parameter_type={kind!r}")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if self.minimum is not None and value < self.minimum:
                raise ValueError("default is below parameter minimum")
            if self.maximum is not None and value > self.maximum:
                raise ValueError("default is above parameter maximum")
        if self.allowed_values and value not in self.allowed_values:
            raise ValueError("default is not present in allowed_values")


class TargetSelector(RuleContract):
    """Configuration-driven conditions for identifying rule targets."""

    required_domains: list[str] = Field(default_factory=list)
    semantic_types: list[str] = Field(default_factory=list)
    column_name_patterns: list[str] = Field(default_factory=list)
    excluded_column_patterns: list[str] = Field(default_factory=list)
    required_relationship_types: list[str] = Field(default_factory=list)
    minimum_domain_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    match_mode: Literal["any", "all"] = "any"

    @field_validator("column_name_patterns", "excluded_column_patterns")
    @classmethod
    def validate_patterns(cls, values: list[str]) -> list[str]:
        for value in values:
            re.compile(value)
        return values


class EvidencePolicy(RuleContract):
    max_evidence_items: int = Field(default=10, ge=0)
    include_aggregate_metrics: bool = True
    include_raw_values: Literal[False] = False
    include_transcript_text: Literal[False] = False
    redact_matches: bool = True


class RuleDefinition(RuleContract):
    """One immutable-version DQ rule loaded from the registry."""

    schema_version: str = "1.0"
    rule_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    # Retain typed values for the seven MVP checks while permitting governed
    # custom checks to be added through the uploaded registry without a code
    # deployment. Execution readiness remains capability-driven.
    check_name: RequiredCheck | str
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    dimension: str = Field(min_length=1)
    severity: Severity
    level: RuleLevel
    lifecycle: RuleLifecycle = RuleLifecycle.APPROVED
    enabled: bool = True
    owner: str = Field(min_length=1)
    execution_handler: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$")
    execution_strategy: ExecutionStrategy
    selector: TargetSelector
    required_capabilities: list[str] = Field(default_factory=list)
    parameters: dict[str, RuleParameterDefinition] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    evidence_policy: EvidencePolicy = Field(default_factory=EvidencePolicy)
    business_dimensions: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    suggested_by_llm: bool = False
    approved_by: str | None = None

    @model_validator(mode="after")
    def validate_governance(self) -> "RuleDefinition":
        if self.suggested_by_llm and self.lifecycle == RuleLifecycle.APPROVED.value:
            raise ValueError("LLM-suggested rules cannot be stored as approved")
        if self.lifecycle == RuleLifecycle.APPROVED.value and not self.approved_by:
            raise ValueError("approved rules require approved_by")
        if self.rule_id in self.depends_on:
            raise ValueError("a rule cannot depend on itself")
        return self

    @property
    def registry_key(self) -> str:
        return f"{self.rule_id}@{self.version}"


class RuleRegistrySnapshot(RuleContract):
    """Validated immutable registry view used for one Understanding run."""

    rules: tuple[RuleDefinition, ...]
    fingerprint: str = Field(min_length=16)
    source_directory: str
    warnings: tuple[str, ...] = ()
