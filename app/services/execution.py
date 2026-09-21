"""Application service for deterministic, streaming Execution runs."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from pydantic import ValidationError

from app.execution.aggregation.models import AggregationPolicy
from app.execution.aggregation.resolver import AggregationResolver
from app.execution.coordinator import ExecutionCoordinator, ExecutionCoordinatorConfig
from app.execution.features.extractor import FeatureExtractor, FeatureExtractorConfig
from app.execution.handlers.non_english_spelling import NonEnglishSpellingHandler
from app.execution.handlers.registry import HandlerRegistry
from app.execution.llm_sampling import enrich_sampled_transcripts
from app.execution.models import (
    AggregationRequest,
    ExecutionOptions,
    ExecutionRunRequest,
    ExecutionSourceIdentity,
)
from app.execution.storage.artifact_writer import ExecutionArtifactWriter
from app.execution.presentation import build_observed_results
from app.execution.rule_ids import normalize_rule_id
from app.models.responses import ExecutionResult, LLMOptions, ServiceResult, UnderstandingResult
from app.understanding.models import (
    ColumnProfile,
    DatasetProfile,
    ExecutionPlan,
    LogicalDataType,
    MetadataKnowledgeBase,
    ProcessingLimits,
    SourceMetadata,
    SourceType,
    UnderstandingOutput,
)
from app.understanding.source_adapters.csv_adapter import CSVReadOptions, CSVSourceAdapter


class ExecutionServiceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ExecutionSourceRequest:
    csv_path: Path
    original_filename: str
    source_id: str
    size_bytes: int
    sha256: str
    source_version: str | None = None


def execute_rules(
    understanding: UnderstandingResult,
    source: ExecutionSourceRequest,
    *,
    aggregation: AggregationRequest | None = None,
    options: ExecutionOptions | None = None,
    column_overrides: Mapping[str, str] | None = None,
    llm: LLMOptions | None = None,
    selected_rule_ids: Sequence[str] | None = None,
) -> ExecutionResult:
    """Execute Understanding's approved plan over one bounded CSV stream."""

    if understanding.run_id is None or understanding.execution_plan is None:
        raise ExecutionServiceError(
            "UNDERSTANDING_HANDOFF_INCOMPLETE",
            "Understanding run_id and execution_plan are required for execution.",
        )
    _validate_source_provenance(understanding, source)
    root = _repository_root()
    execution_config = _read_json(root / "config/execution/execution_config.json")
    language_config = _read_json(root / "config/execution/language_policy.json")
    aggregation_policy = AggregationPolicy.from_mapping(
        _read_json(root / "config/execution/aggregation_policy.json")
    )
    feature_config = FeatureExtractorConfig.from_mappings(
        execution=execution_config,
        language=language_config,
        speaker_tags=_read_json(root / "config/execution/speaker_tags.json"),
        pii=_read_json(root / "config/execution/pii_patterns.json"),
        mistranslations=_read_json(root / "config/execution/mistranslation_dictionary.json"),
    )
    runtime_options = options or _default_options(execution_config)
    limits = _processing_limits(runtime_options.chunk_size, execution_config)
    source_metadata = SourceMetadata(
        source_id=source.source_id,
        source_type=SourceType.CSV,
        display_name=source.original_filename,
        format="csv",
        declared_size_bytes=source.size_bytes,
        source_version=source.source_version,
        fingerprint=source.sha256,
    )
    execution_plan = _select_execution_plan(
        understanding.execution_plan, selected_rule_ids
    )
    canonical = _canonical_understanding(understanding, source_metadata, limits).model_copy(
        update={"execution_plan": execution_plan}
    )
    request = ExecutionRunRequest(
        understanding_run_id=understanding.run_id,
        source=ExecutionSourceIdentity(
            source_id=source.source_id,
            source_type=SourceType.CSV,
            display_name=source.original_filename,
            fingerprint=source.sha256,
            declared_size_bytes=source.size_bytes,
            source_version=source.source_version,
        ),
        execution_plan=execution_plan,
        aggregation=aggregation or AggregationRequest(),
        options=runtime_options,
    )
    registry = HandlerRegistry().with_handler(
        NonEnglishSpellingHandler(
            _governed_vocabulary(root / "config/execution/business_glossary.json"),
            minimum_evaluated_tokens=int(
                language_config.get("thresholds", {}).get("minimum_evaluated_tokens", 20)
            ),
            minimum_vocabulary_size=int(
                language_config.get("thresholds", {}).get("minimum_vocabulary_size", 500)
            ),
        )
    )
    salt = os.getenv("UNDQ_EVIDENCE_HASH_SALT", "").encode("utf-8")
    if not salt:
        raise ExecutionServiceError(
            "EVIDENCE_SALT_REQUIRED",
            "UNDQ_EVIDENCE_HASH_SALT must be configured before execution.",
        )
    coordinator = ExecutionCoordinator(
        config=ExecutionCoordinatorConfig.from_mapping(execution_config),
        aggregation_resolver=AggregationResolver(aggregation_policy),
        feature_extractor=FeatureExtractor(feature_config),
        handler_registry=registry,
        evidence_salt=salt,
        artifact_writer=ExecutionArtifactWriter(
            Path(os.getenv("UNDQ_EXECUTION_OUTPUT_ROOT", root / "data/execution_layer"))
        ),
    )
    adapter = CSVSourceAdapter(
        source.csv_path,
        source_metadata,
        limits,
        CSVReadOptions.from_mapping(execution_config.get("source_validation", {})),
    )
    try:
        output = coordinator.run(
            request,
            canonical,
            adapter,
            column_overrides=dict(column_overrides or {}),
        )
    except Exception as exc:
        raise ExecutionServiceError(
            getattr(exc, "code", "EXECUTION_SERVICE_FAILED"),
            getattr(exc, "message", f"Execution failed ({type(exc).__name__})."),
        ) from exc
    observed = build_observed_results(output)
    ontology = _read_json(root / "config/execution/transcript_dq_ontology.json")
    llm_assessment = enrich_sampled_transcripts(
        source.csv_path,
        llm=llm or LLMOptions(),
        source_fingerprint=source.sha256,
        column_aliases={
            str(role): [str(item) for item in values]
            for role, values in ontology.get("semantic_roles", {}).items()
            if isinstance(values, list)
        },
        column_overrides=dict(column_overrides or {}),
        policy_document=_read_json(root / "config/execution/llm_sampling_policy.json"),
        chunk_size=runtime_options.chunk_size,
    )
    observed["llm_sample_assessment"] = llm_assessment
    return ExecutionResult(
        Observed_Results=ServiceResult(
            status=output.status,
            message="DQ rule execution completed.",
            input_file=source.original_filename,
            details={
                "run_id": output.run_id,
                "execution_plan_id": output.execution_plan_id,
                "summary": output.summary.model_dump(mode="json"),
                "metric_availability": [
                    item.model_dump(mode="json") for item in output.metric_availability
                ],
                "rule_result_count": len(output.rule_results),
                "evidence_collection_count": len(output.evidence),
                **observed,
            },
        ),
        output=output,
    )


