"""Mergeable bounded accumulators for deterministic grouped execution.

All state is proportional to configured rule/group/category limits, never to
source row count.  Accumulators accept compact observations only and must not
retain DataFrames, source rows, transcripts, identifiers, or raw PII values.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Hashable, Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.execution.models import (
    AggregationKey,
    GroupedRuleMetrics,
    HighCardinalityBehavior,
    InvalidTimestampPolicy,
    JsonScalar,
    NullBucketPolicy,
    ResolvedAggregationSpecification,
    RuleMetric,
    TimeGrain,
)


class AccumulatorError(RuntimeError):
    """Safe structured error raised by bounded execution state."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True, order=True, slots=True)
class InternalAggregationKey:
    """Hashable internal representation with deterministic ordering."""

    overall: bool
    time_period: str | None = None
    dimensions: tuple[tuple[str, JsonScalar], ...] = ()

    def to_contract(self) -> AggregationKey:
        return AggregationKey(
            overall=self.overall,
            time_period=self.time_period,
            dimensions=dict(self.dimensions),
        )

    @classmethod
    def overall_key(cls) -> "InternalAggregationKey":
        return cls(overall=True)


@dataclass(slots=True)
class CountAccumulator:
    """Mergeable row eligibility and outcome counters."""

    row_count: int = 0
    eligible_count: int = 0
    passed_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0

    def observe(
        self,
        *,
        eligible: bool,
        passed: bool | None = None,
        weight: int = 1,
    ) -> None:
        if weight < 0:
            raise AccumulatorError("NEGATIVE_WEIGHT", "Observation weight cannot be negative.")
        self.row_count += weight
        if not eligible:
            self.skipped_count += weight
            return
        self.eligible_count += weight
        if passed is True:
            self.passed_count += weight
        elif passed is False:
            self.failed_count += weight

    def merge(self, other: "CountAccumulator") -> None:
        self.row_count += other.row_count
        self.eligible_count += other.eligible_count
        self.passed_count += other.passed_count
        self.failed_count += other.failed_count
        self.skipped_count += other.skipped_count

    def copy(self) -> "CountAccumulator":
        return CountAccumulator(
            row_count=self.row_count,
            eligible_count=self.eligible_count,
            passed_count=self.passed_count,
            failed_count=self.failed_count,
            skipped_count=self.skipped_count,
        )


@dataclass(slots=True)
class NumericAccumulator:
    """Mergeable finite numeric moments using constant memory."""

    count: int = 0
    total: float = 0.0
    minimum: float | None = None
    maximum: float | None = None
    invalid_count: int = 0

    def observe(self, value: int | float | Decimal | None) -> None:
        if value is None:
            self.invalid_count += 1
            return
        number = float(value)
        if not math.isfinite(number):
            self.invalid_count += 1
            return
        self.count += 1
        self.total += number
        self.minimum = number if self.minimum is None else min(self.minimum, number)
        self.maximum = number if self.maximum is None else max(self.maximum, number)

    def merge(self, other: "NumericAccumulator") -> None:
        self.count += other.count
        self.total += other.total
        self.invalid_count += other.invalid_count
        if other.minimum is not None:
            self.minimum = other.minimum if self.minimum is None else min(self.minimum, other.minimum)
        if other.maximum is not None:
            self.maximum = other.maximum if self.maximum is None else max(self.maximum, other.maximum)

    @property
    def mean(self) -> float | None:
        return None if self.count == 0 else self.total / self.count


