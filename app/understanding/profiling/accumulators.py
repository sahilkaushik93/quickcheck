"""Mergeable, bounded-memory accumulators for single-pass profiling."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from numbers import Integral, Real
from typing import Any

from app.understanding.models import (
    BoundedSampleSummary,
    ColumnProfile,
    DateTimeStatistics,
    LogicalDataType,
    NumericStatistics,
    StringStatistics,
    ValueFrequency,
)
from app.understanding.source_adapters.base import SourceColumn, TabularChunk


_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_PHONE = re.compile(r"(?<!\d)(?:\+?\d[\d .()-]{7,}\d)(?!\d)")
_SPEAKER = re.compile(r"(?:^|\n)\s*(?:agent|customer|caller|representative)\s*:", re.I)


@dataclass(frozen=True, slots=True)
class AccumulatorOptions:
    """Limits relevant to per-column accumulator state."""

    max_distinct_values: int = 50000
    max_top_values: int = 20
    max_samples: int = 10
    max_patterns: int = 20
    max_pattern_scan_characters: int = 10000
    max_quantile_samples: int = 2048
    quantiles: tuple[float, ...] = (0.25, 0.5, 0.75, 0.95, 0.99)
    null_tokens: frozenset[str] = frozenset({"", "null", "none", "nan", "n/a", "na"})
    trim_null_strings: bool = True

    def __post_init__(self) -> None:
        positive = {
            "max_distinct_values": self.max_distinct_values,
            "max_top_values": self.max_top_values,
            "max_pattern_scan_characters": self.max_pattern_scan_characters,
            "max_quantile_samples": self.max_quantile_samples,
        }
        if any(value < 1 for value in positive.values()):
            raise ValueError("distinct, frequency, scan and quantile limits must be positive")
        if self.max_samples < 0 or self.max_patterns < 0:
            raise ValueError("sample and pattern limits cannot be negative")
        if any(probability < 0.0 or probability > 1.0 for probability in self.quantiles):
            raise ValueError("quantile probabilities must be between zero and one")

    @classmethod
    def from_mapping(
        cls,
        bounded_state: dict[str, Any],
        statistics: dict[str, Any],
    ) -> "AccumulatorOptions":
        """Build options from the matching profiling-config sections."""

        null_tokens = statistics.get("null_tokens", ["", "null", "none", "nan", "n/a", "na"])
        return cls(
            max_distinct_values=int(bounded_state.get("max_distinct_values_per_column", 50000)),
            max_top_values=int(bounded_state.get("max_top_values_per_column", 20)),
            max_samples=int(bounded_state.get("max_safe_samples_per_column", 10)),
            max_patterns=int(bounded_state.get("max_detected_patterns_per_column", 20)),
            max_pattern_scan_characters=min(
                10000,
                int(bounded_state.get("max_profiled_string_length", 1000000)),
            ),
            quantiles=tuple(float(value) for value in statistics.get("quantiles", [0.25, 0.5, 0.75, 0.95, 0.99])),
            null_tokens=frozenset(str(value).casefold() for value in null_tokens),
            trim_null_strings=bool(statistics.get("trim_whitespace_for_null_detection", True)),
        )


@dataclass(slots=True)
class WelfordAccumulator:
    count: int = 0
    mean: float = 0.0
    m2: float = 0.0
    minimum: float | None = None
    maximum: float | None = None
    max_quantile_samples: int = 2048
    quantile_probabilities: tuple[float, ...] = (0.25, 0.5, 0.75, 0.95, 0.99)
    quantile_samples: dict[str, float] = field(default_factory=dict)

    def update(self, value: float) -> None:
        if not math.isfinite(value):
            return
        self.count += 1
        delta = value - self.mean
        self.mean += delta / self.count
        self.m2 += delta * (value - self.mean)
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)
        key = hashlib.sha256(repr(value).encode("ascii", errors="replace")).hexdigest()
        self.quantile_samples[key] = value
        if len(self.quantile_samples) > self.max_quantile_samples:
            del self.quantile_samples[max(self.quantile_samples)]

    def merge(self, other: "WelfordAccumulator") -> None:
        if other.count == 0:
            return
        if self.count == 0:
            self.count, self.mean, self.m2 = other.count, other.mean, other.m2
            self.minimum, self.maximum = other.minimum, other.maximum
        else:
            combined = self.count + other.count
            delta = other.mean - self.mean
            self.m2 += other.m2 + delta * delta * self.count * other.count / combined
            self.mean = (self.mean * self.count + other.mean * other.count) / combined
            self.count = combined
            self.minimum = min(v for v in (self.minimum, other.minimum) if v is not None)
            self.maximum = max(v for v in (self.maximum, other.maximum) if v is not None)
        self.quantile_samples.update(other.quantile_samples)
        for key in sorted(self.quantile_samples)[self.max_quantile_samples :]:
            del self.quantile_samples[key]

    def to_model(self) -> NumericStatistics | None:
        if not self.count:
            return None
        variance = self.m2 / (self.count - 1) if self.count > 1 else 0.0
        ordered = sorted(self.quantile_samples.values())
        quantiles = {
            str(probability): self._quantile(ordered, probability)
            for probability in self.quantile_probabilities
            if ordered
        }
        return NumericStatistics(
            count=self.count,
            minimum=self.minimum,
            maximum=self.maximum,
            mean=self.mean,
            standard_deviation=math.sqrt(max(0.0, variance)),
            quantiles=quantiles,
        )

    @staticmethod
    def _quantile(values: list[float], probability: float) -> float:
        position = probability * (len(values) - 1)
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return values[lower]
        weight = position - lower
        return values[lower] * (1.0 - weight) + values[upper] * weight


@dataclass(slots=True)
class StringAccumulator:
    count: int = 0
    total_length: int = 0
    minimum_length: int | None = None
    maximum_length: int | None = None
    blank_count: int = 0
    whitespace_only_count: int = 0

    def update(self, value: str) -> None:
        length = len(value)
        self.count += 1
        self.total_length += length
        self.minimum_length = length if self.minimum_length is None else min(self.minimum_length, length)
        self.maximum_length = length if self.maximum_length is None else max(self.maximum_length, length)
        self.blank_count += int(value == "")
        self.whitespace_only_count += int(bool(value) and not value.strip())

    def merge(self, other: "StringAccumulator") -> None:
        self.count += other.count
        self.total_length += other.total_length
        self.blank_count += other.blank_count
        self.whitespace_only_count += other.whitespace_only_count
        values = [v for v in (self.minimum_length, other.minimum_length) if v is not None]
        self.minimum_length = min(values) if values else None
        values = [v for v in (self.maximum_length, other.maximum_length) if v is not None]
        self.maximum_length = max(values) if values else None

    def to_model(self) -> StringStatistics | None:
        if not self.count:
            return None
        return StringStatistics(
            count=self.count,
            minimum_length=self.minimum_length,
            maximum_length=self.maximum_length,
            average_length=self.total_length / self.count,
            blank_count=self.blank_count,
            whitespace_only_count=self.whitespace_only_count,
        )


@dataclass(slots=True)
class DateAccumulator:
    count: int = 0
    minimum: datetime | None = None
    maximum: datetime | None = None

    def update(self, value: date | datetime) -> None:
        normalized = value if isinstance(value, datetime) else datetime.combine(value, datetime.min.time())
        self.count += 1
        self.minimum = normalized if self.minimum is None else min(self.minimum, normalized)
        self.maximum = normalized if self.maximum is None else max(self.maximum, normalized)

    def merge(self, other: "DateAccumulator") -> None:
        self.count += other.count
        values = [v for v in (self.minimum, other.minimum) if v is not None]
        self.minimum = min(values) if values else None
        values = [v for v in (self.maximum, other.maximum) if v is not None]
        self.maximum = max(values) if values else None

    def to_model(self) -> DateTimeStatistics | None:
        if not self.count:
            return None
        return DateTimeStatistics(count=self.count, minimum=self.minimum, maximum=self.maximum)


class ColumnAccumulator:
    """Accumulate safe, mergeable statistics for one column."""

    def __init__(self, column: SourceColumn, options: AccumulatorOptions) -> None:
        self.column = column
        self.options = options
        self.row_count = 0
        self.null_count = 0
        self.type_counts: Counter[str] = Counter()
        self.numeric = WelfordAccumulator(
            max_quantile_samples=options.max_quantile_samples,
            quantile_probabilities=options.quantiles,
        )
        self.strings = StringAccumulator()
        self.dates = DateAccumulator()
        self.distinct_hashes: set[str] = set()
        self.distinct_capped = False
        self.frequencies: Counter[str] = Counter()
        self.samples: set[str] = set()
        self.pattern_counts: Counter[str] = Counter()

    def update_many(self, values: Sequence[Any]) -> None:
        for value in values:
            self.update(value)

    def update(self, value: Any) -> None:
        self.row_count += 1
        if self._is_null(value):
            self.null_count += 1
            return

        safe_hash = self._safe_hash(value)
        if len(self.distinct_hashes) < self.options.max_distinct_values:
            self.distinct_hashes.add(safe_hash)
        elif safe_hash not in self.distinct_hashes:
            self.distinct_capped = True
        self._update_frequency(safe_hash)
        self._update_samples(safe_hash)

        if isinstance(value, bool):
            self.type_counts["boolean"] += 1
        elif isinstance(value, Integral):
            self.type_counts["integer"] += 1
            self.numeric.update(float(value))
        elif isinstance(value, Real):
            self.type_counts["float"] += 1
            self.numeric.update(float(value))
        elif isinstance(value, (datetime, date)):
            self.type_counts["datetime"] += 1
            self.dates.update(value)
        elif isinstance(value, str):
            self.type_counts["string"] += 1
            self.strings.update(value)
            self._detect_patterns(value)
        elif isinstance(value, (dict, list, tuple)):
            self.type_counts["json"] += 1
        else:
            self.type_counts["unknown"] += 1

    def merge(self, other: "ColumnAccumulator") -> None:
        if self.column.name.casefold() != other.column.name.casefold():
            raise ValueError("cannot merge accumulators for different columns")
        self.row_count += other.row_count
        self.null_count += other.null_count
        self.type_counts.update(other.type_counts)
        self.numeric.merge(other.numeric)
        self.strings.merge(other.strings)
        self.dates.merge(other.dates)
        combined = sorted(self.distinct_hashes | other.distinct_hashes)
        self.distinct_capped |= other.distinct_capped or len(combined) > self.options.max_distinct_values
        self.distinct_hashes = set(combined[: self.options.max_distinct_values])
        self.frequencies.update(other.frequencies)
        self.frequencies = Counter(dict(self.frequencies.most_common(self.options.max_top_values)))
        self.samples = set(sorted(self.samples | other.samples)[: self.options.max_samples])
        self.pattern_counts.update(other.pattern_counts)

    def to_profile(self) -> ColumnProfile:
        non_null = self.row_count - self.null_count
        inferred, confidence = self._inferred_type(non_null)
        top = [
            ValueFrequency(
                value=f"<hash:{value}>",
                count=count,
                percentage=(count / non_null * 100.0) if non_null else 0.0,
                redacted=True,
            )
            for value, count in self.frequencies.most_common(self.options.max_top_values)
        ]
        sample_values = [f"<hash:{value}>" for value in sorted(self.samples)]
        return ColumnProfile(
            column_name=self.column.name,
            source_data_type=self.column.source_data_type,
            inferred_data_type=inferred,
            inferred_type_confidence=confidence,
            row_count=self.row_count,
            non_null_count=non_null,
            null_count=self.null_count,
            fill_rate=(non_null / self.row_count) if self.row_count else 0.0,
            distinct_count=None if self.distinct_capped else len(self.distinct_hashes),
            distinct_count_mode="capped" if self.distinct_capped else "exact",
            distinct_count_lower_bound=len(self.distinct_hashes) if self.distinct_capped else None,
            top_values=top,
            numeric_statistics=self.numeric.to_model(),
            string_statistics=self.strings.to_model(),
            datetime_statistics=self.dates.to_model(),
            safe_samples=BoundedSampleSummary(
                strategy="hashed" if sample_values else "none",
                values=sample_values,
                observed_count=non_null,
                retained_count=len(sample_values),
                truncated=non_null > len(sample_values),
            ),
            detected_patterns=[name for name, _ in self.pattern_counts.most_common(self.options.max_patterns)],
        )

    def _is_null(self, value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, float) and math.isnan(value):
            return True
        if isinstance(value, str):
            candidate = value.strip() if self.options.trim_null_strings else value
            return candidate.casefold() in self.options.null_tokens
        return False

    @staticmethod
    def _safe_hash(value: Any) -> str:
        try:
            canonical = json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            canonical = f"<{type(value).__name__}>"
        return hashlib.sha256(canonical.encode("utf-8", errors="replace")).hexdigest()[:16]

    def _update_frequency(self, value_hash: str) -> None:
        if value_hash in self.frequencies or len(self.frequencies) < self.options.max_top_values:
            self.frequencies[value_hash] += 1
            return
        smallest, count = min(self.frequencies.items(), key=lambda item: (item[1], item[0]))
        del self.frequencies[smallest]
        self.frequencies[value_hash] = count + 1

    def _update_samples(self, value_hash: str) -> None:
        if self.options.max_samples == 0:
            return
        self.samples.add(value_hash)
        if len(self.samples) > self.options.max_samples:
            self.samples.remove(max(self.samples))

    def _detect_patterns(self, value: str) -> None:
        text = value[: self.options.max_pattern_scan_characters]
        if _EMAIL.search(text):
            self.pattern_counts["email_like"] += 1
        if _PHONE.search(text):
            self.pattern_counts["phone_like"] += 1
        if _SPEAKER.search(text):
            self.pattern_counts["speaker_labels"] += 1
        if "|" in text:
            self.pattern_counts["pipe_delimited"] += 1

    def _inferred_type(self, non_null: int) -> tuple[LogicalDataType, float]:
        if not non_null or not self.type_counts:
            return LogicalDataType.UNKNOWN, 0.0
        name, count = self.type_counts.most_common(1)[0]
        mapping = {
            "boolean": LogicalDataType.BOOLEAN,
            "integer": LogicalDataType.INTEGER,
            "float": LogicalDataType.FLOAT,
            "datetime": LogicalDataType.DATETIME,
            "string": LogicalDataType.STRING,
            "json": LogicalDataType.JSON,
            "unknown": LogicalDataType.UNKNOWN,
        }
        return mapping[name], count / non_null


class DatasetAccumulator:
    """Column accumulator collection that can be merged across chunk workers."""

    def __init__(self, columns: Iterable[SourceColumn], options: AccumulatorOptions) -> None:
        self.options = options
        self.columns = {column.name: ColumnAccumulator(column, options) for column in columns}
        self.chunks_processed = 0

    def update_chunk(self, chunk: TabularChunk) -> None:
        for name, accumulator in self.columns.items():
            accumulator.update_many(chunk.column_values(name))
        self.chunks_processed += 1

    def merge(self, other: "DatasetAccumulator") -> None:
        if tuple(self.columns) != tuple(other.columns):
            raise ValueError("cannot merge dataset accumulators with different schemas")
        for name, accumulator in self.columns.items():
            accumulator.merge(other.columns[name])
        self.chunks_processed += other.chunks_processed

    @property
    def row_count(self) -> int:
        first = next(iter(self.columns.values()), None)
        return first.row_count if first else 0

    def profiles(self) -> list[ColumnProfile]:
        return [accumulator.to_profile() for accumulator in self.columns.values()]
