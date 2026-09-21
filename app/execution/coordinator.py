"""Complete deterministic, single-pass Execution Layer workflow."""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Mapping

from app.execution.aggregation.accumulators import AggregationKeyFactory, InternalAggregationKey
from app.execution.aggregation.resolver import AggregationResolver
from app.execution.evidence.collector import EvidenceCollector, EvidenceLimits
from app.execution.features.extractor import FeatureExtractor
from app.execution.features.models import FeatureRequirements, RowFeatures
from app.execution.handlers.base import HandlerLimits, HandlerState, RuleHandler
from app.execution.handlers.registry import HandlerRegistry, HandlerResolutionIssue, ResolvedHandlerStep
from app.execution.models import (
    ExecutionOutput,
    ExecutionRunRequest,
    ExecutionStatus,
    ExecutionSummary,
    ExecutionWarning,
    FilterOperator,
    FilterSpecification,
    MetricAvailability,
    MetricAvailabilityStatus,
    ResolvedAggregationSpecification,
    RuleExecutionResult,
    RuleExecutionStatus,
)
from app.execution.storage.artifact_writer import ExecutionArtifactWriter
from app.understanding.models import (
    ExecutionReadiness,
    RuleApplicabilityStatus,
    Severity,
    UnderstandingOutput,
)
from app.understanding.source_adapters.base import SourceAdapter