@dataclass(slots=True)
class BoundedFrequencyAccumulator:
    """Bounded deterministic category counts with an explicit overflow count.

    This is intentionally not an unbounded exact ``Counter``. Once capacity is
    reached, unseen categories contribute only to ``overflow_count``. Existing
    tracked categories remain exact, and merge order is deterministic when the
    coordinator merges chunk partials in chunk-index order.
    """

    capacity: int
    counts: dict[Hashable, int] = field(default_factory=dict)
    overflow_count: int = 0
    overflow_distinct_lower_bound: int = 0

    def __post_init__(self) -> None:
        if self.capacity < 1:
            raise AccumulatorError("INVALID_CATEGORY_CAP", "Category capacity must be positive.")

    def observe(self, category: Hashable, *, weight: int = 1) -> None:
        if weight < 0:
            raise AccumulatorError("NEGATIVE_WEIGHT", "Observation weight cannot be negative.")
        if category in self.counts:
            self.counts[category] += weight
        elif len(self.counts) < self.capacity:
            self.counts[category] = weight
        else:
            self.overflow_count += weight
            self.overflow_distinct_lower_bound += 1

    def merge(self, other: "BoundedFrequencyAccumulator") -> None:
        if self.capacity != other.capacity:
            raise AccumulatorError(
                "INCOMPATIBLE_ACCUMULATORS",
                "Frequency accumulators must use the same category capacity.",
            )
        for category, count in sorted(other.counts.items(), key=lambda item: repr(item[0])):
            self.observe(category, weight=count)
        self.overflow_count += other.overflow_count
        self.overflow_distinct_lower_bound += other.overflow_distinct_lower_bound

    @property
    def truncated(self) -> bool:
        return self.overflow_count > 0

    def top(self, limit: int | None = None) -> tuple[tuple[Hashable, int], ...]:
        effective_limit = self.capacity if limit is None else max(0, min(limit, self.capacity))
        ranked = sorted(self.counts.items(), key=lambda item: (-item[1], repr(item[0])))
        return tuple(ranked[:effective_limit])


@dataclass(slots=True)
class RuleGroupAccumulator:
    """Generic per-rule/per-group state shared by deterministic handlers."""

    counts: CountAccumulator = field(default_factory=CountAccumulator)
    numerics: dict[str, NumericAccumulator] = field(default_factory=dict)
    categories: dict[str, BoundedFrequencyAccumulator] = field(default_factory=dict)

    def numeric(self, name: str) -> NumericAccumulator:
        if not name:
            raise AccumulatorError("INVALID_METRIC_NAME", "Metric name cannot be empty.")
        return self.numerics.setdefault(name, NumericAccumulator())

    def frequency(self, name: str, *, capacity: int) -> BoundedFrequencyAccumulator:
        if not name:
            raise AccumulatorError("INVALID_METRIC_NAME", "Metric name cannot be empty.")
        existing = self.categories.get(name)
        if existing is None:
            existing = BoundedFrequencyAccumulator(capacity=capacity)
            self.categories[name] = existing
        elif existing.capacity != capacity:
            raise AccumulatorError(
                "INCOMPATIBLE_CATEGORY_CAP",
                "A category metric cannot change capacity during execution.",
            )
        return existing

    def merge(self, other: "RuleGroupAccumulator") -> None:
        self.counts.merge(other.counts)
        for name in sorted(other.numerics):
            self.numeric(name).merge(other.numerics[name])
        for name in sorted(other.categories):
            source = other.categories[name]
            self.frequency(name, capacity=source.capacity).merge(source)


@dataclass(frozen=True, slots=True)
class KeyResolution:
    """Result of creating a group key from one compact row projection."""

    key: InternalAggregationKey | None
    invalid_timestamp: bool = False
    excluded_null_dimension: bool = False


