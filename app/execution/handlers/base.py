"""Abstract contracts and lifecycle controls for deterministic DQ handlers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, TypeVar

from app.execution.aggregation.accumulators import (
    BoundedGroupedAccumulator,
    InternalAggregationKey,
    RuleGroupAccumulator,
)
from app.execution.features.models import FeatureRequirements, RowFeatures
from app.execution.models import (
    ResolvedAggregationSpecification,
    RuleExecutionResult,
    RuleExecutionStatus,
    RuleMetric,
)
from app.understanding.models import JsonValue, RequiredCheck, RuleExecutionStep


class HandlerError(RuntimeError):
    """Base safe exception for handler validation or execution failures."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        rule_id: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.rule_id = rule_id
        self.retryable = retryable

    def as_dict(self) -> dict[str, str | bool]:
        result: dict[str, str | bool] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.rule_id is not None:
            result["rule_id"] = self.rule_id
        return result


class HandlerConfigurationError(HandlerError):
    """Raised before scanning when a plan step cannot be executed safely."""


class HandlerExecutionError(HandlerError):
    """Raised during observation, merge, or finalization."""


class HandlerStateStatus(str, Enum):
    CREATED = "created"
    OBSERVING = "observing"
    MERGED = "merged"
    FINALIZED = "finalized"


@dataclass(frozen=True, slots=True)
class HandlerLimits:
    """Bounded runtime limits supplied from ``execution_config.json``."""

    maximum_categories_per_group: int
    maximum_warnings: int
    decimal_places: int = 6

    def __post_init__(self) -> None:
        if self.maximum_categories_per_group < 1:
            raise ValueError("maximum_categories_per_group must be positive")
        if self.maximum_warnings < 0:
            raise ValueError("maximum_warnings cannot be negative")
        if not 0 <= self.decimal_places <= 15:
            raise ValueError("decimal_places must be between 0 and 15")


@dataclass(slots=True)
class HandlerState:
    """Run-scoped state for one rule step; never shared across workers."""

    step: RuleExecutionStep
    aggregation: ResolvedAggregationSpecification
    limits: HandlerLimits
    groups: BoundedGroupedAccumulator
    warnings: list[str] = field(default_factory=list)
    warning_overflow_count: int = 0
    status: HandlerStateStatus = HandlerStateStatus.CREATED

    def add_warning(self, message: str) -> None:
        """Retain a bounded, de-duplicated, non-sensitive warning."""

        if message in self.warnings:
            return
        if len(self.warnings) < self.limits.maximum_warnings:
            self.warnings.append(message)
        else:
            self.warning_overflow_count += 1


T = TypeVar("T")