class ExecutionCoordinatorError(RuntimeError):
    """Safe fatal orchestration error for API/service translation."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def as_dict(self) -> dict[str, str | bool]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


@dataclass(frozen=True, slots=True)
class ExecutionCoordinatorConfig:
    """Immutable runtime controls loaded from ``execution_config.json``."""

    maximum_categories_per_rule_group: int
    maximum_warning_count: int
    decimal_places: int
    continue_on_rule_failure: bool
    require_source_fingerprint_match: bool
    deterministic_merge_order: bool

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "ExecutionCoordinatorConfig":
        try:
            runtime = values.get("runtime", {})
            bounded = values.get("bounded_state", {})
            result = values.get("result_policy", {})
            return cls(
                maximum_categories_per_rule_group=int(
                    bounded.get("max_categories_per_rule_group", 100)
                ),
                maximum_warning_count=int(bounded.get("max_warning_count", 500)),
                decimal_places=int(result.get("round_decimal_places", 6)),
                continue_on_rule_failure=bool(
                    runtime.get("continue_on_rule_failure", True)
                ),
                require_source_fingerprint_match=bool(
                    values.get("source_validation", {}).get(
                        "require_source_fingerprint_match", True
                    )
                ),
                deterministic_merge_order=bool(
                    runtime.get("deterministic_merge_order", True)
                ),
            )
        except (TypeError, ValueError) as exc:
            raise ExecutionCoordinatorError(
                "INVALID_EXECUTION_CONFIGURATION",
                f"Execution configuration is invalid ({type(exc).__name__}).",
            ) from exc

    def __post_init__(self) -> None:
        if self.maximum_categories_per_rule_group < 1:
            raise ValueError("maximum_categories_per_rule_group must be positive")
        if self.maximum_warning_count < 0:
            raise ValueError("maximum_warning_count cannot be negative")
        if not 0 <= self.decimal_places <= 15:
            raise ValueError("decimal_places must be between 0 and 15")


class ExecutionCoordinator:
    """Execute approved rules over one streaming source scan."""

    def __init__(
        self,
        *,
        config: ExecutionCoordinatorConfig,
        aggregation_resolver: AggregationResolver,
        feature_extractor: FeatureExtractor,
        handler_registry: HandlerRegistry,
        evidence_salt: bytes,
        artifact_writer: ExecutionArtifactWriter | None = None,
    ) -> None:
        self._config = config
        self._aggregation_resolver = aggregation_resolver
        self._feature_extractor = feature_extractor
        self._handler_registry = handler_registry
        self._evidence_salt = bytes(evidence_salt)
        self._artifact_writer = artifact_writer

    def run(
        self,
        request: ExecutionRunRequest,
        understanding: UnderstandingOutput,
        source_adapter: SourceAdapter,
        *,
        column_overrides: Mapping[str, str] | None = None,
    ) -> ExecutionOutput:
        """Run aggregation, filtering, features and handlers in one source pass."""

        started = perf_counter()
        run_id = request.run_id or f"exec-{uuid.uuid4().hex}"
        self._validate_handoff(request, understanding, source_adapter)
        inspection = source_adapter.inspect()
        resolution = self._aggregation_resolver.resolve_with_diagnostics(
            request.aggregation,
            understanding,
            column_overrides=column_overrides,
        )
        aggregation = resolution.specification
        approved = {
            item.rule_id
            for item in understanding.rule_applicability
            if item.approved
            and not item.suggested_by_llm
            and item.status == RuleApplicabilityStatus.APPLICABLE.value
            and item.execution_readiness == ExecutionReadiness.READY.value
        }
        plan_resolution = self._handler_registry.resolve_plan(
            request.execution_plan,
            approved_rule_ids=approved,
        )
        warnings = self._initial_warnings(
            request,
            understanding,
            resolution.warnings,
            plan_resolution.issues,
        )
        handler_limits = HandlerLimits(
            maximum_categories_per_group=self._config.maximum_categories_per_rule_group,
            maximum_warnings=self._config.maximum_warning_count,
            decimal_places=self._config.decimal_places,
        )
        active: dict[str, tuple[ResolvedHandlerStep, HandlerState, float]] = {}
        failed_results: dict[str, RuleExecutionResult] = {}
        for item in plan_resolution.runnable:
            try:
                active[item.step.step_id] = (
                    item,
                    item.handler.prepare(item.step, aggregation, handler_limits),
                    perf_counter(),
                )
            except Exception as exc:
                failed_results[item.step.step_id] = self._failed_result(item.step, exc)
                if not self._config.continue_on_rule_failure or request.options.fail_fast:
                    raise ExecutionCoordinatorError(
                        "HANDLER_PREPARATION_FAILED",
                        "A selected rule handler could not be prepared.",
                    ) from exc

        requirements = self._union_requirements(
            tuple(item.handler for item, _, _ in active.values())
        )
        all_targets = {
            column
            for item, _, _ in active.values()
            for column in item.step.target_columns
        }
        aggregation_columns = {
            *(dimension.column for dimension in aggregation.dimensions),
            *(item.column for item in aggregation.filters),
        }
        if aggregation.time_column:
            aggregation_columns.add(aggregation.time_column)
        preliminary = self._feature_extractor.resolve_bindings(
            column_names=inspection.column_names,
            requirements=requirements,
            domains=understanding.domains,
            overrides=column_overrides,
        )
        projection = sorted(
            (all_targets | aggregation_columns) - {preliminary.transcript},
            key=str.casefold,
        )
        bindings = self._feature_extractor.resolve_bindings(
            column_names=inspection.column_names,
            requirements=requirements,
            domains=understanding.domains,
            overrides=column_overrides,
            projection_columns=projection,
        )
        evidence = EvidenceCollector(
            salt=self._evidence_salt,
            limits=EvidenceLimits(
                maximum_per_rule_group=request.options.max_evidence_per_rule_group,
                maximum_total=request.options.max_total_evidence,
                maximum_buckets=min(
                    request.options.max_total_evidence,
                    max(1, aggregation.maximum_groups * max(1, len(active))),
                ),
            ),
        )
        key_factory = AggregationKeyFactory(aggregation)
        rows_read = 0
        rows_after_filters = 0
        chunks_processed = 0
        try:
            for chunk in source_adapter.iter_chunks():
                chunks_processed += 1
                rows_read += chunk.row_count
                feature_stream = self._feature_extractor.extract_chunk(
                    chunk,
                    bindings=bindings,
                    requirements=requirements,
                )
                for row in feature_stream:
                    if not self._matches_filters(row, aggregation.filters):
                        continue
                    rows_after_filters += 1
                    group_keys = self._evidence_keys(key_factory, aggregation, row)
                    for step_id in tuple(active):
                        item, state, handler_started = active[step_id]
                        before = self._overall_failed_count(state)
                        try:
                            item.handler.observe(state, row)
                            after = self._overall_failed_count(state)
                            if after > before:
                                category = self._failure_category(item.step.check_name, row)
                                evidence.observe_failure(
                                    step=item.step,
                                    aggregation_keys=group_keys,
                                    source_fingerprint=request.source.fingerprint,
                                    row_ordinal=row.row_ordinal,
                                    failure_type="rule_failure",
                                    category=category,
                                    metadata={"check_name": str(item.step.check_name)},
                                )
                        except Exception as exc:
                            failed_results[step_id] = self._failed_result(
                                item.step,
                                exc,
                                elapsed_seconds=perf_counter() - handler_started,
                            )
                            del active[step_id]
                            if not self._config.continue_on_rule_failure or request.options.fail_fast:
                                raise
        except ExecutionCoordinatorError:
            raise
        except Exception as exc:
            raise ExecutionCoordinatorError(
                "EXECUTION_STREAM_FAILED",
                f"Execution stream failed ({type(exc).__name__}).",
                retryable=False,
            ) from exc
        finally:
            source_adapter.close()

        completed_results: dict[str, RuleExecutionResult] = {}
        for step_id, (item, state, handler_started) in active.items():
            try:
                completed_results[step_id] = item.handler.finalize(
                    state,
                    elapsed_seconds=perf_counter() - handler_started,
                )
            except Exception as exc:
                failed_results[step_id] = self._failed_result(
                    item.step,
                    exc,
                    elapsed_seconds=perf_counter() - handler_started,
                )
        issue_results = {
            issue.step_id: self._issue_result(issue, request.execution_plan)
            for issue in plan_resolution.issues
        }
        all_results_by_step = {
            **completed_results,
            **failed_results,
            **issue_results,
        }
        ordered_results = [
            all_results_by_step[step.step_id]
            for step in request.execution_plan.steps
            if step.step_id in all_results_by_step
        ]
        evidence_collections = list(evidence.finalize())
        elapsed = perf_counter() - started
        summary = self._summary(
            request,
            ordered_results,
            rows_read,
            rows_after_filters,
            chunks_processed,
            aggregation,
            evidence,
            elapsed,
        )
        metric_availability = self._metric_availability(
            ordered_results, rows_after_filters
        )
        status = self._status(ordered_results, warnings)
        output = ExecutionOutput(
            run_id=run_id,
            understanding_run_id=understanding.run_id,
            status=status,
            source=request.source,
            execution_plan_id=request.execution_plan.plan_id,
            registry_fingerprint=request.execution_plan.registry_fingerprint,
            aggregation=aggregation,
            summary=summary,
            metric_availability=metric_availability,
            rule_results=ordered_results,
            evidence=evidence_collections,
            warnings=warnings,
        )
        if request.options.persist_artifacts:
            if self._artifact_writer is None:
                raise ExecutionCoordinatorError(
                    "ARTIFACT_WRITER_NOT_CONFIGURED",
                    "Artifact persistence was requested but no writer is configured.",
                )
            output = self._artifact_writer.write(output).result
        return output

    def _validate_handoff(
        self,
        request: ExecutionRunRequest,
        understanding: UnderstandingOutput,
        source_adapter: SourceAdapter,
    ) -> None:
        if understanding.status == "failed":
            raise ExecutionCoordinatorError(
                "UNDERSTANDING_FAILED",
                "Execution cannot run from a failed Understanding result.",
            )
        if request.understanding_run_id != understanding.run_id:
            raise ExecutionCoordinatorError(
                "UNDERSTANDING_RUN_MISMATCH",
                "Execution request does not reference the supplied Understanding run.",
            )
        if request.execution_plan.plan_id != understanding.execution_plan.plan_id:
            raise ExecutionCoordinatorError(
                "EXECUTION_PLAN_MISMATCH",
                "Execution request plan differs from the supplied Understanding output.",
            )
        if request.source.source_id != source_adapter.metadata.source_id:
            raise ExecutionCoordinatorError(
                "SOURCE_ID_MISMATCH",
                "Execution source does not match the configured source adapter.",
            )
        if (
            self._config.require_source_fingerprint_match
            and source_adapter.metadata.fingerprint
            and source_adapter.metadata.fingerprint != request.source.fingerprint
        ):
            raise ExecutionCoordinatorError(
                "SOURCE_FINGERPRINT_MISMATCH",
                "Execution source fingerprint differs from Understanding provenance.",
            )

    def _initial_warnings(
        self,
        request: ExecutionRunRequest,
        understanding: UnderstandingOutput,
        resolution_warnings: tuple[str, ...],
        issues: tuple[HandlerResolutionIssue, ...],
    ) -> list[ExecutionWarning]:
        warnings = [
            ExecutionWarning(code="AGGREGATION_RESOLUTION_WARNING", message=message)
            for message in resolution_warnings
        ]
        warnings.extend(
            ExecutionWarning(
                code=issue.code,
                message=issue.message,
                rule_id=issue.rule_id,
            )
            for issue in issues
        )
        if request.options.parallel_workers > 1:
            warnings.append(
                ExecutionWarning(
                    code="SERIAL_EXECUTION_USED",
                    message="The MVP coordinator uses deterministic serial chunk execution.",
                    severity=Severity.INFO,
                )
            )
        for message in understanding.execution_plan.warnings:
            warnings.append(
                ExecutionWarning(
                    code="UNDERSTANDING_PLAN_WARNING",
                    message=message,
                    severity=Severity.LOW,
                )
            )
        return warnings[: self._config.maximum_warning_count]

    @staticmethod
    def _union_requirements(handlers: tuple[RuleHandler, ...]) -> FeatureRequirements:
        requirements = tuple(handler.feature_requirements for handler in handlers)
        return FeatureRequirements(
            tokenize=any(item.tokenize for item in requirements),
            inspect_speaker_tags=any(item.inspect_speaker_tags for item in requirements),
            inspect_pii=any(item.inspect_pii for item in requirements),
            inspect_mistranslations=any(item.inspect_mistranslations for item in requirements),
            calculate_speech_statistics=any(item.calculate_speech_statistics for item in requirements),
        )

    @staticmethod
    def _matches_filters(row: RowFeatures, filters: list[FilterSpecification]) -> bool:
        return all(ExecutionCoordinator._matches_filter(row.value(item.column), item) for item in filters)

    @staticmethod
    def _matches_filter(value: object, item: FilterSpecification) -> bool:
        operator = str(item.operator)
        expected = item.values
        if operator == FilterOperator.IS_NULL.value:
            return value is None
        if operator == FilterOperator.IS_NOT_NULL.value:
            return value is not None
        if operator == FilterOperator.EQUALS.value:
            return value == expected[0]
        if operator == FilterOperator.NOT_EQUALS.value:
            return value != expected[0]
        if operator == FilterOperator.IN.value:
            return value in expected
        if operator == FilterOperator.NOT_IN.value:
            return value not in expected
        if value is None:
            return False
        try:
            if operator == FilterOperator.GREATER_THAN.value:
                return value > expected[0]
            if operator == FilterOperator.GREATER_THAN_OR_EQUAL.value:
                return value >= expected[0]
            if operator == FilterOperator.LESS_THAN.value:
                return value < expected[0]
            if operator == FilterOperator.LESS_THAN_OR_EQUAL.value:
                return value <= expected[0]
            if operator == FilterOperator.BETWEEN.value:
                return expected[0] <= value <= expected[1]
        except TypeError:
            return False
        return False

    @staticmethod
    def _evidence_keys(
        factory: AggregationKeyFactory,
        aggregation: ResolvedAggregationSpecification,
        row: RowFeatures,
    ) -> tuple[InternalAggregationKey, ...]:
        keys: list[InternalAggregationKey] = []
        if aggregation.include_overall:
            keys.append(InternalAggregationKey.overall_key())
        if aggregation.time_column or aggregation.dimensions:
            resolution = factory.build(row.values)
            if resolution.key is not None:
                keys.append(resolution.key)
        return tuple(keys)

    @staticmethod
    def _overall_failed_count(state: HandlerState) -> int:
        groups = state.groups._groups  # noqa: SLF001 - execution subsystem boundary
        overall = groups.get(InternalAggregationKey.overall_key())
        return overall.counts.failed_count if overall is not None else 0

    @staticmethod
    def _failure_category(check_name: object, row: RowFeatures) -> str:
        name = str(check_name)
        if name == "pii_detection" and row.transcript.pii_category_counts:
            return "pii_category_detected"
        if name == "mistranslated_rate" and row.transcript.mistranslation_category_counts:
            return "configured_mistranslation"
        if name == "agent_speaker_tag_validation":
            speaker = row.transcript.speaker
            if speaker.total_turns == 0:
                return "no_speaker_tags"
            if not speaker.required_roles_present:
                return "required_role_missing"
            if speaker.unknown_label_count:
                return "unknown_speaker_label"
            if speaker.empty_turn_count:
                return "empty_speaker_turn"
        if name == "fill_rate":
            return "missing_required_value"
        if name == "speech_per_duration_rate":
            return "speech_duration_outlier"
        if name == "non_english_spelling":
            return "unknown_token_rate_exceeded"
        return "rule_failure"

    @staticmethod
    def _failed_result(
        step: Any,
        exc: Exception,
        *,
        elapsed_seconds: float = 0.0,
    ) -> RuleExecutionResult:
        return RuleExecutionResult(
            step_id=step.step_id,
            rule_id=step.rule_id,
            rule_version=step.rule_version,
            check_name=step.check_name,
            status=RuleExecutionStatus.FAILED,
            target_columns=step.target_columns,
            elapsed_seconds=max(0.0, elapsed_seconds),
            errors=[f"{getattr(exc, 'code', type(exc).__name__)}: {getattr(exc, 'message', 'Rule execution failed.')}"],
        )

    @staticmethod
    def _issue_result(issue: HandlerResolutionIssue, plan: Any) -> RuleExecutionResult:
        step = next(item for item in plan.steps if item.step_id == issue.step_id)
        status = (
            RuleExecutionStatus.IMPLEMENTATION_PENDING
            if issue.code == "HANDLER_NOT_AVAILABLE"
            else RuleExecutionStatus.SKIPPED
        )
        return RuleExecutionResult(
            step_id=step.step_id,
            rule_id=step.rule_id,
            rule_version=step.rule_version,
            check_name=step.check_name,
            status=status,
            target_columns=step.target_columns,
            warnings=[f"{issue.code}: {issue.message}"],
        )

    @staticmethod
    def _metric_availability(
        results: list[RuleExecutionResult], rows_after_filters: int
    ) -> list[MetricAvailability]:
        availability: list[MetricAvailability] = []
        for result in results:
            if result.status in {
                RuleExecutionStatus.FAILED.value,
                RuleExecutionStatus.SKIPPED.value,
                RuleExecutionStatus.IMPLEMENTATION_PENDING.value,
            }:
                availability.append(
                    MetricAvailability(
                        rule_id=result.rule_id,
                        metric_name=str(result.check_name),
                        status=MetricAvailabilityStatus.UNAVAILABLE,
                        reason=result.errors[0] if result.errors else result.warnings[0],
                    )
                )
                continue
            overall = next((group for group in result.groups if group.aggregation_key.overall), None)
            eligible = overall.eligible_count if overall else 0
            unavailable = max(0, rows_after_filters - eligible)
            status = (
                MetricAvailabilityStatus.UNAVAILABLE
                if eligible == 0
                else MetricAvailabilityStatus.PARTIAL
                if unavailable
                else MetricAvailabilityStatus.AVAILABLE
            )
            availability.append(
                MetricAvailability(
                    rule_id=result.rule_id,
                    metric_name=str(result.check_name),
                    status=status,
                    reason="No eligible rows were available." if eligible == 0 else None,
                    eligible_count=eligible,
                    unavailable_count=unavailable,
                    coverage=eligible / rows_after_filters if rows_after_filters else 0.0,
                )
            )
        return availability

    @staticmethod
    def _summary(
        request: ExecutionRunRequest,
        results: list[RuleExecutionResult],
        rows_read: int,
        rows_after_filters: int,
        chunks_processed: int,
        aggregation: ResolvedAggregationSpecification,
        evidence: EvidenceCollector,
        elapsed: float,
    ) -> ExecutionSummary:
        statuses = [str(item.status) for item in results]
        group_keys = {
            repr(group.aggregation_key.model_dump(mode="json"))
            for result in results
            for group in result.groups
            if not group.aggregation_key.overall
        }
        return ExecutionSummary(
            rows_read=rows_read,
            rows_after_filters=rows_after_filters,
            chunks_processed=chunks_processed,
            groups_created=len(group_keys),
            rules_requested=len(request.execution_plan.steps),
            rules_executed=sum(status not in {RuleExecutionStatus.SKIPPED.value, RuleExecutionStatus.IMPLEMENTATION_PENDING.value} for status in statuses),
            rules_completed=sum(status in {RuleExecutionStatus.COMPLETED.value, RuleExecutionStatus.COMPLETED_WITH_WARNINGS.value} for status in statuses),
            rules_skipped=sum(status in {RuleExecutionStatus.SKIPPED.value, RuleExecutionStatus.IMPLEMENTATION_PENDING.value} for status in statuses),
            rules_failed=sum(status == RuleExecutionStatus.FAILED.value for status in statuses),
            evidence_observed=evidence.observed_count,
            evidence_retained=evidence.retained_count,
            elapsed_seconds=elapsed,
        )

    @staticmethod
    def _status(
        results: list[RuleExecutionResult], warnings: list[ExecutionWarning]
    ) -> ExecutionStatus:
        statuses = {str(item.status) for item in results}
        if RuleExecutionStatus.FAILED.value in statuses or RuleExecutionStatus.IMPLEMENTATION_PENDING.value in statuses:
            return ExecutionStatus.PARTIAL
        if warnings or RuleExecutionStatus.COMPLETED_WITH_WARNINGS.value in statuses or RuleExecutionStatus.SKIPPED.value in statuses:
            return ExecutionStatus.COMPLETED_WITH_WARNINGS
        return ExecutionStatus.COMPLETED


__all__ = [
    "ExecutionCoordinator",
    "ExecutionCoordinatorConfig",
    "ExecutionCoordinatorError",
]
