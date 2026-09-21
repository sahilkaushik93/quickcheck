"""Normalize approved Execution metrics to a deterministic 0--100 DQ scale.

This module is deliberately limited to execution output and governed JSON
policy.  It never reads source data, invokes an LLM, or infers an unapproved
metric/threshold.  It also keeps technical DQ scoring distinct from any future
business-impact assessment.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from app.business_impact.scoring.models import (
    NormalizedMetricScore,
    RawMetricObservation,
    ScoreAvailability,
    ScoreBand,
)
from app.execution.models import (
    ExecutionOutput,
    GroupedRuleMetrics,
    MetricAvailabilityStatus,
    MetricOutcome,
    RuleExecutionResult,
    RuleMetric,
)
from app.execution.rule_ids import canonical_rule_id, normalize_rule_id


class MetricNormalizationError(ValueError):
    """A safe, structured error raised for invalid governed scoring policy."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class MetricScoringSkip:
    """An explicit explanation for a result that was not made into a score."""

    rule_id: str
    metric_name: str | None
    aggregation_scope: str | None
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class MetricNormalizationResult:
    """Safe scoring intermediate consumed by rule and aggregate scorers."""

    observations: tuple[RawMetricObservation, ...]
    normalized_scores: tuple[NormalizedMetricScore, ...]
    skipped: tuple[MetricScoringSkip, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MetricNormalizationConfig:
    """Validated immutable scoring and metric-catalog policy."""

    policy_version: str
    catalog_version: str
    score_minimum: float
    score_maximum: float
    score_bands: tuple[tuple[ScoreBand, float], ...]
    formula_ids: frozenset[str]
    rule_weights: Mapping[str, float]
    rules: Mapping[str, Mapping[str, Any]]
    allow_partial_metric_scoring: bool

    @classmethod
    def from_files(
        cls,
        scoring_policy_path: str | Path,
        metric_catalog_path: str | Path,
    ) -> "MetricNormalizationConfig":
        """Load and validate the two required JSON files without global state."""

        scoring_policy = _load_json_object(scoring_policy_path, "SCORING_POLICY_READ_FAILED")
        catalog = _load_json_object(metric_catalog_path, "METRIC_CATALOG_READ_FAILED")
        return cls.from_mappings(scoring_policy, catalog)

    @classmethod
    def from_mappings(
        cls,
        scoring_policy: Mapping[str, Any],
        metric_catalog: Mapping[str, Any],
    ) -> "MetricNormalizationConfig":
        """Validate deserialized policy mappings for deterministic scoring."""

        policy_version = _required_text(scoring_policy, "policy_version", "SCORING_POLICY_INVALID")
        catalog_version = _required_text(metric_catalog, "catalog_version", "METRIC_CATALOG_INVALID")
        score_range = _required_mapping(scoring_policy, "score_range", "SCORING_POLICY_INVALID")
        score_minimum = _finite_number(score_range.get("minimum"), "score_range.minimum")
        score_maximum = _finite_number(score_range.get("maximum"), "score_range.maximum")
        if score_minimum >= score_maximum:
            raise MetricNormalizationError("SCORING_POLICY_INVALID", "score range must have minimum below maximum")

        formulas = _required_mapping(scoring_policy, "normalization_formulas", "SCORING_POLICY_INVALID")
        formula_ids = frozenset(
            _required_text(item, "formula_id", "SCORING_POLICY_INVALID")
            for item in formulas.values()
            if isinstance(item, Mapping)
        )
        if not formula_ids:
            raise MetricNormalizationError("SCORING_POLICY_INVALID", "at least one normalization formula is required")

        bands_raw = scoring_policy.get("score_bands")
        if not isinstance(bands_raw, list) or not bands_raw:
            raise MetricNormalizationError("SCORING_POLICY_INVALID", "score_bands must be a non-empty list")
        bands: list[tuple[ScoreBand, float]] = []
        seen_bands: set[ScoreBand] = set()
        for item in bands_raw:
            if not isinstance(item, Mapping):
                raise MetricNormalizationError("SCORING_POLICY_INVALID", "score band must be an object")
            try:
                band = ScoreBand(_required_text(item, "name", "SCORING_POLICY_INVALID"))
            except ValueError as exc:
                raise MetricNormalizationError("SCORING_POLICY_INVALID", "unknown score band") from exc
            minimum = _finite_number(item.get("minimum_inclusive"), "score band minimum")
            if band in seen_bands or minimum < score_minimum or minimum > score_maximum:
                raise MetricNormalizationError("SCORING_POLICY_INVALID", "invalid score band boundaries")
            seen_bands.add(band)
            bands.append((band, minimum))
        bands.sort(key=lambda item: (-item[1], item[0].value))
        if bands[-1][1] != score_minimum:
            raise MetricNormalizationError("SCORING_POLICY_INVALID", "score bands must cover the configured minimum")

        weights_raw = _required_mapping(scoring_policy, "rule_weights", "SCORING_POLICY_INVALID")
        weights: dict[str, float] = {}
        for rule_id, item in weights_raw.items():
            if not isinstance(rule_id, str) or not isinstance(item, Mapping):
                raise MetricNormalizationError("SCORING_POLICY_INVALID", "rule weight entries must be objects")
            canonical = _canonical(rule_id, "SCORING_POLICY_INVALID")
            weight = _finite_number(item.get("weight"), f"rule weight for {rule_id}")
            if weight < 0.0 or weight > 1.0 or canonical in weights:
                raise MetricNormalizationError("SCORING_POLICY_INVALID", "invalid duplicate or out-of-range rule weight")
            weights[canonical] = weight

        validation = _required_mapping(scoring_policy, "validation", "SCORING_POLICY_INVALID")
        required_weight_sum = _finite_number(
            validation.get("require_exact_weight_sum"), "validation.require_exact_weight_sum"
        )
        tolerance = _finite_number(validation.get("weight_sum_tolerance"), "validation.weight_sum_tolerance")
        if abs(sum(weights.values()) - required_weight_sum) > tolerance:
            raise MetricNormalizationError("SCORING_POLICY_INVALID", "configured rule weights do not meet the required total")

        catalog_rules = _required_mapping(metric_catalog, "rules", "METRIC_CATALOG_INVALID")
        validated_rules: dict[str, Mapping[str, Any]] = {}
        for rule_id, item in catalog_rules.items():
            if not isinstance(rule_id, str) or not isinstance(item, Mapping):
                raise MetricNormalizationError("METRIC_CATALOG_INVALID", "catalog rule entries must be objects")
            canonical = _canonical(rule_id, "METRIC_CATALOG_INVALID")
            primary = _required_mapping(item, "primary_metric", "METRIC_CATALOG_INVALID")
            formula_id = _required_text(primary, "formula_id", "METRIC_CATALOG_INVALID")
            if formula_id not in formula_ids:
                raise MetricNormalizationError("METRIC_CATALOG_INVALID", f"unknown formula {formula_id!r}")
            _required_text(primary, "name", "METRIC_CATALOG_INVALID")
            scope = _required_text(primary, "aggregation_scope", "METRIC_CATALOG_INVALID")
            if scope not in {"overall_preferred", "grouped_required"}:
                raise MetricNormalizationError("METRIC_CATALOG_INVALID", "unsupported aggregation scope")
            parameters = _required_mapping(primary, "parameters", "METRIC_CATALOG_INVALID")
            _validate_formula_parameters(formula_id, parameters)
            runtime_ids = item.get("runtime_rule_ids")
            if not isinstance(runtime_ids, list) or not runtime_ids:
                raise MetricNormalizationError("METRIC_CATALOG_INVALID", "runtime_rule_ids must be non-empty")
            for runtime_id in runtime_ids:
                if not isinstance(runtime_id, str) or _canonical(runtime_id, "METRIC_CATALOG_INVALID") != canonical:
                    raise MetricNormalizationError("METRIC_CATALOG_INVALID", "runtime rule id does not map to catalog rule")
            if canonical not in weights:
                raise MetricNormalizationError("SCORING_POLICY_INVALID", f"missing explicit weight for {canonical}")
            validated_rules[canonical] = MappingProxyType(dict(item))

        if set(weights) != set(validated_rules):
            raise MetricNormalizationError("SCORING_POLICY_INVALID", "weight and primary-metric catalog rule IDs must match")
        partial = bool(_required_mapping(scoring_policy, "selection_policy", "SCORING_POLICY_INVALID").get("allow_partial_metric_scoring", False))
        return cls(
            policy_version=policy_version,
            catalog_version=catalog_version,
            score_minimum=score_minimum,
            score_maximum=score_maximum,
            score_bands=tuple(bands),
            formula_ids=formula_ids,
            rule_weights=MappingProxyType(weights),
            rules=MappingProxyType(validated_rules),
            allow_partial_metric_scoring=partial,
        )


class MetricNormalizer:
    """Select catalog metrics from `ExecutionOutput` and score only valid ones."""

    def __init__(self, config: MetricNormalizationConfig) -> None:
        self._config = config

    def normalize(self, execution: ExecutionOutput) -> MetricNormalizationResult:
        """Produce observations and scores without combining overlapping populations."""

        availability = {
            _canonical(item.rule_id, "EXECUTION_RULE_ID_INVALID"): item
            for item in execution.metric_availability
            if _is_supported_rule_id(item.rule_id)
        }
        observations: list[RawMetricObservation] = []
        scores: list[NormalizedMetricScore] = []
        skips: list[MetricScoringSkip] = []

        for result in sorted(execution.rule_results, key=lambda item: (self._safe_canonical(item.rule_id), item.step_id)):
            canonical = self._safe_canonical(result.rule_id)
            catalog_rule = self._config.rules.get(canonical)
            if catalog_rule is None:
                skips.append(MetricScoringSkip(canonical, None, None, "RULE_NOT_IN_CATALOG", "Rule has no approved primary metric and was not scored."))
                continue
            primary = _required_mapping(catalog_rule, "primary_metric", "METRIC_CATALOG_INVALID")
            selected = self._select_metrics(result, primary)
            if not selected:
                skips.append(MetricScoringSkip(canonical, str(primary["name"]), str(primary["aggregation_scope"]), "PRIMARY_METRIC_UNAVAILABLE", "No eligible approved primary metric was returned by Execution."))
                continue
            rule_availability = availability.get(canonical)
            for group, metric in selected:
                observation = self._observation(execution, result, canonical, group, metric, rule_availability)
                observations.append(observation)
                score = self._score_observation(observation, primary)
                scores.append(score)
                if score.score is None:
                    skips.append(MetricScoringSkip(canonical, observation.metric_name, str(primary["aggregation_scope"]), "METRIC_NOT_SCOREABLE", score.rationale))

        return MetricNormalizationResult(
            observations=tuple(observations),
            normalized_scores=tuple(scores),
            skipped=tuple(skips),
            warnings=tuple(),
        )

    def _select_metrics(self, result: RuleExecutionResult, primary: Mapping[str, Any]) -> list[tuple[GroupedRuleMetrics, RuleMetric]]:
        name = str(primary["name"]).casefold()
        candidates = [(group, metric) for group in result.groups for metric in group.metrics if metric.name.casefold() == name]
        scope = str(primary["aggregation_scope"])
        if scope == "grouped_required":
            return [(group, metric) for group, metric in candidates if not group.aggregation_key.overall]
        overall = [(group, metric) for group, metric in candidates if group.aggregation_key.overall]
        if any(_metric_has_value(metric) for _, metric in overall):
            return overall
        grouped = [(group, metric) for group, metric in candidates if not group.aggregation_key.overall]
        return grouped or overall

    def _observation(self, execution: ExecutionOutput, result: RuleExecutionResult, canonical: str, group: GroupedRuleMetrics, metric: RuleMetric, rule_availability: Any) -> RawMetricObservation:
        availability = _availability_for(metric, rule_availability, self._config.allow_partial_metric_scoring)
        period = group.aggregation_key.time_period
        dimensions = dict(group.aggregation_key.dimensions)
        identity = _stable_id(execution.run_id, result.step_id, metric.name, period or "overall", json.dumps(dimensions, sort_keys=True, default=str))
        return RawMetricObservation(
            observation_id=f"metric-{identity}",
            rule_id=canonical,
            rule_version=result.rule_version,
            metric_name=metric.name,
            value=metric.value,
            unit=metric.unit,
            numerator=metric.numerator,
            denominator=metric.denominator,
            threshold=metric.threshold,
            outcome=metric.outcome,
            aggregation_key=group.aggregation_key,
            period=period,
            dimensions=dimensions,
            availability=availability,
            source_fact_id=f"fact-metric-{identity}",
        )

    def _score_observation(self, observation: RawMetricObservation, primary: Mapping[str, Any]) -> NormalizedMetricScore:
        formula_id = str(primary["formula_id"])
        score: float | None = None
        rationale: str
        if observation.availability != ScoreAvailability.AVAILABLE:
            rationale = f"No score: execution metric availability is {observation.availability.value}."
        elif not _is_numeric(observation.value):
            rationale = "No score: primary metric is null, non-numeric, or non-finite."
        elif observation.outcome == MetricOutcome.NOT_EVALUATED:
            rationale = "No score: Execution marked the primary metric not_evaluated."
        else:
            score = _calculate_score(formula_id, float(observation.value), _required_mapping(primary, "parameters", "METRIC_CATALOG_INVALID"), self._config)
            rationale = f"Scored with approved formula {formula_id} using the configured primary metric."
        band = _score_band(score, self._config) if score is not None else ScoreBand.NOT_EVALUATED
        return NormalizedMetricScore(
            score_id=f"score-{_stable_id(observation.observation_id, formula_id, self._config.policy_version)}",
            observation_id=observation.observation_id,
            rule_id=observation.rule_id,
            metric_name=observation.metric_name,
            score=score,
            band=band,
            availability=observation.availability,
            formula_id=formula_id,
            policy_version=self._config.policy_version,
            weight=self._config.rule_weights[observation.rule_id],
            rationale=rationale,
            aggregation_key=observation.aggregation_key,
        )

    @staticmethod
    def _safe_canonical(rule_id: str) -> str:
        return canonical_rule_id(rule_id) if _is_supported_rule_id(rule_id) else rule_id.strip().casefold()


def _load_json_object(path: str | Path, code: str) -> Mapping[str, Any]:
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise MetricNormalizationError(code, "Could not read a required governed JSON policy.") from exc
    if not isinstance(value, Mapping):
        raise MetricNormalizationError(code, "Governed JSON policy must contain an object.")
    return value


def _required_mapping(value: Mapping[str, Any], key: str, code: str) -> Mapping[str, Any]:
    result = value.get(key)
    if not isinstance(result, Mapping):
        raise MetricNormalizationError(code, f"{key} must be an object")
    return result


def _required_text(value: Mapping[str, Any], key: str, code: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result.strip():
        raise MetricNormalizationError(code, f"{key} must be a non-empty string")
    return result.strip()


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(float(value)):
        raise MetricNormalizationError("SCORING_POLICY_INVALID", f"{label} must be a finite number")
    return float(value)


def _canonical(rule_id: str, code: str) -> str:
    try:
        return canonical_rule_id(rule_id)
    except ValueError as exc:
        raise MetricNormalizationError(code, f"Unknown canonical rule ID: {rule_id!r}") from exc


def _is_supported_rule_id(rule_id: str) -> bool:
    try:
        normalize_rule_id(rule_id)
    except ValueError:
        return False
    return True


def _validate_formula_parameters(formula_id: str, parameters: Mapping[str, Any]) -> None:
    required = {
        "higher_is_better_linear.v1": ("fail_at_or_below", "pass_at_or_above"),
        "lower_is_better_linear.v1": ("pass_at_or_below", "fail_at_or_above"),
        "inside_range_linear.v1": ("warning_minimum", "pass_minimum", "pass_maximum", "warning_maximum"),
    }.get(formula_id)
    if required is None:
        raise MetricNormalizationError("METRIC_CATALOG_INVALID", "normalizer does not implement configured formula")
    values = [_finite_number(parameters.get(item), f"formula parameter {item}") for item in required]
    if any(left >= right for left, right in zip(values, values[1:])):
        raise MetricNormalizationError("METRIC_CATALOG_INVALID", "formula parameters must be strictly increasing")


def _availability_for(metric: RuleMetric, availability: Any, allow_partial: bool) -> ScoreAvailability:
    if metric.outcome == MetricOutcome.NOT_EVALUATED or not _is_numeric(metric.value):
        return ScoreAvailability.NOT_EVALUATED
    if availability is None:
        return ScoreAvailability.UNAVAILABLE
    if availability.status == MetricAvailabilityStatus.AVAILABLE:
        return ScoreAvailability.AVAILABLE
    if availability.status == MetricAvailabilityStatus.PARTIAL and allow_partial:
        return ScoreAvailability.AVAILABLE
    if availability.status == MetricAvailabilityStatus.PARTIAL:
        return ScoreAvailability.PARTIAL
    return ScoreAvailability.UNAVAILABLE


def _metric_has_value(metric: RuleMetric) -> bool:
    return metric.outcome != MetricOutcome.NOT_EVALUATED and _is_numeric(metric.value)


def _is_numeric(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and isfinite(float(value))


def _calculate_score(formula_id: str, value: float, parameters: Mapping[str, Any], config: MetricNormalizationConfig) -> float:
    if formula_id == "higher_is_better_linear.v1":
        lower = float(parameters["fail_at_or_below"])
        upper = float(parameters["pass_at_or_above"])
        raw = 0.0 if value <= lower else 100.0 if value >= upper else 100.0 * (value - lower) / (upper - lower)
    elif formula_id == "lower_is_better_linear.v1":
        lower = float(parameters["pass_at_or_below"])
        upper = float(parameters["fail_at_or_above"])
        raw = 100.0 if value <= lower else 0.0 if value >= upper else 100.0 * (upper - value) / (upper - lower)
    elif formula_id == "inside_range_linear.v1":
        warning_low = float(parameters["warning_minimum"])
        pass_low = float(parameters["pass_minimum"])
        pass_high = float(parameters["pass_maximum"])
        warning_high = float(parameters["warning_maximum"])
        if value < warning_low or value > warning_high:
            raw = 0.0
        elif pass_low <= value <= pass_high:
            raw = 100.0
        elif value < pass_low:
            raw = 100.0 * (value - warning_low) / (pass_low - warning_low)
        else:
            raw = 100.0 * (warning_high - value) / (warning_high - pass_high)
    else:
        raise MetricNormalizationError("FORMULA_UNSUPPORTED", f"Unsupported approved formula {formula_id!r}")
    return min(config.score_maximum, max(config.score_minimum, raw))


def _score_band(score: float, config: MetricNormalizationConfig) -> ScoreBand:
    for band, minimum in config.score_bands:
        if score >= minimum:
            return band
    return ScoreBand.NOT_EVALUATED


def _stable_id(*parts: str) -> str:
    return sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:32]