class RuleHandler(ABC):
    """Stateless handler implementation shared across request-scoped states.

    Concrete handlers implement only per-group observation and metric
    finalization. This base owns lifecycle validation, aggregation independence,
    deterministic merging, and construction of the public rule result.
    """

    check_name: RequiredCheck | str
    feature_requirements: FeatureRequirements = FeatureRequirements()

    def prepare(
        self,
        step: RuleExecutionStep,
        aggregation: ResolvedAggregationSpecification,
        limits: HandlerLimits,
    ) -> HandlerState:
        """Validate a plan step and allocate only bounded accumulator state."""

        self.validate_step(step)
        return HandlerState(
            step=step,
            aggregation=aggregation,
            limits=limits,
            groups=BoundedGroupedAccumulator(
                aggregation,
                maximum_categories_per_metric=limits.maximum_categories_per_group,
            ),
        )

    def validate_step(self, step: RuleExecutionStep) -> None:
        """Validate the plan/handler binding without reading source rows."""

        if str(step.check_name) != str(self.check_name):
            raise HandlerConfigurationError(
                "HANDLER_CHECK_MISMATCH",
                "The resolved handler does not implement the requested check.",
                rule_id=step.rule_id,
            )
        if not step.rule_id.strip() or not step.step_id.strip():
            raise HandlerConfigurationError(
                "INVALID_EXECUTION_STEP",
                "Execution step identifiers must be non-empty.",
                rule_id=step.rule_id or None,
            )
        self.validate_parameters(step.parameters, step)

    def validate_parameters(
        self, parameters: Mapping[str, JsonValue], step: RuleExecutionStep
    ) -> None:
        """Validate handler-specific resolved parameters.

        The default accepts parameters so concrete handlers can remain backward
        compatible. Handlers with a strict schema should override this method.
        """

        if any(not str(name).strip() for name in parameters):
            raise HandlerConfigurationError(
                "INVALID_RULE_PARAMETER",
                "Rule parameter names must be non-empty.",
                rule_id=step.rule_id,
            )

    def observe(self, state: HandlerState, row: RowFeatures) -> None:
        """Apply one already-extracted row to overall and grouped state."""

        self._require_active(state)
        try:
            groups = state.groups.groups_for_values(row.values)
            for key, group in groups:
                self.observe_group(state, row, key, group)
            state.status = HandlerStateStatus.OBSERVING
        except HandlerError:
            raise
        except Exception as exc:
            raise HandlerExecutionError(
                "HANDLER_OBSERVATION_FAILED",
                f"Rule observation failed ({type(exc).__name__}).",
                rule_id=state.step.rule_id,
            ) from exc

    def merge(self, target: HandlerState, partial: HandlerState) -> None:
        """Merge a compact worker result in deterministic chunk order."""

        self._require_active(target)
        self._require_active(partial)
        if target.step != partial.step or target.aggregation != partial.aggregation:
            raise HandlerExecutionError(
                "INCOMPATIBLE_HANDLER_STATE",
                "Handler states must reference the same step and aggregation plan.",
                rule_id=target.step.rule_id,
            )
        if target.limits != partial.limits:
            raise HandlerExecutionError(
                "INCOMPATIBLE_HANDLER_LIMITS",
                "Handler states must use identical bounded limits.",
                rule_id=target.step.rule_id,
            )
        target.groups.merge(partial.groups)
        for warning in partial.warnings:
            target.add_warning(warning)
        target.warning_overflow_count += partial.warning_overflow_count
        self.merge_custom_state(target, partial)
        target.status = HandlerStateStatus.MERGED

    def merge_custom_state(self, target: HandlerState, partial: HandlerState) -> None:
        """Hook for handlers whose compact state extends grouped accumulators."""

        return None

    def finalize(
        self,
        state: HandlerState,
        *,
        elapsed_seconds: float,
    ) -> RuleExecutionResult:
        """Finalize aggregate-only output and make the state immutable-by-lifecycle."""

        self._require_active(state)
        if elapsed_seconds < 0:
            raise HandlerExecutionError(
                "INVALID_ELAPSED_TIME",
                "Handler elapsed time cannot be negative.",
                rule_id=state.step.rule_id,
            )
        metrics = {
            key: tuple(self.metrics_for_group(state, key, group))
            for key, group in self._ordered_group_states(state)
        }
        warnings = list(state.warnings)
        if state.warning_overflow_count:
            warnings.append(
                f"{state.warning_overflow_count} additional handler warnings were suppressed."
            )
        if state.groups.invalid_timestamp_count:
            warnings.append(
                f"{state.groups.invalid_timestamp_count} rows contained invalid aggregation timestamps."
            )
        if state.groups.excluded_row_count:
            warnings.append(
                f"{state.groups.excluded_row_count} rows were excluded from grouped results by aggregation policy."
            )
        if state.groups.overflow_row_count:
            warnings.append(
                f"{state.groups.overflow_row_count} rows were assigned to a bounded overflow group."
            )
        status = (
            RuleExecutionStatus.COMPLETED_WITH_WARNINGS
            if warnings
            else RuleExecutionStatus.COMPLETED
        )
        result = RuleExecutionResult(
            step_id=state.step.step_id,
            rule_id=state.step.rule_id,
            rule_version=state.step.rule_version,
            check_name=state.step.check_name,
            status=status,
            target_columns=state.step.target_columns,
            groups=list(state.groups.finalized_groups(metrics_by_group=metrics)),
            elapsed_seconds=elapsed_seconds,
            warnings=warnings,
        )
        state.status = HandlerStateStatus.FINALIZED
        return result

    @abstractmethod
    def observe_group(
        self,
        state: HandlerState,
        row: RowFeatures,
        key: InternalAggregationKey,
        group: RuleGroupAccumulator,
    ) -> None:
        """Update one compact group accumulator from already-shared features."""

        raise NotImplementedError

    @abstractmethod
    def metrics_for_group(
        self,
        state: HandlerState,
        key: InternalAggregationKey,
        group: RuleGroupAccumulator,
    ) -> tuple[RuleMetric, ...]:
        """Build deterministic finalized metrics for one aggregate group."""

        raise NotImplementedError

    @staticmethod
    def parameter(
        step: RuleExecutionStep,
        name: str,
        expected_type: type[T],
        *,
        default: T | None = None,
        required: bool = False,
    ) -> T | None:
        """Read one resolved rule parameter with strict runtime type checking."""

        if name not in step.parameters:
            if required:
                raise HandlerConfigurationError(
                    "MISSING_RULE_PARAMETER",
                    f"Required parameter {name!r} is missing.",
                    rule_id=step.rule_id,
                )
            return default
        value = step.parameters[name]
        if expected_type is float and isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)  # type: ignore[return-value]
        if expected_type is int and isinstance(value, int) and not isinstance(value, bool):
            return value  # type: ignore[return-value]
        if not isinstance(value, expected_type):
            raise HandlerConfigurationError(
                "INVALID_RULE_PARAMETER_TYPE",
                f"Parameter {name!r} has an invalid type.",
                rule_id=step.rule_id,
            )
        return value

    @staticmethod
    def rounded(value: float, state: HandlerState) -> float:
        return round(value, state.limits.decimal_places)

    @staticmethod
    def _ordered_group_states(
        state: HandlerState,
    ) -> tuple[tuple[InternalAggregationKey, RuleGroupAccumulator], ...]:
        # Access is deliberately kept behind the bounded accumulator's compact
        # state. Concrete handlers never depend on source chunks or DataFrames.
        groups = state.groups._groups  # noqa: SLF001 - same execution subsystem
        return tuple(
            (key, groups[key])
            for key in sorted(groups, key=state.groups._sort_key)  # noqa: SLF001
        )

    @staticmethod
    def _require_active(state: HandlerState) -> None:
        if state.status == HandlerStateStatus.FINALIZED:
            raise HandlerExecutionError(
                "HANDLER_STATE_FINALIZED",
                "A finalized handler state cannot be reused.",
                rule_id=state.step.rule_id,
            )


__all__ = [
    "HandlerConfigurationError",
    "HandlerError",
    "HandlerExecutionError",
    "HandlerLimits",
    "HandlerState",
    "HandlerStateStatus",
    "RuleHandler",
]
