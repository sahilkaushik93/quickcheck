"""Bounded deterministic non-English and unknown-token assessment."""

from __future__ import annotations

from typing import AbstractSet, Mapping

from app.execution.aggregation.accumulators import InternalAggregationKey, RuleGroupAccumulator
from app.execution.features.models import FeatureRequirements, RowFeatures
from app.execution.handlers.base import HandlerConfigurationError, HandlerState, RuleHandler
from app.execution.models import MetricOutcome, RuleMetric
from app.understanding.models import JsonValue, RequiredCheck, RuleExecutionStep


class NonEnglishSpellingHandler(RuleHandler):
    """Evaluate bounded tokens against an injected governed vocabulary.

    The handler never retains unknown token text. Without a configured
    vocabulary it still reports the deterministic non-ASCII alphabetic rate,
    while spelling availability is explicitly ``not_evaluated``.
    """

    check_name = RequiredCheck.NON_ENGLISH_SPELLING.value
    feature_requirements = FeatureRequirements(tokenize=True)

    def __init__(
        self,
        known_vocabulary: AbstractSet[str] | None = None,
        *,
        minimum_evaluated_tokens: int = 20,
        minimum_vocabulary_size: int = 500,
    ) -> None:
        self._vocabulary = frozenset(
            str(value).casefold() for value in (known_vocabulary or ()) if str(value).strip()
        )
        if minimum_evaluated_tokens < 1:
            raise ValueError("minimum_evaluated_tokens must be positive")
        if minimum_vocabulary_size < 1:
            raise ValueError("minimum_vocabulary_size must be positive")
        self._minimum_evaluated_tokens = minimum_evaluated_tokens
        self._minimum_vocabulary_size = minimum_vocabulary_size

    def validate_parameters(self, parameters: Mapping[str, JsonValue], step: RuleExecutionStep) -> None:
        super().validate_parameters(parameters, step)
        language = self.parameter(step, "expected_language", str, required=True)
        maximum = self.parameter(step, "maximum_unknown_token_rate", float, required=True)
        minimum_length = self.parameter(step, "minimum_token_length", int, required=True)
        if not language:
            raise HandlerConfigurationError("EXPECTED_LANGUAGE_REQUIRED", "expected_language must be non-empty.", rule_id=step.rule_id)
        if language.casefold() != "en":
            raise HandlerConfigurationError("UNSUPPORTED_EXPECTED_LANGUAGE", "The MVP deterministic spelling handler currently supports expected_language='en'.", rule_id=step.rule_id)
        if maximum is None or not 0.0 <= maximum <= 1.0:
            raise HandlerConfigurationError("INVALID_UNKNOWN_TOKEN_THRESHOLD", "maximum_unknown_token_rate must be between 0 and 1.", rule_id=step.rule_id)
        if minimum_length is None or minimum_length < 1:
            raise HandlerConfigurationError("INVALID_MINIMUM_TOKEN_LENGTH", "minimum_token_length must be positive.", rule_id=step.rule_id)

    def observe_group(self, state: HandlerState, row: RowFeatures, key: InternalAggregationKey, group: RuleGroupAccumulator) -> None:
        if not row.transcript.present:
            group.counts.observe(eligible=False)
            return
        group.numeric("alphabetic_character_count").observe(row.transcript.alphabetic_character_count)
        group.numeric("non_ascii_alphabetic_character_count").observe(row.transcript.non_ascii_alphabetic_character_count)
        minimum_length = self.parameter(state.step, "minimum_token_length", int, required=True)
        assert minimum_length is not None
        evaluated = [token for token in row.transcript.normalized_tokens if len(token) >= minimum_length]
        vocabulary_ready = len(self._vocabulary) >= self._minimum_vocabulary_size
        if not vocabulary_ready or not evaluated:
            group.counts.observe(eligible=False)
            if not vocabulary_ready:
                state.add_warning(
                    "Spelling rate was not evaluated because the governed vocabulary "
                    f"contains {len(self._vocabulary)} terms; at least {self._minimum_vocabulary_size} are required to avoid false positives."
                )
            return
        unknown = sum(token.casefold() not in self._vocabulary for token in evaluated)
        maximum = self.parameter(state.step, "maximum_unknown_token_rate", float, required=True)
        assert maximum is not None
        row_rate = unknown / len(evaluated)
        group.counts.observe(eligible=True, passed=row_rate <= maximum)
        group.numeric("evaluated_token_count").observe(len(evaluated))
        group.numeric("unknown_token_count").observe(unknown)

    def metrics_for_group(self, state: HandlerState, key: InternalAggregationKey, group: RuleGroupAccumulator) -> tuple[RuleMetric, ...]:
        maximum = self.parameter(state.step, "maximum_unknown_token_rate", float, required=True)
        assert maximum is not None
        tokens = int(group.numeric("evaluated_token_count").total)
        unknown = int(group.numeric("unknown_token_count").total)
        spelling_rate = unknown / tokens if tokens >= self._minimum_evaluated_tokens else None
        spelling_outcome = MetricOutcome.NOT_EVALUATED if spelling_rate is None else MetricOutcome.PASSED if spelling_rate <= maximum else MetricOutcome.FAILED
        alpha = int(group.numeric("alphabetic_character_count").total)
        non_ascii = int(group.numeric("non_ascii_alphabetic_character_count").total)
        non_ascii_rate = non_ascii / alpha if alpha else None
        return (
            RuleMetric(name="unknown_token_rate", value=self.rounded(spelling_rate, state) if spelling_rate is not None else None, unit="ratio", numerator=unknown, denominator=tokens, threshold=maximum, outcome=spelling_outcome),
            RuleMetric(name="non_ascii_alphabetic_rate", value=self.rounded(non_ascii_rate, state) if non_ascii_rate is not None else None, unit="ratio", numerator=non_ascii, denominator=alpha, outcome=MetricOutcome.NOT_EVALUATED),
            RuleMetric(name="language_classification_method", value="sampled_llm_enrichment_required", unit=None, outcome=MetricOutcome.NOT_EVALUATED),
        )


__all__ = ["NonEnglishSpellingHandler"]
