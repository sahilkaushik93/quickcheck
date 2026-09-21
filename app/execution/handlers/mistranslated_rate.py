"""Configured domain mistranslation-rate handler."""

from __future__ import annotations

from typing import Mapping

from app.execution.aggregation.accumulators import InternalAggregationKey, RuleGroupAccumulator
from app.execution.features.models import FeatureRequirements, RowFeatures
from app.execution.handlers.base import HandlerConfigurationError, HandlerState, RuleHandler
from app.execution.models import MetricOutcome, RuleMetric
from app.understanding.models import JsonValue, RequiredCheck, RuleExecutionStep


class MistranslatedRateHandler(RuleHandler):
    """Aggregate approved lexicon matches already extracted without raw terms."""

    check_name = RequiredCheck.MISTRANSLATED_RATE.value
    feature_requirements = FeatureRequirements(tokenize=True, inspect_mistranslations=True)

    def validate_parameters(self, parameters: Mapping[str, JsonValue], step: RuleExecutionStep) -> None:
        super().validate_parameters(parameters, step)
        reference = self.parameter(step, "lexicon_reference", str, required=True)
        maximum = self.parameter(step, "maximum_mistranslated_rate", float, required=True)
        self.parameter(step, "case_sensitive", bool, required=True)
        if not reference:
            raise HandlerConfigurationError("MISTRANSLATION_LEXICON_REQUIRED", "lexicon_reference must be non-empty.", rule_id=step.rule_id)
        if maximum is None or not 0.0 <= maximum <= 1.0:
            raise HandlerConfigurationError("INVALID_MISTRANSLATION_THRESHOLD", "maximum_mistranslated_rate must be between 0 and 1.", rule_id=step.rule_id)

    def observe_group(self, state: HandlerState, row: RowFeatures, key: InternalAggregationKey, group: RuleGroupAccumulator) -> None:
        token_count = row.transcript.token_count
        if not row.transcript.present or token_count <= 0:
            group.counts.observe(eligible=False)
            return
        count = row.transcript.mistranslation_count
        group.counts.observe(eligible=True, passed=count == 0)
        group.numeric("evaluated_token_count").observe(token_count)
        group.numeric("mistranslation_count").observe(count)
        frequency = group.frequency("mistranslation_categories", capacity=state.limits.maximum_categories_per_group)
        for category, occurrences in row.transcript.mistranslation_category_counts:
            frequency.observe(category, weight=occurrences)

    def metrics_for_group(self, state: HandlerState, key: InternalAggregationKey, group: RuleGroupAccumulator) -> tuple[RuleMetric, ...]:
        maximum = self.parameter(state.step, "maximum_mistranslated_rate", float, required=True)
        assert maximum is not None
        tokens = int(group.numeric("evaluated_token_count").total)
        occurrences = int(group.numeric("mistranslation_count").total)
        rate = occurrences / tokens if tokens else None
        outcome = MetricOutcome.NOT_EVALUATED if rate is None else MetricOutcome.PASSED if rate <= maximum else MetricOutcome.FAILED
        categories = {str(category): count for category, count in group.frequency("mistranslation_categories", capacity=state.limits.maximum_categories_per_group).top()}
        return (
            RuleMetric(name="mistranslated_rate", value=self.rounded(rate, state) if rate is not None else None, unit="matches_per_token", numerator=occurrences, denominator=tokens, threshold=maximum, outcome=outcome),
            RuleMetric(name="mistranslation_category_counts", value=categories, unit="count", outcome=MetricOutcome.NOT_EVALUATED),
        )


__all__ = ["MistranslatedRateHandler"]