def _select_execution_plan(
    plan: ExecutionPlan, requested: Sequence[str] | None
) -> ExecutionPlan:
    """Return a dependency-complete plan restricted to explicitly selected rules."""

    if requested is None:
        return plan
    try:
        runtime_ids = {normalize_rule_id(value) for value in requested}
    except ValueError as exc:
        raise ExecutionServiceError("UNSUPPORTED_RULE_ID", str(exc)) from exc
    by_step = {step.step_id: step for step in plan.steps}
    selected_steps = [step for step in plan.steps if step.rule_id in runtime_ids]
    missing = runtime_ids - {step.rule_id for step in selected_steps}
    if missing:
        raise ExecutionServiceError(
            "RULE_NOT_RUNNABLE",
            "Selected rule is not present as a runnable Understanding execution step: "
            + ", ".join(sorted(missing)),
        )
    required_step_ids = {step.step_id for step in selected_steps}
    pending = list(required_step_ids)
    while pending:
        step = by_step[pending.pop()]
        for dependency in step.dependencies:
            if dependency not in required_step_ids:
                required_step_ids.add(dependency)
                pending.append(dependency)
    steps = [step for step in plan.steps if step.step_id in required_step_ids]
    selected = list(dict.fromkeys(step.rule_id for step in steps))
    return plan.model_copy(
        update={
            "steps": steps,
            "selected_rules": selected,
            "implementation_pending_rules": [],
            "blocked_rules": [],
            "warnings": [*plan.warnings, "Execution plan filtered by explicit API rule selection."],
        }
    )


def _canonical_understanding(
    result: UnderstandingResult,
    source: SourceMetadata,
    limits: ProcessingLimits,
) -> UnderstandingOutput:
    details = result.Profile.details
    try:
        columns = [ColumnProfile.model_validate(item) for item in details.get("columns", [])]
        if not columns:
            raise ValueError("profile columns are absent")
        columns = _apply_declared_temporal_types(columns, result)
        now = datetime.now(timezone.utc)
        profile = DatasetProfile(
            row_count=int(details["row_count"]),
            column_count=len(columns),
            chunks_processed=int(details.get("chunks_processed", 0)),
            started_at=now,
            completed_at=now,
            elapsed_seconds=float(details.get("elapsed_seconds", 0.0)),
            limits=limits,
            columns=columns,
        )
        return UnderstandingOutput(
            run_id=result.run_id or "",
            status=result.status,
            source=source,
            input_provenance=result.input_provenance,
            profile=profile,
            metadata_knowledge_base=result.metadata_knowledge_base or MetadataKnowledgeBase(),
            domains=result.domains,
            relationships=result.relationships,
            dq_signals=result.dq_signals,
            rule_applicability=result.rule_applicability,
            execution_plan=result.execution_plan,  # type: ignore[arg-type]
            llm_insights=result.llm_insights,
            warnings=result.warnings,
        )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise ExecutionServiceError(
            "UNDERSTANDING_PROFILE_INVALID",
            "Understanding profile cannot be converted to the Execution contract.",
        ) from exc


