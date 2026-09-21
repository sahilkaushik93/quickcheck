"""Transcript word-volume to media-duration consistency handler."""

from __future__ import annotations

import math
from typing import Mapping

from app.execution.aggregation.accumulators import InternalAggregationKey, RuleGroupAccumulator
from app.execution.features.models import FeatureRequirements, RowFeatures
from app.execution.handlers.base import HandlerConfigurationError, HandlerState, RuleHandler
from app.execution.models import MetricOutcome, RuleMetric
from app.understanding.models import JsonValue, RequiredCheck, RuleExecutionStep


class SpeechPerDurationRateHandler(RuleHandler):
    """Calculate words per minute using paired transcript and duration fields."""

    check_name = RequiredCheck.SPEECH_PER_DURATION_RATE.value
    feature_requirements = FeatureRequirements(tokenize=True, calculate_speech_statistics=True)

    def validate_parameters(self, parameters: Mapping[str, JsonValue], step: RuleExecutionStep) -> None:
        super().validate_parameters(parameters, step)
        unit = self.parameter(step, "duration_unit", str, required=True)
        minimum_rate = self.parameter(step, "minimum_words_per_minute", float, required=True)
        maximum_rate = self.parameter(step, "maximum_words_per_minute", float, required=True)
        minimum_duration = self.parameter(step, "minimum_duration", float, required=True)
        warning_minimum = self.parameter(step, "warning_minimum_words_per_minute", float, default=30.0)
        warning_maximum = self.parameter(step, "warning_maximum_words_per_minute", float, default=300.0)
        if unit not in {"milliseconds", "seconds"}:
            raise HandlerConfigurationError("INVALID_DURATION_UNIT", "duration_unit must be milliseconds or seconds.", rule_id=step.rule_id)
        if minimum_rate is None or maximum_rate is None or minimum_rate < 0 or maximum_rate <= minimum_rate:
            raise HandlerConfigurationError("INVALID_SPEECH_RATE_RANGE", "Speech-rate bounds must be non-negative and increasing.", rule_id=step.rule_id)
        if minimum_duration is None or minimum_duration < 0:
            raise HandlerConfigurationError("INVALID_MINIMUM_DURATION", "minimum_duration cannot be negative.", rule_id=step.rule_id)
        if warning_minimum is None or warning_maximum is None or warning_minimum > minimum_rate or warning_maximum < maximum_rate:
            raise HandlerConfigurationError("INVALID_SPEECH_WARNING_RANGE", "Warning bounds must enclose the pass range.", rule_id=step.rule_id)
        if not step.target_columns:
            raise HandlerConfigurationError("DURATION_TARGET_REQUIRED", "Speech-rate execution requires a duration target column.", rule_id=step.rule_id)

    def observe_group(self, state: HandlerState, row: RowFeatures, key: InternalAggregationKey, group: RuleGroupAccumulator) -> None:
        duration = self._duration_value(state, row)
        minimum_duration = self.parameter(state.step, "minimum_duration", float, required=True)
        unit = self.parameter(state.step, "duration_unit", str, required=True)
        minimum_rate = self.parameter(state.step, "minimum_words_per_minute", float, required=True)
        maximum_rate = self.parameter(state.step, "maximum_words_per_minute", float, required=True)
        warning_minimum = self.parameter(state.step, "warning_minimum_words_per_minute", float, default=30.0)
        warning_maximum = self.parameter(state.step, "warning_maximum_words_per_minute", float, default=300.0)
        assert minimum_duration is not None and unit is not None and minimum_rate is not None and maximum_rate is not None and warning_minimum is not None and warning_maximum is not None
        if not row.transcript.present or duration is None or duration < minimum_duration or not row.transcript.normalized_tokens:
            group.counts.observe(eligible=False)
            return
        seconds = duration / 1000.0 if unit == "milliseconds" else duration
        if seconds <= 0:
            group.counts.observe(eligible=False)
            return
        word_count = len(row.transcript.normalized_tokens)
        words_per_minute = word_count / (seconds / 60.0)
        status = "pass" if minimum_rate <= words_per_minute <= maximum_rate else "warn" if warning_minimum <= words_per_minute <= warning_maximum else "fail"
        group.counts.observe(eligible=True, passed=status == "pass")
        group.frequency("speech_rate_status", capacity=3).observe(status)
        group.numeric("word_count").observe(word_count)
        group.numeric("duration_seconds").observe(seconds)
        group.numeric("row_words_per_minute").observe(words_per_minute)

    def metrics_for_group(self, state: HandlerState, key: InternalAggregationKey, group: RuleGroupAccumulator) -> tuple[RuleMetric, ...]:
        minimum_rate = self.parameter(state.step, "minimum_words_per_minute", float, required=True)
        maximum_rate = self.parameter(state.step, "maximum_words_per_minute", float, required=True)
        warning_minimum = self.parameter(state.step, "warning_minimum_words_per_minute", float, default=30.0)
        warning_maximum = self.parameter(state.step, "warning_maximum_words_per_minute", float, default=300.0)
        assert minimum_rate is not None and maximum_rate is not None and warning_minimum is not None and warning_maximum is not None
        words = group.numeric("word_count").total
        seconds = group.numeric("duration_seconds").total
        aggregate_rate = words / (seconds / 60.0) if seconds > 0 else None
        outcome = MetricOutcome.NOT_EVALUATED if aggregate_rate is None else MetricOutcome.PASSED if minimum_rate <= aggregate_rate <= maximum_rate else MetricOutcome.WARNING if warning_minimum <= aggregate_rate <= warning_maximum else MetricOutcome.FAILED
        status_counts = {str(name): count for name, count in group.frequency("speech_rate_status", capacity=3).top()}
        row_rates = group.numeric("row_words_per_minute")
        return (
            RuleMetric(name="speech_words_per_minute", value=self.rounded(aggregate_rate, state) if aggregate_rate is not None else None, unit="words_per_minute", numerator=int(words), denominator=seconds, threshold={"pass_minimum": minimum_rate, "pass_maximum": maximum_rate, "warning_minimum": warning_minimum, "warning_maximum": warning_maximum}, outcome=outcome),
            RuleMetric(name="row_words_per_minute_summary", value={"average": self.rounded(row_rates.mean or 0.0, state) if row_rates.count else None, "minimum": self.rounded(row_rates.minimum or 0.0, state) if row_rates.count else None, "maximum": self.rounded(row_rates.maximum or 0.0, state) if row_rates.count else None}, unit="words_per_minute", outcome=MetricOutcome.NOT_EVALUATED),
            RuleMetric(name="speech_rate_status_counts", value=status_counts, unit="count", outcome=MetricOutcome.NOT_EVALUATED),
        )

    @staticmethod
    def _duration_value(state: HandlerState, row: RowFeatures) -> float | None:
        configured = state.step.parameters.get("duration_column")
        candidates = ([str(configured)] if configured else []) + list(state.step.target_columns)
        for column in candidates:
            value = row.value(column)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                number = float(value)
                if math.isfinite(number):
                    return number
        return None


__all__ = ["SpeechPerDurationRateHandler"]
