"""Composition service for the end-to-end UnDQ pipeline."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

from app.execution.models import AggregationRequest, ExecutionOptions
from app.models.responses import (
    BusinessImpactLLMOptions,
    BusinessImpactOptions,
    BusinessImpactRequest,
    EngineResponse,
    LLMOptions,
    ServiceResult,
)
from app.business_impact.impact.models import BusinessContext
from app.services.business_impact import BusinessImpactService, BusinessImpactServiceError
from app.services.execution import ExecutionSourceRequest, execute_rules
from app.services.understanding import UnderstandingSourceRequest, run_understanding


class EngineServiceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def run_end_to_end(
    source: UnderstandingSourceRequest | str | Path,
    llm: LLMOptions | None = None,
    *,
    aggregation: AggregationRequest | None = None,
    execution_options: ExecutionOptions | None = None,
    column_overrides: Mapping[str, str] | None = None,
    selected_rule_ids: Sequence[str] | None = None,
    include_business_impact: bool = False,
    business_context: BusinessContext | None = None,
    impact_options: BusinessImpactOptions | None = None,
    impact_llm: BusinessImpactLLMOptions | None = None,
    business_impact_service: BusinessImpactService | None = None,
) -> EngineResponse:
    """Run Understanding → Execution and optionally an isolated Business Impact branch."""

    if not isinstance(source, UnderstandingSourceRequest):
        raise EngineServiceError(
            "ENGINE_SOURCE_PROVENANCE_REQUIRED",
            "End-to-end execution requires a staged UnderstandingSourceRequest.",
        )
    llm = llm or LLMOptions()
    understanding = run_understanding(source, llm)
    provenance = understanding.input_provenance
    if provenance is None:
        raise EngineServiceError(
            "ENGINE_SOURCE_PROVENANCE_REQUIRED",
            "Understanding did not return verified source provenance.",
        )
    execution = execute_rules(
        understanding,
        ExecutionSourceRequest(
            csv_path=source.csv_path,
            original_filename=source.original_filename,
            source_id=source.source_id,
            size_bytes=provenance.sample_csv.size_bytes,
            sha256=provenance.sample_csv.sha256,
            source_version=source.source_version,
        ),
        aggregation=aggregation,
        options=execution_options,
        column_overrides=column_overrides,
        selected_rule_ids=selected_rule_ids,
        llm=llm,
    )
    impact_result = None
    impact_error = None
    if include_business_impact:
        try:
            if impact_llm is None:
                raise EngineServiceError("IMPACT_LLM_SELECTION_REQUIRED", "A configured LLM provider is required when Business Impact is requested.")
            if business_impact_service is None:
                raise EngineServiceError("BUSINESS_IMPACT_NOT_CONFIGURED", "Business Impact service composition is not configured.")
            if understanding.output is None:
                raise EngineServiceError("UNDERSTANDING_CANONICAL_OUTPUT_REQUIRED", "Business Impact requires the canonical UnderstandingOutput handoff.")
            impact_result = business_impact_service.analyze(
                BusinessImpactRequest(
                    understanding=understanding.output,
                    execution=execution.output,
                    business_context=business_context,
                    options=impact_options or BusinessImpactOptions(),
                    llm=impact_llm,
                )
            )
        except (BusinessImpactServiceError, EngineServiceError) as exc:
            impact_error = ServiceResult(
                status="partial", message=getattr(exc, "message", str(exc)), input_file=source.original_filename,
                details={"code": getattr(exc, "code", "BUSINESS_IMPACT_PROCESSING_FAILED")},
            )
        except Exception as exc:
            impact_error = ServiceResult(
                status="partial", message="Business Impact failed; Understanding and Execution completed successfully.",
                input_file=source.original_filename,
                details={"code": "BUSINESS_IMPACT_PROCESSING_FAILED", "exception_type": type(exc).__name__},
            )
    return EngineResponse(
        request_id=llm.request_id,
        status=str(execution.output.status),
        understanding=understanding,
        execution=execution,
        business_impact=impact_result,
        business_impact_error=impact_error,
    )
