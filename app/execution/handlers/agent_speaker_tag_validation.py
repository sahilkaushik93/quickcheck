"""Validation of canonical speaker structure extracted once per transcript."""

from __future__ import annotations

from typing import Mapping

from app.execution.aggregation.accumulators import InternalAggregationKey, RuleGroupAccumulator
from app.execution.features.models import FeatureRequirements, RowFeatures
from app.execution.handlers.base import HandlerConfigurationError, HandlerState, RuleHandler
from app.execution.models import MetricOutcome, RuleMetric
from app.understanding.models import JsonValue, RequiredCheck, RuleExecutionStep


class AgentSpeakerTagValidationHandler(RuleHandler):
    """Assess required roles, unknown labels and empty speaker turns."""

    check_name = RequiredCheck.AGENT_SPEAKER_TAG_VALIDATION.value
    feature_requirements = FeatureRequirements(inspect_speaker_tags=True)

    def validate_parameters(self, parameters: Mapping[str, JsonValue], step: RuleExecutionStep) -> None:
        super().validate_parameters(parameters, step)
        labels = self.parameter(step, "allowed_speaker_labels", list, required=True)
        threshold = self.parameter(step, "maximum_invalid_rate", float, required=True)
        if not labels or not all(isinstance(value, str) and value.strip() for value in labels):
            raise HandlerConfigurationError("INVALID_SPEAKER_LABELS", "allowed_speaker_labels must contain non-empty strings.", rule_id=step.rule_id)
        if threshold is None or not 0.0 <= threshold <= 1.0:
            raise HandlerConfigurationError("INVALID_SPEAKER_THRESHOLD", "maximum_invalid_rate must be between 0 and 1.", rule_id=step.rule_id)

    def observe_group(self, state: HandlerState, row: RowFeatures, key: InternalAggregationKey, group: RuleGroupAccumulator) -> None:
        if not row.transcript.present:
            group.counts.observe(eligible=False)
            return
        speaker = row.transcript.speaker
        valid = (
            speaker.total_turns > 0
            and speaker.required_roles_present
            and speaker.unknown_label_count == 0
            and speaker.empty_turn_count == 0
        )
        group.counts.observe(eligible=True, passed=valid)
        group.numeric("speaker_turn_count").observe(speaker.total_turns)
        group.numeric("unknown_label_count").observe(speaker.unknown_label_count)
        group.numeric("empty_turn_count").observe(speaker.empty_turn_count)
        group.numeric("mismatched_utterance_count").observe(speaker.unknown_label_count + speaker.empty_turn_count)
        if speaker.total_turns == 0:
            group.frequency("speaker_failure_categories", capacity=min(4, state.limits.maximum_categories_per_group)).observe("no_speaker_tags")
        if not speaker.required_roles_present:
            group.frequency("speaker_failure_categories", capacity=min(4, state.limits.maximum_categories_per_group)).observe("required_role_missing")
        if speaker.unknown_label_count:
            group.frequency("speaker_failure_categories", capacity=min(4, state.limits.maximum_categories_per_group)).observe("unknown_label")
        if speaker.empty_turn_count:
            group.frequency("speaker_failure_categories", capacity=min(4, state.limits.maximum_categories_per_group)).observe("empty_turn")

    def metrics_for_group(self, state: HandlerState, key: InternalAggregationKey, group: RuleGroupAccumulator) -> tuple[RuleMetric, ...]:
        threshold = self.parameter(state.step, "maximum_invalid_rate", float, required=True)
        assert threshold is not None
        eligible = group.counts.eligible_count
        invalid = group.counts.failed_count
        invalid_rate = invalid / eligible if eligible else 0.0
        outcome = MetricOutcome.NOT_EVALUATED if not eligible else MetricOutcome.PASSED if invalid_rate <= threshold else MetricOutcome.FAILED
        categories = {str(name): count for name, count in group.frequency("speaker_failure_categories", capacity=min(4, state.limits.maximum_categories_per_group)).top()}
        total_turns = int(group.numeric("speaker_turn_count").total)
        mismatched_turns = int(group.numeric("mismatched_utterance_count").total)
        return (
            RuleMetric(name="invalid_speaker_tag_rate", value=self.rounded(invalid_rate, state) if eligible else None, unit="ratio", numerator=invalid, denominator=eligible, threshold=threshold, outcome=outcome),
            RuleMetric(name="speaker_tag_summary", value={"rows_evaluated": eligible, "rows_with_mismatch": invalid, "rows_matching": group.counts.passed_count, "mismatch_rate": self.rounded(invalid_rate, state) if eligible else None, "total_utterances": total_turns, "mismatched_utterances": mismatched_turns}, unit="count", outcome=outcome),
            RuleMetric(name="speaker_failure_categories", value=categories, unit="count", outcome=MetricOutcome.NOT_EVALUATED),
            RuleMetric(name="average_speaker_turns", value=self.rounded(group.numeric("speaker_turn_count").mean or 0.0, state) if eligible else None, unit="turns_per_transcript", outcome=MetricOutcome.NOT_EVALUATED),
        )


__all__ = ["AgentSpeakerTagValidationHandler"]
