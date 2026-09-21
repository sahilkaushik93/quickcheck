"""Schema-aware resolution and safety validation for aggregation controls.

The resolver converts caller-facing semantic or physical column references into
one deterministic :class:`ResolvedAggregationSpecification`.  It uses only the
compact Understanding output; source rows are never read or retained here.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.execution.aggregation.models import (
    AggregationColumnDescriptor,
    AggregationPolicy,
    AggregationRequest,
    CardinalityEstimate,
    FilterSpecification,
    HighCardinalityBehavior,
    ResolvedAggregationSpecification,
    ResolvedDimension,
    TimeGrain,
)
from app.understanding.models import (
    ColumnProfile,
    DomainAssignment,
    LogicalDataType,
    UnderstandingOutput,
)


class AggregationResolutionError(ValueError):
    """Safe, structured validation error suitable for API translation."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        field: str | None = None,
        column: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.column = column

    def as_dict(self) -> dict[str, str]:
        result = {"code": self.code, "message": self.message}
        if self.field is not None:
            result["field"] = self.field
        if self.column is not None:
            result["column"] = self.column
        return result


@dataclass(frozen=True, slots=True)
class AggregationResolution:
    """Resolved specification plus compact diagnostics for observability."""

    specification: ResolvedAggregationSpecification
    cardinality: CardinalityEstimate
    column_bindings: Mapping[str, str]
    warnings: tuple[str, ...] = ()