class AggregationKeyFactory:
    """Create canonical group keys independently from rule calculations."""

    def __init__(self, specification: ResolvedAggregationSpecification) -> None:
        self._specification = specification
        try:
            self._timezone = ZoneInfo(specification.timezone)
        except ZoneInfoNotFoundError as exc:
            raise AccumulatorError("INVALID_TIMEZONE", "Resolved timezone is not recognized.") from exc

    def build(self, values: Mapping[str, object]) -> KeyResolution:
        invalid_timestamp = False
        time_period: str | None = None
        if self._specification.time_column is not None:
            raw_timestamp = values.get(self._specification.time_column)
            parsed = self._parse_timestamp(raw_timestamp)
            if parsed is None:
                invalid_timestamp = True
                policy = self._specification.invalid_timestamp_policy
                if policy == InvalidTimestampPolicy.FAIL.value:
                    raise AccumulatorError(
                        "INVALID_TIMESTAMP",
                        "An aggregation timestamp could not be parsed.",
                    )
                if policy == InvalidTimestampPolicy.EXCLUDE.value:
                    return KeyResolution(key=None, invalid_timestamp=True)
                time_period = self._specification.unknown_time_bucket_label
            else:
                time_period = self._bucket_timestamp(parsed)

        dimensions: list[tuple[str, JsonScalar]] = []
        for dimension in self._specification.dimensions:
            raw = values.get(dimension.column)
            normalized = self._normalize_dimension_value(raw)
            if normalized is None:
                if dimension.null_policy == NullBucketPolicy.FAIL.value:
                    raise AccumulatorError(
                        "NULL_DIMENSION_VALUE",
                        "A null aggregation dimension was encountered.",
                    )
                if dimension.null_policy == NullBucketPolicy.EXCLUDE.value:
                    return KeyResolution(
                        key=None,
                        invalid_timestamp=invalid_timestamp,
                        excluded_null_dimension=True,
                    )
                normalized = dimension.null_bucket_label
            dimensions.append((dimension.output_name, normalized))

        return KeyResolution(
            key=InternalAggregationKey(
                overall=False,
                time_period=time_period,
                dimensions=tuple(dimensions),
            ),
            invalid_timestamp=invalid_timestamp,
        )

    def _parse_timestamp(self, value: object) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, date):
            parsed = datetime(value.year, value.month, value.day)
        elif isinstance(value, str):
            candidate = value.strip()
            if not candidate:
                return None
            try:
                parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
            except ValueError:
                return None
        else:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=self._timezone)
        return parsed.astimezone(self._timezone)

    def _bucket_timestamp(self, value: datetime) -> str:
        grain = self._specification.time_grain
        if grain == TimeGrain.DAY.value:
            return value.date().isoformat()
        if grain == TimeGrain.WEEK.value:
            monday = value.date().toordinal() - value.weekday()
            return f"{date.fromordinal(monday).isoformat()}/P7D"
        if grain == TimeGrain.MONTH.value:
            return f"{value.year:04d}-{value.month:02d}"
        if grain == TimeGrain.QUARTER.value:
            return f"{value.year:04d}-Q{((value.month - 1) // 3) + 1}"
        if grain == TimeGrain.YEAR.value:
            return f"{value.year:04d}"
        raise AccumulatorError("INVALID_TIME_GRAIN", "Cannot bucket an unsupported time grain.")

    @staticmethod
    def _normalize_dimension_value(value: object) -> JsonScalar:
        if value is None:
            return None
        try:
            if value != value:  # NaN/NaT without importing pandas.
                return None
        except (TypeError, ValueError):
            pass
        if isinstance(value, (str, int, bool)):
            return value
        if isinstance(value, (float, Decimal)):
            number = float(value)
            return number if math.isfinite(number) else None
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        return str(value)


