"""Create rule-level score rollups from governed normalized metric scores."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Iterable

from app.business_impact.scoring.metric_normalizer import MetricNormalizationResult
from app.business_impact.scoring.models import (
    NormalizedMetricScore,
    RawMetricObservation,
    RuleQualityScore,
    ScoreAvailability,
    ScoreBand,
)
from app.execution.models import AggregationKey


@dataclass(frozen=True, slots=True)
class GroupedRuleScore:
    """A group-specific score retained outside the current public rollup model.

    `RuleQualityScore` has no aggregation-key field.  This companion contract
    preserves group output for future API-model extension while the rule rollup
    remains suitable for the overall DQ assessment.
    """

    rule_id: str
    rule_version: str
    aggregation_key: AggregationKey
    metric_score_ids: tuple[str, ...]
    score: float | None
    band: ScoreBand
    availability: ScoreAvailability
    records_evaluated: int
    records_affected: int


@dataclass(frozen=True, slots=True)
class RuleScoringResult:
    """Rule rollups plus non-overlapping group details and explicit warnings."""

    rule_scores: tuple[RuleQualityScore, ...]
    grouped_scores: tuple[GroupedRuleScore, ...]
    warnings: tuple[str, ...]


class RuleScorer:
    """Build deterministic rule score rollups without cross-population mixing."""

    def score(self, normalized: MetricNormalizationResult) -> RuleScoringResult:
        """Roll up scores by rule; prefer overall scores over grouped scores.

        A rule that has an overall population never incorporates any grouped
        population.  A rule with only grouped metrics gets a denominator-weighted
        rollup for overall technical assessment, while group details are retained
        separately for presentation.
        """

        observations = {item.observation_id: item for item in normalized.observations}
        by_rule: dict[str, list[NormalizedMetricScore]] = {}
        for item in normalized.normalized_scores:
            by_rule.setdefault(item.rule_id, []).append(item)

        rollups: list[RuleQualityScore] = []
        grouped: list[GroupedRuleScore] = []
        warnings: list[str] = []
        for rule_id in sorted(by_rule):
            scores = sorted(by_rule[rule_id], key=lambda item: item.score_id)
            overall = [item for item in scores if item.aggregation_key is not None and item.aggregation_key.overall]
            chosen = overall or scores
            if overall and len(scores) != len(overall):
                warnings.append(f"Grouped metric scores for {rule_id} were excluded from its overall rollup.")
            if not overall:
                grouped.extend(self._group_scores(chosen, observations))
            rollups.append(self._rollup(rule_id, chosen, observations, used_grouped_population=not bool(overall)))
        return RuleScoringResult(tuple(rollups), tuple(grouped), tuple(warnings))

    def _rollup(
        self,
        rule_id: str,
        scores: list[NormalizedMetricScore],
        observations: dict[str, RawMetricObservation],
        *,
        used_grouped_population: bool,
    ) -> RuleQualityScore:
        available = [item for item in scores if item.availability == ScoreAvailability.AVAILABLE and item.score is not None]
        availability = ScoreAvailability.AVAILABLE if available else _worst_availability(item.availability for item in scores)
        rule_version = observations[scores[0].observation_id].rule_version
        evaluated, affected = _counts(scores, observations)
        base_weight = next((item.weight for item in scores if item.weight is not None), None)
        formula = scores[0].formula_id if len(available) == 1 else "population_weighted_metric_rollup.v1"
        if not available:
            return RuleQualityScore(
                rule_score_id=_identifier("rule-score", rule_id, rule_version), rule_id=rule_id,
                rule_version=rule_version, score=None, band=ScoreBand.NOT_EVALUATED,
                availability=availability, metric_score_ids=[item.score_id for item in scores],
                effective_weight=base_weight, formula_id=formula, policy_version=scores[0].policy_version,
                records_evaluated=evaluated, records_affected=affected,
                rationale="No approved available primary metric was supplied for this rule.",
            )
        value = _weighted_average(available, observations)
        return RuleQualityScore(
            rule_score_id=_identifier("rule-score", rule_id, rule_version), rule_id=rule_id,
            rule_version=rule_version, score=value, band=_band_from_scores(available, value),
            availability=ScoreAvailability.AVAILABLE, metric_score_ids=[item.score_id for item in available],
            effective_weight=base_weight, formula_id=formula, policy_version=available[0].policy_version,
            records_evaluated=evaluated, records_affected=affected,
            rationale=(
                "Rule score uses its approved overall primary metric."
                if not used_grouped_population else
                "Rule score uses only non-overlapping grouped primary metrics; no overall population was available."
            ),
        )

    def _group_scores(self, scores: list[NormalizedMetricScore], observations: dict[str, RawMetricObservation]) -> list[GroupedRuleScore]:
        results: list[GroupedRuleScore] = []
        for item in scores:
            if item.aggregation_key is None or item.aggregation_key.overall:
                continue
            observation = observations[item.observation_id]
            results.append(GroupedRuleScore(
                rule_id=item.rule_id, rule_version=observation.rule_version,
                aggregation_key=item.aggregation_key, metric_score_ids=(item.score_id,),
                score=item.score, band=item.band, availability=item.availability,
                records_evaluated=_nonnegative_count(observation.denominator),
                records_affected=_nonnegative_count(observation.numerator),
            ))
        return results


def _weighted_average(scores: list[NormalizedMetricScore], observations: dict[str, RawMetricObservation]) -> float:
    weights = [float(observations[item.observation_id].denominator or 1.0) for item in scores]
    total = sum(weights)
    return sum(float(item.score) * weight for item, weight in zip(scores, weights)) / total


def _counts(scores: Iterable[NormalizedMetricScore], observations: dict[str, RawMetricObservation]) -> tuple[int, int]:
    selected = list(scores)
    return (
        sum(_nonnegative_count(observations[item.observation_id].denominator) for item in selected),
        sum(_nonnegative_count(observations[item.observation_id].numerator) for item in selected),
    )


def _nonnegative_count(value: int | float | None) -> int:
    return int(value) if value is not None and value >= 0 else 0


def _worst_availability(values: Iterable[ScoreAvailability]) -> ScoreAvailability:
    order = {ScoreAvailability.NOT_EVALUATED: 0, ScoreAvailability.UNAVAILABLE: 1, ScoreAvailability.PARTIAL: 2, ScoreAvailability.AVAILABLE: 3}
    return min(values, key=lambda item: order[item])


def _band_from_scores(scores: list[NormalizedMetricScore], value: float) -> ScoreBand:
    ordered = sorted((item for item in scores if item.score is not None), key=lambda item: float(item.score))
    return ordered[0].band if ordered and value == float(ordered[0].score) else _band_for_value(value)


def _band_for_value(value: float) -> ScoreBand:
    if value >= 90: return ScoreBand.EXCELLENT
    if value >= 75: return ScoreBand.ACCEPTABLE
    if value >= 60: return ScoreBand.WATCH
    if value >= 40: return ScoreBand.POOR
    return ScoreBand.CRITICAL


def _identifier(prefix: str, *parts: str) -> str:
    return f"{prefix}-" + sha256("\x1f".join(parts).encode()).hexdigest()[:24]