def _validate_source_provenance(
    understanding: UnderstandingResult, source: ExecutionSourceRequest
) -> None:
    provenance = understanding.input_provenance
    if provenance is None:
        raise ExecutionServiceError(
            "SOURCE_PROVENANCE_REQUIRED",
            "Execution requires Understanding input provenance for source verification.",
        )
    if provenance.sample_csv.sha256 != source.sha256:
        raise ExecutionServiceError(
            "SOURCE_FINGERPRINT_MISMATCH",
            "Uploaded CSV differs from the source assessed by Understanding.",
        )


def _apply_declared_temporal_types(
    columns: list[ColumnProfile], result: UnderstandingResult
) -> list[ColumnProfile]:
    """Reconcile CSV string inference with trusted metadata/domain time typing."""

    knowledge = result.metadata_knowledge_base or MetadataKnowledgeBase()
    declared = {
        key.casefold(): str(value).casefold()
        for key, value in knowledge.declared_types.items()
    }
    domains = {item.column_name.casefold(): item for item in result.domains}
    normalized: list[ColumnProfile] = []
    for column in columns:
        assignment = domains.get(column.column_name.casefold())
        declared_type = declared.get(column.column_name.casefold(), "")
        semantic = (assignment.semantic_type or "").casefold() if assignment else ""
        domain = assignment.domain.casefold() if assignment else ""
        inferred = str(column.inferred_data_type)
        if inferred not in {LogicalDataType.DATE.value, LogicalDataType.DATETIME.value}:
            if (
                "timestamp" in declared_type
                or "datetime" in declared_type
                or "timestamp" in semantic
            ):
                column = column.model_copy(
                    update={
                        "inferred_data_type": LogicalDataType.DATETIME.value,
                        "inferred_type_confidence": max(
                            column.inferred_type_confidence, 0.8
                        ),
                    }
                )
            elif (
                declared_type == "date"
                or semantic.endswith("_date")
                or domain == "interaction.time"
            ):
                column = column.model_copy(
                    update={
                        "inferred_data_type": LogicalDataType.DATE.value,
                        "inferred_type_confidence": max(
                            column.inferred_type_confidence, 0.8
                        ),
                    }
                )
        normalized.append(column)
    return normalized


def _default_options(config: Mapping[str, Any]) -> ExecutionOptions:
    runtime = config.get("runtime", {})
    bounded = config.get("bounded_state", {})
    return ExecutionOptions(
        chunk_size=int(runtime.get("chunk_size_rows", 10_000)),
        parallel_workers=int(runtime.get("parallel_workers", 1)),
        max_in_flight_chunks=int(runtime.get("max_in_flight_chunks", 1)),
        max_evidence_per_rule_group=int(bounded.get("max_evidence_per_rule_group", 5)),
        max_total_evidence=int(bounded.get("max_total_evidence", 1_000)),
        fail_fast=bool(runtime.get("fail_fast", False)),
    )


def _processing_limits(chunk_size: int, config: Mapping[str, Any]) -> ProcessingLimits:
    bounded = config.get("bounded_state", {})
    return ProcessingLimits(
        chunk_size=chunk_size,
        max_distinct_values=int(bounded.get("max_cardinality_per_dimension", 500)),
        max_top_values=int(bounded.get("max_categories_per_rule_group", 100)),
        max_samples_per_column=0,
        max_evidence_items=int(bounded.get("max_total_evidence", 1_000)),
        max_relationship_pairs=0,
    )


def _governed_vocabulary(path: Path) -> frozenset[str]:
    glossary = _read_json(path)
    values = [
        *glossary.get("terms", []),
        *glossary.get("acronyms", []),
        *glossary.get("product_terms", []),
        *glossary.get("organization_terms", []),
    ]
    return frozenset(
        token.casefold()
        for value in values
        for token in str(value).replace("-", " ").split()
        if token.strip()
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutionServiceError(
            "EXECUTION_CONFIGURATION_INVALID",
            f"Execution configuration {path.name!r} could not be loaded.",
        ) from exc
    if not isinstance(document, dict):
        raise ExecutionServiceError(
            "EXECUTION_CONFIGURATION_INVALID",
            f"Execution configuration {path.name!r} must contain a JSON object.",
        )
    return document


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]
