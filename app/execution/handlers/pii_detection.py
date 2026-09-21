"""Privacy-safe aggregate PII detection handler."""

from __future__ import annotations

from typing import Mapping

from app.execution.aggregation.accumulators import InternalAggregationKey, RuleGroupAccumulator
from app.execution.features.models import FeatureRequirements, RowFeatures
from app.execution.handlers.base import HandlerConfigurationError, HandlerState, RuleHandler
from app.execution.models import MetricOutcome, RuleMetric
from app.understanding.models import JsonValue, RequiredCheck, RuleExecutionStep


class PIIDetectionHandler(RuleHandler):
    """Aggregate configured PII categories without receiving raw matches."""

    check_name = RequiredCheck.PII_DETECTION.value
    feature_requirements = FeatureRequirements(inspect_pii=True)

    def validate_parameters(self, parameters: Mapping[str, JsonValue], step: RuleExecutionStep) -> None:
        super().validate_parameters(parameters, step)
        categories = self.parameter(step, "pii_categories", list, required=True)
        maximum = self.parameter(step, "maximum_record_rate", float, required=True)
        store_matches = self.parameter(step, "store_matches", bool, required=True)
        if not categories or not all(isinstance(item, str) and item.strip() for item in categories):
            raise HandlerConfigurationError("INVALID_PII_CATEGORIES", "pii_categories must contain non-empty category names.", rule_id=step.rule_id)
        if maximum is None or not 0.0 <= maximum <= 1.0:
            raise HandlerConfigurationError("INVALID_PII_THRESHOLD", "maximum_record_rate must be between 0 and 1.", rule_id=step.rule_id)
        if store_matches is not False:
            raise HandlerConfigurationError("RAW_PII_STORAGE_FORBIDDEN", "store_matches must remain false.", rule_id=step.rule_id)

    def observe_group(self, state: HandlerState, row: RowFeatures, key: InternalAggregationKey, group: RuleGroupAccumulator) -> None:
        if not row.transcript.present:
            group.counts.observe(eligible=False)
            return
        enabled = {str(value).casefold() for value in (self.parameter(state.step, "pii_categories", list, required=True) or [])}
        selected = [(category, count) for category, count in row.transcript.pii_category_counts if category.casefold() in enabled]
        occurrence_count = sum(count for _, count in selected)
        group.counts.observe(eligible=True, passed=occurrence_count == 0)
        group.numeric("pii_occurrence_count").observe(occurrence_count)
        frequency = group.frequency("pii_category_counts", capacity=state.limits.maximum_categories_per_group)
        for category, count in selected:
            frequency.observe(category, weight=count)

    def metrics_for_group(self, state: HandlerState, key: InternalAggregationKey, group: RuleGroupAccumulator) -> tuple[RuleMetric, ...]:
        maximum = self.parameter(state.step, "maximum_record_rate", float, required=True)
        assert maximum is not None
        eligible = group.counts.eligible_count
        exposed = group.counts.failed_count
        record_rate = exposed / eligible if eligible else 0.0
        outcome = MetricOutcome.NOT_EVALUATED if not eligible else MetricOutcome.PASSED if record_rate <= maximum else MetricOutcome.FAILED
        frequency = group.frequency("pii_category_counts", capacity=state.limits.maximum_categories_per_group)
        categories = {str(category): count for category, count in frequency.top()}
        if frequency.overflow_count:
            categories["__OTHER_PII_CATEGORY__"] = frequency.overflow_count
        occurrence_count = int(group.numeric("pii_occurrence_count").total)
        return (
            RuleMetric(name="pii_record_rate", value=self.rounded(record_rate, state) if eligible else None, unit="ratio", numerator=exposed, denominator=eligible, threshold=maximum, outcome=outcome),
            RuleMetric(name="pii_occurrence_count", value=occurrence_count, unit="count", outcome=MetricOutcome.NOT_EVALUATED),
            RuleMetric(name="pii_category_counts", value=categories, unit="count", outcome=MetricOutcome.NOT_EVALUATED),
        )


__all__ = ["PIIDetectionHandler"]