class BoundedGroupedAccumulator:
    """Bounded map of group keys to compact handler accumulator state."""

    def __init__(
        self,
        specification: ResolvedAggregationSpecification,
        *,
        maximum_categories_per_metric: int,
    ) -> None:
        if maximum_categories_per_metric < 1:
            raise AccumulatorError(
                "INVALID_CATEGORY_CAP", "Maximum categories per metric must be positive."
            )
        self._specification = specification
        self._category_cap = maximum_categories_per_metric
        self._groups: dict[InternalAggregationKey, RuleGroupAccumulator] = {}
        self._key_factory = AggregationKeyFactory(specification)
        self._invalid_timestamp_count = 0
        self._excluded_row_count = 0
        self._overflow_row_count = 0
        if specification.include_overall:
            self._groups[InternalAggregationKey.overall_key()] = RuleGroupAccumulator()

    @property
    def category_capacity(self) -> int:
        return self._category_cap

    @property
    def group_count(self) -> int:
        return sum(1 for key in self._groups if not key.overall)

    @property
    def invalid_timestamp_count(self) -> int:
        return self._invalid_timestamp_count

    @property
    def excluded_row_count(self) -> int:
        return self._excluded_row_count

    @property
    def overflow_row_count(self) -> int:
        return self._overflow_row_count

    def groups_for_values(
        self, values: Mapping[str, object]
    ) -> tuple[tuple[InternalAggregationKey, RuleGroupAccumulator], ...]:
        """Return overall and resolved group state for one filtered row projection."""

        result: list[tuple[InternalAggregationKey, RuleGroupAccumulator]] = []
        overall = InternalAggregationKey.overall_key()
        if self._specification.include_overall:
            result.append((overall, self._groups[overall]))

        has_grouping = (
            self._specification.time_column is not None
            or bool(self._specification.dimensions)
        )
        if not has_grouping:
            return tuple(result)

        resolution = self._key_factory.build(values)
        if resolution.invalid_timestamp:
            self._invalid_timestamp_count += 1
        if resolution.key is None:
            self._excluded_row_count += 1
            return tuple(result)

        key = resolution.key
        state = self._groups.get(key)
        if state is None:
            if self.group_count >= self._new_group_limit:
                if self._specification.high_cardinality_behavior == HighCardinalityBehavior.REJECT.value:
                    raise AccumulatorError(
                        "OBSERVED_GROUP_LIMIT_EXCEEDED",
                        "Observed aggregation groups exceed the configured maximum.",
                    )
                key = self._overflow_key(key)
                self._overflow_row_count += 1
            state = self._groups.setdefault(key, RuleGroupAccumulator())
        result.append((key, state))
        return tuple(result)

    def merge(self, other: "BoundedGroupedAccumulator") -> None:
        """Merge a compact chunk partial in deterministic chunk order."""

        if self._specification != other._specification:
            raise AccumulatorError(
                "INCOMPATIBLE_ACCUMULATORS",
                "Grouped accumulators must use the same resolved specification.",
            )
        if self._category_cap != other._category_cap:
            raise AccumulatorError(
                "INCOMPATIBLE_ACCUMULATORS",
                "Grouped accumulators must use the same category limit.",
            )
        self._invalid_timestamp_count += other._invalid_timestamp_count
        self._excluded_row_count += other._excluded_row_count
        self._overflow_row_count += other._overflow_row_count
        for key in sorted(other._groups, key=self._sort_key):
            source = other._groups[key]
            target_key = key
            if target_key not in self._groups and not target_key.overall:
                if self.group_count >= self._new_group_limit:
                    if self._specification.high_cardinality_behavior == HighCardinalityBehavior.REJECT.value:
                        raise AccumulatorError(
                            "OBSERVED_GROUP_LIMIT_EXCEEDED",
                            "Merged aggregation groups exceed the configured maximum.",
                        )
                    target_key = self._overflow_key(target_key)
            self._groups.setdefault(target_key, RuleGroupAccumulator()).merge(source)

    def finalized_groups(
        self,
        *,
        metrics_by_group: Mapping[InternalAggregationKey, Iterable[RuleMetric]] | None = None,
    ) -> tuple[GroupedRuleMetrics, ...]:
        """Return canonical grouped counts in stable key order."""

        metrics_by_group = metrics_by_group or {}
        finalized: list[GroupedRuleMetrics] = []
        for key in sorted(self._groups, key=self._sort_key):
            state = self._groups[key]
            finalized.append(
                GroupedRuleMetrics(
                    aggregation_key=key.to_contract(),
                    row_count=state.counts.row_count,
                    eligible_count=state.counts.eligible_count,
                    passed_count=state.counts.passed_count,
                    failed_count=state.counts.failed_count,
                    skipped_count=state.counts.skipped_count,
                    metrics=list(metrics_by_group.get(key, ())),
                )
            )
        return tuple(finalized)

    def _overflow_key(self, key: InternalAggregationKey) -> InternalAggregationKey:
        return InternalAggregationKey(
            overall=False,
            time_period=(
                "__OTHER_TIME__"
                if self._specification.time_column is not None
                else None
            ),
            dimensions=tuple(
                (name, self._other_label(name)) for name, _ in key.dimensions
            ),
        )

    def _other_label(self, output_name: str) -> str:
        for dimension in self._specification.dimensions:
            if dimension.output_name == output_name:
                return dimension.other_bucket_label
        return "__OTHER__"

    @property
    def _new_group_limit(self) -> int:
        """Reserve one bounded slot for the deterministic overflow group."""

        if (
            self._specification.high_cardinality_behavior
            == HighCardinalityBehavior.OTHER_BUCKET.value
        ):
            return max(0, self._specification.maximum_groups - 1)
        return self._specification.maximum_groups

    @staticmethod
    def _sort_key(key: InternalAggregationKey) -> tuple[object, ...]:
        return (
            0 if key.overall else 1,
            key.time_period or "",
            tuple((name, repr(value)) for name, value in key.dimensions),
        )


__all__ = [
    "AccumulatorError",
    "AggregationKeyFactory",
    "BoundedFrequencyAccumulator",
    "BoundedGroupedAccumulator",
    "CountAccumulator",
    "InternalAggregationKey",
    "KeyResolution",
    "NumericAccumulator",
    "RuleGroupAccumulator",
]
