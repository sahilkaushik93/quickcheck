"""Bounded topic-frequency and concentration handler."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Mapping

from app.execution.aggregation.accumulators import InternalAggregationKey, RuleGroupAccumulator
from app.execution.features.models import FeatureRequirements, RowFeatures
from app.execution.handlers.base import HandlerConfigurationError, HandlerState, RuleHandler
from app.execution.models import GroupedRuleMetrics, MetricOutcome, RuleExecutionResult, RuleMetric
from app.understanding.models import JsonValue, RequiredCheck, RuleExecutionStep


class TopicDistributionHandler(RuleHandler):
    """Calculate a capped topic distribution and dominant-topic share."""

    check_name = RequiredCheck.TOPIC_DISTRIBUTION.value
    feature_requirements = FeatureRequirements()
    _FREQUENCY_NAME = "topic_frequency"

    def __init__(self, ontology: Mapping[str, Any] | None = None) -> None:
        if ontology is None:
            path = Path(__file__).resolve().parents[3] / "config/execution/transcript_dq_ontology.json"
            ontology = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        rules: list[tuple[int, str, tuple[re.Pattern[str], ...]]] = []
        for item in ontology.get("topic_categories", []):
            rules.append((
                int(item.get("priority", 1000)),
                str(item["name"]),
                tuple(re.compile(str(pattern), re.I) for pattern in item.get("patterns", [])),
            ))
        self._ontology = tuple(sorted(rules, key=lambda item: (item[0], item[1].casefold())))
        self._fallback = str(ontology.get("fallback_topic_category", "Other"))
        self._topic_aliases = tuple(
            str(value).casefold()
            for value in ontology.get("semantic_roles", {}).get("topic", [])
        )

    def validate_parameters(
        self, parameters: Mapping[str, JsonValue], step: RuleExecutionStep
    ) -> None:
        super().validate_parameters(parameters, step)
        maximum = self.parameter(step, "dominant_topic_maximum", float, required=True)
        minimum = self.parameter(step, "minimum_distinct_topics", int, required=True)
        if maximum is None or not 0.0 <= maximum <= 1.0:
            raise HandlerConfigurationError(
                "INVALID_TOPIC_CONCENTRATION_THRESHOLD",
                "dominant_topic_maximum must be between 0 and 1.",
                rule_id=step.rule_id,
            )
        if minimum is None or minimum < 1:
            raise HandlerConfigurationError(
                "INVALID_MINIMUM_TOPIC_COUNT",
                "minimum_distinct_topics must be positive.",
                rule_id=step.rule_id,
            )
        if not step.target_columns:
            raise HandlerConfigurationError(
                "TOPIC_TARGET_REQUIRED",
                "Topic distribution requires a resolved topic column.",
                rule_id=step.rule_id,
            )

    def observe_group(self, state: HandlerState, row: RowFeatures, key: InternalAggregationKey, group: RuleGroupAccumulator) -> None:
        configured = state.step.parameters.get("topic_column")
        column = str(configured) if configured else self._preferred_topic_column(state.step.target_columns)
        value = row.value(column)
        raw = str(value).strip() if value is not None else ""
        if not raw:
            group.counts.observe(eligible=False)
            return
        frequency = group.frequency(
            self._FREQUENCY_NAME,
            capacity=state.limits.maximum_categories_per_group,
        )
        delimiters = str(state.step.parameters.get("topic_delimiters") or "|;,>")
        topics = [part.strip() for part in re.split(f"[{re.escape(delimiters)}]", raw) if part.strip()]
        for topic in topics or [raw]:
            frequency.observe(self._category(topic))
        group.counts.observe(eligible=True, passed=None)

    def _preferred_topic_column(self, targets: list[str]) -> str:
        by_name = {value.casefold(): value for value in targets}
        for alias in self._topic_aliases:
            if alias in by_name:
                return by_name[alias]
        return targets[0]

    def metrics_for_group(self, state: HandlerState, key: InternalAggregationKey, group: RuleGroupAccumulator) -> tuple[RuleMetric, ...]:
        maximum = self.parameter(state.step, "dominant_topic_maximum", float, required=True)
        minimum = self.parameter(state.step, "minimum_distinct_topics", int, required=True)
        assert maximum is not None and minimum is not None
        frequency = group.frequency(
            self._FREQUENCY_NAME,
            capacity=state.limits.maximum_categories_per_group,
        )
        total = group.counts.eligible_count
        ranked = frequency.top()
        dominant_count = ranked[0][1] if ranked else 0
        dominant_share = dominant_count / total if total else 0.0
        distinct_lower_bound = len(frequency.counts) + int(frequency.truncated)
        passed = dominant_share <= maximum and distinct_lower_bound >= minimum
        outcome = MetricOutcome.PASSED if passed else MetricOutcome.FAILED
        distribution = {str(topic): count for topic, count in ranked}
        if frequency.overflow_count:
            distribution["__OTHER__"] = frequency.overflow_count
        return (
            RuleMetric(name="dominant_topic_share", value=self.rounded(dominant_share, state), unit="ratio", numerator=dominant_count, denominator=total, threshold=maximum, outcome=outcome),
            RuleMetric(name="distinct_topic_count_lower_bound", value=distinct_lower_bound, unit="count", threshold=minimum, outcome=outcome),
            RuleMetric(name="topic_distribution", value=distribution, unit="count", outcome=MetricOutcome.NOT_EVALUATED),
        )

    def _category(self, value: str) -> str:
        for _, name, patterns in self._ontology:
            if any(pattern.search(value) for pattern in patterns):
                return name
        return value if not self._ontology else self._fallback

    def finalize(self, state: HandlerState, *, elapsed_seconds: float) -> RuleExecutionResult:
        """Add consecutive-period drift statistics after bounded frequencies finalize."""

        result = super().finalize(state, elapsed_seconds=elapsed_seconds)
        timed = sorted(
            (group for group in result.groups if not group.aggregation_key.overall and group.aggregation_key.time_period),
            key=lambda group: (
                tuple(sorted(group.aggregation_key.dimensions.items(), key=lambda item: item[0].casefold())),
                group.aggregation_key.time_period or "",
            ),
        )
        previous_by_dimensions: dict[tuple[tuple[str, object], ...], GroupedRuleMetrics] = {}
        replacements: dict[tuple[str | None, tuple[tuple[str, object], ...]], GroupedRuleMetrics] = {}
        for group in timed:
            dimensions = tuple(
                sorted(group.aggregation_key.dimensions.items(), key=lambda item: item[0].casefold())
            )
            previous = previous_by_dimensions.get(dimensions)
            previous_by_dimensions[dimensions] = group
            if previous is None:
                continue
            current_counts = self._distribution(group)
            previous_counts = self._distribution(previous)
            jsd, psi, maximum_h = self._drift(previous_counts, current_counts)
            outcome = MetricOutcome.PASSED if jsd < 0.05 else MetricOutcome.WARNING if jsd < 0.15 else MetricOutcome.FAILED
            additions = [
                RuleMetric(name="jensen_shannon_divergence", value=self.rounded(jsd, state), unit="divergence", threshold={"stable_below": 0.05, "significant_at_or_above": 0.15}, outcome=outcome),
                RuleMetric(name="population_stability_index", value=self.rounded(psi, state), unit="index", threshold={"low_below": 0.10, "high_at_or_above": 0.25}, outcome=MetricOutcome.NOT_EVALUATED),
                RuleMetric(name="maximum_cohen_h", value=self.rounded(maximum_h, state), unit="effect_size", threshold={"small_below": 0.20, "medium_below": 0.50, "large_below": 0.80}, outcome=MetricOutcome.NOT_EVALUATED),
                RuleMetric(name="semantic_centroid_drift", value=None, unit="cosine_distance", outcome=MetricOutcome.NOT_EVALUATED),
            ]
            key = (group.aggregation_key.time_period, dimensions)
            replacements[key] = group.model_copy(update={"metrics": [*group.metrics, *additions]})
        updated = []
        for group in result.groups:
            dimensions = tuple(
                sorted(group.aggregation_key.dimensions.items(), key=lambda item: item[0].casefold())
            )
            updated.append(replacements.get((group.aggregation_key.time_period, dimensions), group))
        return result.model_copy(update={"groups": updated})

    @staticmethod
    def _distribution(group: GroupedRuleMetrics) -> dict[str, int]:
        metric = next((item for item in group.metrics if item.name == "topic_distribution"), None)
        if metric is None or not isinstance(metric.value, dict):
            return {}
        return {str(key): int(value) for key, value in metric.value.items() if isinstance(value, (int, float))}

    @staticmethod
    def _drift(previous: Mapping[str, int], current: Mapping[str, int]) -> tuple[float, float, float]:
        categories = sorted(set(previous) | set(current))
        if not categories:
            return 0.0, 0.0, 0.0
        epsilon = 1e-8
        previous_total = sum(previous.values()) or 1
        current_total = sum(current.values()) or 1
        p = [(previous.get(item, 0) + epsilon) / (previous_total + epsilon * len(categories)) for item in categories]
        q = [(current.get(item, 0) + epsilon) / (current_total + epsilon * len(categories)) for item in categories]
        midpoint = [(left + right) / 2.0 for left, right in zip(p, q)]
        jsd = 0.5 * sum(left * math.log(left / middle) for left, middle in zip(p, midpoint)) + 0.5 * sum(right * math.log(right / middle) for right, middle in zip(q, midpoint))
        psi = sum((right - left) * math.log(right / left) for left, right in zip(p, q))
        maximum_h = max(abs(2 * math.asin(math.sqrt(right)) - 2 * math.asin(math.sqrt(left))) for left, right in zip(p, q))
        return jsd, psi, maximum_h


__all__ = ["TopicDistributionHandler"]
