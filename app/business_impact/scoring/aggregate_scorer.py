"""Calculate the governed overall technical DQ score from rule rollups."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

from app.business_impact.scoring.models import AggregateQualityScore, RuleQualityScore, ScoreAvailability, ScoreBand


class AggregateScoringError(ValueError):
    """Safe error for an invalid aggregate-scoring policy."""


@dataclass(frozen=True, slots=True)
class AggregateScoringConfig:
    """Immutable controls extracted from the governed scoring policy."""

    policy_version: str
    minimum_coverage_ratio: float
    renormalize_available_weights: bool
    rule_weights: Mapping[str, float]
    bands: tuple[tuple[ScoreBand, float], ...]

    @classmethod
    def from_file(cls, path: str | Path) -> "AggregateScoringConfig":
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
            selection = document["selection_policy"]["weight_renormalization"]
            weights = {str(key): float(value["weight"]) for key, value in document["rule_weights"].items()}
            bands = tuple(sorted(((ScoreBand(item["name"]), float(item["minimum_inclusive"])) for item in document["score_bands"]), key=lambda item: -item[1]))
            config = cls(str(document["policy_version"]), float(selection["minimum_coverage_ratio"]), bool(selection["enabled"]), weights, bands)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise AggregateScoringError("Invalid governed scoring policy.") from exc
        if not 0.0 <= config.minimum_coverage_ratio <= 1.0 or not config.rule_weights or abs(sum(config.rule_weights.values()) - 1.0) > 0.000001:
            raise AggregateScoringError("Scoring policy has invalid coverage or rule weights.")
        return config


class AggregateScorer:
    """Produce one DQ-only aggregate score; never a business impact score."""

    def __init__(self, config: AggregateScoringConfig) -> None:
        self._config = config

    def score(self, rule_scores: list[RuleQualityScore] | tuple[RuleQualityScore, ...], assessment_seed: str) -> AggregateQualityScore:
        """Apply documented coverage and weight-renormalization policy."""

        supplied = {item.rule_id: item for item in rule_scores}
        available = [item for item in supplied.values() if item.availability == ScoreAvailability.AVAILABLE and item.score is not None and item.rule_id in self._config.rule_weights]
        coverage = sum(self._config.rule_weights[item.rule_id] for item in available)
        excluded = sorted(set(self._config.rule_weights) - {item.rule_id for item in available})
        if coverage < self._config.minimum_coverage_ratio:
            return self._result(assessment_seed, None, ScoreAvailability.NOT_EVALUATED, [], excluded, coverage, "No aggregate score: approved available-rule coverage is below the configured minimum.")
        denominator = coverage if self._config.renormalize_available_weights else 1.0
        value = sum(float(item.score) * self._config.rule_weights[item.rule_id] / denominator for item in available)
        return self._result(assessment_seed, value, ScoreAvailability.AVAILABLE, sorted(item.rule_score_id for item in available), excluded, coverage, "Aggregate technical DQ score uses only available approved rules and governed weight renormalization.")

    def _result(self, seed: str, score: float | None, availability: ScoreAvailability, contributors: list[str], excluded: list[str], coverage: float, rationale: str) -> AggregateQualityScore:
        band = ScoreBand.NOT_EVALUATED if score is None else next((band for band, minimum in self._config.bands if score >= minimum), ScoreBand.NOT_EVALUATED)
        return AggregateQualityScore(assessment_id="dq-assessment-" + sha256(seed.encode()).hexdigest()[:24], score=score, band=band, availability=availability, contributing_rule_score_ids=contributors, excluded_rule_ids=excluded, coverage_ratio=coverage, minimum_coverage_ratio=self._config.minimum_coverage_ratio, formula_id="weighted_available_rule_score.v1", policy_version=self._config.policy_version, rationale=rationale)
