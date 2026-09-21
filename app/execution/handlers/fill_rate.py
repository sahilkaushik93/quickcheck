"""Deterministic grouped fill-rate handler."""

from __future__ import annotations

from typing import Mapping

from app.execution.aggregation.accumulators import InternalAggregationKey, RuleGroupAccumulator
from app.execution.features.models import FeatureRequirements, RowFeatures
from app.execution.handlers.base import HandlerConfigurationError, HandlerState, RuleHandler
from app.execution.models import MetricOutcome, RuleMetric
from app.understanding.models import JsonValue, RequiredCheck, RuleExecutionStep


class FillRateHandler(RuleHandler):
    """Measure populated cells overall and per configured target column."""

    check_name = RequiredCheck.FILL_RATE.value
    feature_requirements = FeatureRequirements()

    def validate_parameters(
        self, parameters: Mapping[str, JsonValue], step: RuleExecutionStep
    ) -> None:
        super().validate_parameters(parameters, step)
        threshold = self.parameter(step, "minimum_fill_rate", float, required=True)
        warning = self.parameter(step, "warning_fill_rate", float, default=0.70)
        if threshold is None or not 0.0 <= threshold <= 1.0:
            raise HandlerConfigurationError(
                "INVALID_FILL_RATE_THRESHOLD",
                "minimum_fill_rate must be between 0 and 1.",
                rule_id=step.rule_id,
            )
        if warning is None or not 0.0 <= warning < threshold:
            raise HandlerConfigurationError(
                "INVALID_FILL_RATE_WARNING_THRESHOLD",
                "warning_fill_rate must be lower than minimum_fill_rate.",
                rule_id=step.rule_id,
            )
        if not step.target_columns:
            raise HandlerConfigurationError(
                "FILL_RATE_TARGET_REQUIRED",
                "Fill-rate execution requires at least one target column.",
                rule_id=step.rule_id,
            )

    def observe_group(
        self,
        state: HandlerState,
        row: RowFeatures,
        key: InternalAggregationKey,
        group: RuleGroupAccumulator,
    ) -> None:
        populated = 0
        for column in state.step.target_columns:
            # Transcript text is deliberately absent from ``RowFeatures.values``.
            # The coordinator projects every other fill-rate target, making the
            # compact presence feature the privacy-safe transcript fallback.
            present = (
                self._present(row.value(column))
                if column in row.values
                else row.transcript.present
            )
            group.numeric(f"observed::{column}").observe(1)
            group.numeric(f"populated::{column}").observe(1 if present else 0)
            populated += int(present)
        group.counts.observe(
            eligible=True,
            passed=populated == len(state.step.target_columns),
        )

    def metrics_for_group(
        self,
        state: HandlerState,
        key: InternalAggregationKey,
        group: RuleGroupAccumulator,
    ) -> tuple[RuleMetric, ...]:
        threshold = self.parameter(
            state.step, "minimum_fill_rate", float, required=True
        )
        assert threshold is not None
        warning = self.parameter(state.step, "warning_fill_rate", float, default=0.70)
        assert warning is not None
        per_column: dict[str, dict[str, object]] = {}
        total_populated = 0.0
        total_observed = 0
        for column in state.step.target_columns:
            observed = group.numeric(f"observed::{column}").count
            populated = group.numeric(f"populated::{column}").total
            column_rate = populated / observed if observed else 0.0
            per_column[column] = {
                "total": observed,
                "filled": int(populated),
                "missing": max(0, observed - int(populated)),
                "fill_rate": self.rounded(column_rate, state),
                "status": "pass" if column_rate >= threshold else "warn" if column_rate > warning else "fail",
            }
            total_populated += populated
            total_observed += observed
        rate = total_populated / total_observed if total_observed else 0.0
        outcome = MetricOutcome.PASSED if rate >= threshold else MetricOutcome.WARNING if rate > warning else MetricOutcome.FAILED
        return (
            RuleMetric(
                name="fill_rate",
                value=self.rounded(rate, state),
                unit="ratio",
                numerator=int(total_populated),
                denominator=total_observed,
                threshold={"pass_at_or_above": threshold, "warn_above": warning},
                outcome=outcome,
            ),
            RuleMetric(
                name="fill_rate_by_column",
                value=per_column,
                unit="ratio",
                threshold={"pass_at_or_above": threshold, "warn_above": warning},
                outcome=outcome,
            ),
        )

    @staticmethod
    def _present(value: object) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return value.strip().casefold() not in {"", "null", "none", "nan", "n/a", "na", "not available"}
        return True


__all__ = ["FillRateHandler"]
