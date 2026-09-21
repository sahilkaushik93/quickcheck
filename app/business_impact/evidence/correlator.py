"""Aggregate-only evidence co-occurrence and repeated-pattern discovery."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Iterable, Mapping

from app.execution.models import EvidenceCollection, ExecutionOutput
from app.execution.rule_ids import canonical_rule_id


class EvidenceCorrelationError(ValueError):
    """Safe error raised for invalid aggregate evidence correlation requests."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class EvidenceCorrelationConfig:
    """Explicit bounded controls; supplied by a future policy loader."""

    maximum_results: int
    minimum_distinct_periods_for_repeated_pattern: int

    @classmethod
    def from_evidence_policy(
        cls, policy: Mapping[str, object]
    ) -> "EvidenceCorrelationConfig":
        """Derive bounded output capacity from the approved evidence policy.

        A repeated pattern has a semantic minimum of two distinct evaluated
        periods; it is not a business threshold.  Future policy versions may
        expose a stricter explicit correlation section without changing this
        contract.
        """

        try:
            fact_pack = policy["internal_fact_pack"]
            if not isinstance(fact_pack, Mapping):
                raise TypeError("internal_fact_pack must be an object")
            maximum_results = int(fact_pack["maximum_normalized_evidence"])
        except (KeyError, TypeError, ValueError) as exc:
            raise EvidenceCorrelationError(
                "INVALID_EVIDENCE_POLICY",
                "Evidence policy is missing normalized-evidence bounds.",
            ) from exc
        return cls(
            maximum_results=maximum_results,
            minimum_distinct_periods_for_repeated_pattern=2,
        )

    def __post_init__(self) -> None:
        if self.maximum_results < 1:
            raise ValueError("maximum_results must be positive")
        if self.minimum_distinct_periods_for_repeated_pattern < 2:
            raise ValueError(
                "minimum_distinct_periods_for_repeated_pattern must be at least two"
            )


@dataclass(frozen=True, slots=True)
class AggregateEvidenceCorrelation:
    """Non-causal aggregate pattern. No record or fingerprint is exposed."""

    correlation_id: str
    correlation_type: str
    canonical_rule_ids: tuple[str, ...]
    aggregate_scope_id: str
    time_periods: tuple[str, ...]
    observed_failure_counts: Mapping[str, int]
    statement: str
    correlation_not_causation: bool = True

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-safe public representation for a future output model."""

        return {
            "correlation_id": self.correlation_id,
            "correlation_type": self.correlation_type,
            "canonical_rule_ids": list(self.canonical_rule_ids),
            "aggregate_scope_id": self.aggregate_scope_id,
            "time_periods": list(self.time_periods),
            "observed_failure_counts": dict(self.observed_failure_counts),
            "statement": self.statement,
            "correlation_not_causation": True,
        }


class AggregateEvidenceCorrelator:
    """Find only bounded aggregate patterns; never joins entity-level evidence.

    The correlator deliberately ignores row fingerprints and retained evidence
    metadata. It uses only canonical rule IDs, counts and anonymous aggregate
    scope hashes, so it cannot infer an affected person, record, transcript or
    raw source value.
    """

    def __init__(self, config: EvidenceCorrelationConfig) -> None:
        self._config = config

    def correlate(self, execution: ExecutionOutput) -> tuple[AggregateEvidenceCorrelation, ...]:
        """Return deterministic co-occurrence and repeated-period observations."""

        collections = tuple(
            item for item in execution.evidence if item.observed_failure_count > 0
        )
        cooccurrence = self._cooccurrences(collections)
        repeated = self._repeated_period_patterns(collections)
        combined = sorted(
            (*cooccurrence, *repeated),
            key=lambda item: (
                item.correlation_type,
                item.canonical_rule_ids,
                item.aggregate_scope_id,
                item.time_periods,
            ),
        )
        return tuple(combined[: self._config.maximum_results])

    def _cooccurrences(
        self, collections: Iterable[EvidenceCollection]
    ) -> tuple[AggregateEvidenceCorrelation, ...]:
        grouped: dict[str, dict[str, int]] = {}
        for collection in collections:
            scope_id = self._scope_id(collection, include_time=True)
            rule_id = canonical_rule_id(collection.rule_id)
            counts = grouped.setdefault(scope_id, {})
            counts[rule_id] = counts.get(rule_id, 0) + collection.observed_failure_count
        results: list[AggregateEvidenceCorrelation] = []
        for scope_id, counts in sorted(grouped.items()):
            rules = tuple(sorted(counts))
            if len(rules) < 2:
                continue
            correlation_id = self._correlation_id("aggregate_cooccurrence", scope_id, rules)
            results.append(
                AggregateEvidenceCorrelation(
                    correlation_id=correlation_id,
                    correlation_type="aggregate_cooccurrence",
                    canonical_rule_ids=rules,
                    aggregate_scope_id=scope_id,
                    time_periods=(),
                    observed_failure_counts=dict(sorted(counts.items())),
                    statement=(
                        "Aggregate evidence observations for the listed rules co-occurred "
                        "within one approved aggregate scope; this is not evidence of causality."
                    ),
                )
            )
        return tuple(results)

    def _repeated_period_patterns(
        self, collections: Iterable[EvidenceCollection]
    ) -> tuple[AggregateEvidenceCorrelation, ...]:
        by_rule_scope: dict[tuple[str, str], dict[str, int]] = {}
        for collection in collections:
            period = collection.aggregation_key.time_period
            if not period:
                continue
            rule_id = canonical_rule_id(collection.rule_id)
            scope_id = self._scope_id(collection, include_time=False)
            key = (rule_id, scope_id)
            periods = by_rule_scope.setdefault(key, {})
            periods[period] = periods.get(period, 0) + collection.observed_failure_count
        results: list[AggregateEvidenceCorrelation] = []
        for (rule_id, scope_id), counts in sorted(by_rule_scope.items()):
            periods = tuple(sorted(counts))
            if len(periods) < self._config.minimum_distinct_periods_for_repeated_pattern:
                continue
            correlation_id = self._correlation_id("repeated_period_pattern", scope_id, (rule_id, *periods))
            results.append(
                AggregateEvidenceCorrelation(
                    correlation_id=correlation_id,
                    correlation_type="repeated_period_pattern",
                    canonical_rule_ids=(rule_id,),
                    aggregate_scope_id=scope_id,
                    time_periods=periods,
                    observed_failure_counts={rule_id: sum(counts.values())},
                    statement=(
                        "Aggregate evidence observations for this rule were present across "
                        "multiple ordered evaluated periods; this repeated pattern is not a causal conclusion."
                    ),
                )
            )
        return tuple(results)

    @staticmethod
    def _scope_id(collection: EvidenceCollection, *, include_time: bool) -> str:
        key = collection.aggregation_key
        payload = {
            "overall": key.overall,
            "dimensions": key.dimensions,
            "time_period": key.time_period if include_time else None,
        }
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return "scope-" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _correlation_id(kind: str, scope_id: str, values: tuple[str, ...]) -> str:
        payload = "|".join((kind, scope_id, *values))
        return "corr-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


__all__ = [
    "AggregateEvidenceCorrelation",
    "AggregateEvidenceCorrelator",
    "EvidenceCorrelationConfig",
    "EvidenceCorrelationError",
]