class AggregationResolver:
    """Resolve and validate user aggregation controls against Understanding.

    Resolution precedence is explicit physical override, exact source column,
    Understanding semantic/domain assignment, configured aliases, and finally
    a conservative logical-type fallback for time columns only.
    """

    def __init__(self, policy: AggregationPolicy) -> None:
        self._policy = policy
        self._forbidden_name_patterns = tuple(
            re.compile(pattern, flags=re.IGNORECASE)
            for pattern in policy.dimensions.forbidden_name_patterns
        )

    def resolve(
        self,
        request: AggregationRequest,
        understanding: UnderstandingOutput,
        *,
        column_overrides: Mapping[str, str] | None = None,
    ) -> ResolvedAggregationSpecification:
        """Return the canonical aggregation specification.

        ``column_overrides`` maps semantic roles (for example,
        ``interaction_direction`` or ``interaction_date``) to physical source
        columns.  Unknown roles and missing physical columns are rejected.
        """

        return self.resolve_with_diagnostics(
            request,
            understanding,
            column_overrides=column_overrides,
        ).specification

    def resolve_with_diagnostics(
        self,
        request: AggregationRequest,
        understanding: UnderstandingOutput,
        *,
        column_overrides: Mapping[str, str] | None = None,
    ) -> AggregationResolution:
        """Resolve controls and retain bounded diagnostics for the coordinator."""

        profiles = self._profile_index(understanding)
        assignments = self._domain_index(understanding.domains)
        overrides = self._validate_overrides(column_overrides or {}, profiles)
        warnings: list[str] = []
        bindings: dict[str, str] = {}

        grain = TimeGrain(request.time.grain)
        if grain not in self._policy.time.allowed_grains:
            raise AggregationResolutionError(
                "TIME_GRAIN_NOT_ALLOWED",
                f"Time grain {grain.value!r} is not allowed by aggregation policy.",
                field="aggregation.time.grain",
            )
        try:
            ZoneInfo(request.time.timezone)
        except ZoneInfoNotFoundError as exc:
            raise AggregationResolutionError(
                "INVALID_TIMEZONE",
                "The requested aggregation timezone is not recognized.",
                field="aggregation.time.timezone",
            ) from exc

        time_column: str | None = None
        if grain != TimeGrain.NONE:
            time_reference = request.time.column or "interaction_date"
            time_column = self._resolve_column(
                time_reference,
                profiles,
                assignments,
                overrides,
                aliases=self._policy.time.candidate_columns,
                allowed_types={LogicalDataType.DATE.value, LogicalDataType.DATETIME.value},
                allow_type_fallback=request.time.column is None,
                field="aggregation.time.column",
            )
            self._validate_time_column(profiles[time_column.casefold()])
            bindings[time_reference] = time_column

        if len(request.dimensions) > self._policy.dimensions.maximum_dimensions:
            raise AggregationResolutionError(
                "TOO_MANY_DIMENSIONS",
                "Requested dimensions exceed the configured maximum.",
                field="aggregation.dimensions",
            )

        resolved_dimensions: list[ResolvedDimension] = []
        used_columns: set[str] = set()
        used_output_names: set[str] = set()
        cardinalities: dict[str, int] = {}
        cardinality_exact = True
        for index, dimension in enumerate(request.dimensions):
            column = self._resolve_column(
                dimension.column,
                profiles,
                assignments,
                overrides,
                aliases=self._aliases_for_role(dimension.column),
                allowed_types=None,
                allow_type_fallback=False,
                field=f"aggregation.dimensions[{index}].column",
            )
            normalized = column.casefold()
            output_name = dimension.output_name
            if normalized in used_columns:
                raise AggregationResolutionError(
                    "DUPLICATE_RESOLVED_DIMENSION",
                    "Multiple requested dimensions resolve to the same source column.",
                    field=f"aggregation.dimensions[{index}].column",
                    column=column,
                )
            if output_name.casefold() in used_output_names:
                raise AggregationResolutionError(
                    "DUPLICATE_DIMENSION_ALIAS",
                    "Resolved dimension output names must be unique.",
                    field=f"aggregation.dimensions[{index}].alias",
                )

            descriptor = self._descriptor(
                profiles[normalized], assignments.get(normalized)
            )
            self._validate_dimension(descriptor, index)
            limit = min(
                dimension.maximum_cardinality
                or self._policy.dimensions.maximum_cardinality_per_dimension,
                self._policy.dimensions.maximum_cardinality_per_dimension,
            )
            cardinality, exact = self._dimension_cardinality(
                profiles[normalized], limit
            )
            cardinalities[output_name] = cardinality
            cardinality_exact = cardinality_exact and exact
            resolved_dimensions.append(
                ResolvedDimension(
                    column=column,
                    output_name=output_name,
                    null_policy=dimension.null_policy,
                    null_bucket_label=dimension.null_bucket_label,
                    cardinality_limit=limit,
                    observed_or_estimated_cardinality=cardinality,
                    other_bucket_label=dimension.other_bucket_label,
                )
            )
            used_columns.add(normalized)
            used_output_names.add(output_name.casefold())
            bindings[dimension.column] = column

        resolved_filters: list[FilterSpecification] = []
        if len(request.filters) > self._policy.filters.maximum_filters:
            raise AggregationResolutionError(
                "TOO_MANY_FILTERS",
                "Requested filters exceed the configured maximum.",
                field="aggregation.filters",
            )
        for index, item in enumerate(request.filters):
            if item.operator not in self._policy.filters.allowed_operators:
                raise AggregationResolutionError(
                    "FILTER_OPERATOR_NOT_ALLOWED",
                    f"Filter operator {item.operator!r} is not allowed.",
                    field=f"aggregation.filters[{index}].operator",
                )
            if len(item.values) > self._policy.filters.maximum_values_per_filter:
                raise AggregationResolutionError(
                    "TOO_MANY_FILTER_VALUES",
                    "Filter values exceed the configured bounded limit.",
                    field=f"aggregation.filters[{index}].values",
                )
            column = self._resolve_column(
                item.column,
                profiles,
                assignments,
                overrides,
                aliases=self._aliases_for_role(item.column),
                allowed_types=None,
                allow_type_fallback=False,
                field=f"aggregation.filters[{index}].column",
            )
            descriptor = self._descriptor(
                profiles[column.casefold()], assignments.get(column.casefold())
            )
            if descriptor.sensitive and not self._policy.filters.allow_filters_on_sensitive_columns:
                raise AggregationResolutionError(
                    "SENSITIVE_FILTER_FORBIDDEN",
                    "Filtering on a sensitive identifier is not allowed.",
                    field=f"aggregation.filters[{index}].column",
                    column=column,
                )
            if descriptor.free_text and not self._policy.filters.allow_filters_on_free_text_columns:
                raise AggregationResolutionError(
                    "FREE_TEXT_FILTER_FORBIDDEN",
                    "Filtering on transcript or free-text content is not allowed.",
                    field=f"aggregation.filters[{index}].column",
                    column=column,
                )
            resolved_filters.append(item.model_copy(update={"column": column}))
            bindings[item.column] = column

        if not request.include_overall and time_column is None and not resolved_dimensions:
            raise AggregationResolutionError(
                "NO_AGGREGATION_OUTPUT_REQUESTED",
                "At least overall results, a time grain, or one dimension must be requested.",
                field="aggregation.include_overall",
            )

        maximum_groups = min(
            request.maximum_groups or self._policy.dimensions.maximum_groups,
            self._policy.dimensions.maximum_groups,
        )
        time_bucket_count = self._estimate_time_buckets(
            profiles[time_column.casefold()] if time_column else None,
            grain,
            maximum_groups,
            self._policy.time.unknown_range_bucket_estimates,
        )
        estimated_groups = self._bounded_product(
            [time_bucket_count, *cardinalities.values()], maximum_groups + 1
        )
        safe = estimated_groups <= maximum_groups
        if not safe and request.high_cardinality_behavior == HighCardinalityBehavior.REJECT.value:
            raise AggregationResolutionError(
                "MAXIMUM_GROUP_COUNT_EXCEEDED",
                "Estimated aggregation group count exceeds the configured maximum.",
                field="aggregation.maximum_groups",
            )
        if not safe:
            warnings.append(
                "Estimated group count exceeds the limit; overflow groups will use the configured other bucket."
            )

        estimate = CardinalityEstimate(
            per_dimension=cardinalities,
            time_bucket_count=time_bucket_count,
            estimated_group_count=estimated_groups,
            maximum_groups=maximum_groups,
            exact=cardinality_exact,
            safe=safe,
            rationale=(
                "Estimate derived from Understanding profile cardinalities and bounded time range."
            ),
        )
        specification = ResolvedAggregationSpecification(
            time_column=time_column,
            time_grain=grain,
            timezone=request.time.timezone,
            invalid_timestamp_policy=request.time.invalid_timestamp_policy,
            unknown_time_bucket_label=request.time.unknown_bucket_label,
            dimensions=resolved_dimensions,
            filters=resolved_filters,
            include_overall=request.include_overall,
            include_empty_groups=request.include_empty_groups,
            maximum_groups=maximum_groups,
            estimated_group_count=estimated_groups,
            high_cardinality_behavior=request.high_cardinality_behavior,
        )
        return AggregationResolution(
            specification=specification,
            cardinality=estimate,
            column_bindings=dict(sorted(bindings.items(), key=lambda item: item[0].casefold())),
            warnings=tuple(warnings),
        )

    @staticmethod
    def _profile_index(understanding: UnderstandingOutput) -> dict[str, ColumnProfile]:
        return {
            profile.column_name.casefold(): profile
            for profile in understanding.profile.columns
        }

    @staticmethod
    def _domain_index(
        assignments: Iterable[DomainAssignment],
    ) -> dict[str, DomainAssignment]:
        return {item.column_name.casefold(): item for item in assignments}

    @staticmethod
    def _validate_overrides(
        overrides: Mapping[str, str],
        profiles: Mapping[str, ColumnProfile],
    ) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for role, column in overrides.items():
            role_key = role.strip().casefold()
            column_key = column.strip().casefold()
            if not role_key or not column_key:
                raise AggregationResolutionError(
                    "INVALID_COLUMN_OVERRIDE",
                    "Column override role and value must be non-empty.",
                    field="column_overrides",
                )
            if column_key not in profiles:
                raise AggregationResolutionError(
                    "OVERRIDE_COLUMN_NOT_FOUND",
                    "A configured column override does not exist in the source schema.",
                    field="column_overrides",
                    column=column,
                )
            normalized[role_key] = profiles[column_key].column_name
        return normalized

    def _resolve_column(
        self,
        reference: str,
        profiles: Mapping[str, ColumnProfile],
        assignments: Mapping[str, DomainAssignment],
        overrides: Mapping[str, str],
        *,
        aliases: Iterable[str],
        allowed_types: set[str] | None,
        allow_type_fallback: bool,
        field: str,
    ) -> str:
        token = reference.strip().casefold()
        if token in overrides:
            return overrides[token]
        if token in profiles:
            return profiles[token].column_name

        semantic_matches = sorted(
            assignment.column_name
            for assignment in assignments.values()
            if token
            in {
                (assignment.semantic_type or "").casefold(),
                assignment.domain.casefold(),
                (assignment.subdomain or "").casefold(),
            }
        )
        semantic_matches = self._filter_by_type(
            semantic_matches, profiles, allowed_types
        )
        if len(semantic_matches) == 1:
            return semantic_matches[0]
        if len(semantic_matches) > 1:
            raise AggregationResolutionError(
                "AMBIGUOUS_SEMANTIC_COLUMN",
                "The semantic column reference matches multiple source columns; provide an explicit override.",
                field=field,
            )

        alias_matches = [
            profiles[alias.casefold()].column_name
            for alias in aliases
            if alias.casefold() in profiles
        ]
        alias_matches = list(dict.fromkeys(alias_matches))
        alias_matches = self._filter_by_type(alias_matches, profiles, allowed_types)
        if alias_matches:
            return alias_matches[0]

        if allow_type_fallback and allowed_types:
            type_matches = sorted(
                profile.column_name
                for profile in profiles.values()
                if str(profile.inferred_data_type) in allowed_types
            )
            if len(type_matches) == 1:
                return type_matches[0]

        raise AggregationResolutionError(
            "AGGREGATION_COLUMN_NOT_FOUND",
            "The aggregation column could not be resolved safely; provide an explicit source column override.",
            field=field,
            column=reference,
        )

    @staticmethod
    def _filter_by_type(
        columns: Iterable[str],
        profiles: Mapping[str, ColumnProfile],
        allowed_types: set[str] | None,
    ) -> list[str]:
        if allowed_types is None:
            return list(columns)
        return [
            column
            for column in columns
            if str(profiles[column.casefold()].inferred_data_type) in allowed_types
        ]

    def _aliases_for_role(self, reference: str) -> tuple[str, ...]:
        normalized = reference.casefold()
        for role, aliases in self._policy.dimensions.role_aliases.items():
            candidates = (role, *aliases)
            if normalized in {value.casefold() for value in candidates}:
                return tuple(candidates)
        return ()

    @staticmethod
    def _validate_time_column(profile: ColumnProfile) -> None:
        if str(profile.inferred_data_type) not in {
            LogicalDataType.DATE.value,
            LogicalDataType.DATETIME.value,
        }:
            raise AggregationResolutionError(
                "INVALID_TIME_COLUMN_TYPE",
                "Resolved time column is not profiled as a date or timestamp.",
                field="aggregation.time.column",
                column=profile.column_name,
            )

    def _descriptor(
        self,
        profile: ColumnProfile,
        assignment: DomainAssignment | None,
    ) -> AggregationColumnDescriptor:
        semantic = assignment.semantic_type if assignment else None
        domain = assignment.domain if assignment else None
        semantic_normalized = (semantic or "").casefold()
        domain_normalized = (domain or "").casefold()
        forbidden_semantics = {
            item.casefold() for item in self._policy.dimensions.forbidden_semantic_types
        }
        sensitive = any(
            marker in semantic_normalized or marker in domain_normalized
            for marker in ("pii", "secret", "identifier", "customer", "account")
        )
        free_text = (
            semantic_normalized in forbidden_semantics
            or domain_normalized in {"transcript", "free_text"}
            or any(pattern.search(profile.column_name) for pattern in self._forbidden_name_patterns)
            or (
                str(profile.inferred_data_type) == LogicalDataType.STRING.value
                and profile.string_statistics is not None
                and (profile.string_statistics.average_length or 0.0) >= 256.0
            )
        )
        distinct = profile.distinct_count
        if distinct is None:
            distinct = profile.distinct_count_lower_bound
        return AggregationColumnDescriptor(
            column_name=profile.column_name,
            logical_type=profile.inferred_data_type,
            semantic_type=semantic,
            domain=domain,
            distinct_count=distinct,
            distinct_count_is_lower_bound=profile.distinct_count_mode != "exact",
            sensitive=sensitive,
            free_text=free_text,
        )

    def _validate_dimension(
        self, descriptor: AggregationColumnDescriptor, index: int
    ) -> None:
        field = f"aggregation.dimensions[{index}].column"
        semantic = (descriptor.semantic_type or "").casefold()
        if descriptor.sensitive and not self._policy.dimensions.sensitive_columns_allowed:
            raise AggregationResolutionError(
                "SENSITIVE_DIMENSION_FORBIDDEN",
                "Sensitive identifiers cannot be aggregation dimensions.",
                field=field,
                column=descriptor.column_name,
            )
        if descriptor.free_text and not self._policy.dimensions.free_text_columns_allowed:
            raise AggregationResolutionError(
                "FREE_TEXT_DIMENSION_FORBIDDEN",
                "Transcript and free-text columns cannot be aggregation dimensions.",
                field=field,
                column=descriptor.column_name,
            )
        if semantic in {
            value.casefold() for value in self._policy.dimensions.forbidden_semantic_types
        }:
            raise AggregationResolutionError(
                "FORBIDDEN_DIMENSION_SEMANTIC_TYPE",
                "The column semantic type is not allowed for grouping.",
                field=field,
                column=descriptor.column_name,
            )

    @staticmethod
    def _dimension_cardinality(
        profile: ColumnProfile, limit: int
    ) -> tuple[int, bool]:
        exact = profile.distinct_count_mode == "exact" and profile.distinct_count is not None
        observed = (
            profile.distinct_count
            if profile.distinct_count is not None
            else profile.distinct_count_lower_bound
        )
        if observed is None:
            raise AggregationResolutionError(
                "DIMENSION_CARDINALITY_UNKNOWN",
                "Dimension cardinality is unavailable; grouping cannot be proven safe.",
                column=profile.column_name,
            )
        if observed > limit:
            raise AggregationResolutionError(
                "DIMENSION_CARDINALITY_EXCEEDED",
                "Dimension cardinality exceeds its configured limit.",
                column=profile.column_name,
            )
        if not exact and profile.distinct_count_mode in {"capped", "not_computed"}:
            raise AggregationResolutionError(
                "DIMENSION_CARDINALITY_NOT_VERIFIABLE",
                "Dimension cardinality is only a lower bound; safe grouping requires an exact or bounded estimate.",
                column=profile.column_name,
            )
        return max(1, observed), exact

    @classmethod
    def _estimate_time_buckets(
        cls,
        profile: ColumnProfile | None,
        grain: TimeGrain,
        maximum_groups: int,
        unknown_range_estimates: Mapping[TimeGrain, int],
    ) -> int:
        if profile is None or grain == TimeGrain.NONE:
            return 1
        stats = profile.datetime_statistics
        if stats is None or stats.minimum is None or stats.maximum is None:
            # CSV profiling may identify a date from governed metadata without
            # retaining parsed date extrema.  Use a policy-controlled planning
            # estimate here; the grouped accumulator still enforces the hard
            # maximum at runtime and never grows with unbounded cardinality.
            return min(maximum_groups, unknown_range_estimates.get(grain, maximum_groups))
        start, end = stats.minimum, stats.maximum
        days = max(0, (end.date() - start.date()).days)
        if grain == TimeGrain.DAY:
            return min(maximum_groups, days + 1)
        if grain == TimeGrain.WEEK:
            return min(maximum_groups, days // 7 + 1)
        if grain == TimeGrain.MONTH:
            return min(maximum_groups, (end.year - start.year) * 12 + end.month - start.month + 1)
        if grain == TimeGrain.QUARTER:
            start_q = start.year * 4 + (start.month - 1) // 3
            end_q = end.year * 4 + (end.month - 1) // 3
            return min(maximum_groups, end_q - start_q + 1)
        if grain == TimeGrain.YEAR:
            return min(maximum_groups, end.year - start.year + 1)
        return 1

    @staticmethod
    def _bounded_product(values: Iterable[int], stop_after: int) -> int:
        result = 1
        for value in values:
            if value < 1:
                continue
            if result > stop_after // value:
                return stop_after
            result *= value
        return min(result, stop_after)


__all__ = [
    "AggregationResolution",
    "AggregationResolutionError",
    "AggregationResolver",
]
