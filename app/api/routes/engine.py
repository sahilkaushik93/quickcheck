"""Multipart end-to-end engine API with execution aggregation controls."""

from __future__ import annotations

import json

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app.api.routes._execution_inputs import (
    parse_aggregation,
    parse_column_overrides,
    parse_execution_options,
    parse_selected_rule_ids,
)
from app.api.routes._understanding_inputs import build_source_request, select_sample_csv
from app.api.routes._uploads import stage_understanding_inputs
from app.core.config import settings
from app.business_impact.impact.models import BusinessContext
from app.models.responses import (
    BusinessImpactLLMOptions,
    BusinessImpactOptions,
    ComponentStatusResponse,
    EngineResponse,
    LLMOptions,
)
from app.services.business_impact import BusinessImpactService
from app.execution.models import ExecutionOptions
from app.services.engine import run_end_to_end


router = APIRouter(prefix=settings.api_prefix, tags=["engine"])


@router.get("/engine", response_model=ComponentStatusResponse)
def engine_status() -> ComponentStatusResponse:
    return ComponentStatusResponse(
        component="end-to-end-engine",
        message="End-to-end engine is healthy and accepts data, metadata, rules and aggregation controls.",
        processing_method="POST multipart/form-data",
    )


@router.post("/engine", response_model=EngineResponse)
async def engine(
    request: Request,
    sample_csv: UploadFile | None = File(None),
    metadata_dictionary: UploadFile = File(...),
    rule_files: list[UploadFile] | None = File(None),
    rules_archive: UploadFile | None = File(None),
    file: UploadFile | None = File(None, description="Deprecated alias for sample_csv."),
    dictionary_version: str | None = Form(None),
    run_id: str | None = Form(None),
    persist_artifacts: bool = Form(True),
    aggregation_json: str = Form("{}"),
    execution_options_json: str | None = Form(None),
    column_overrides_json: str = Form("{}"),
    selected_rule_ids_json: str | None = Form(None),
    llm_enabled: bool = Form(False),
    llm_provider: str = Form("launchpad"),
    llm_model: str | None = Form(None),
    request_id: str = Form("1"),
    include_business_impact: bool = Form(False),
    business_context_json: str | None = Form(None),
    impact_options_json: str | None = Form(None),
    impact_llm_provider: str | None = Form(None),
    impact_llm_model: str | None = Form(None),
) -> EngineResponse:
    selected_csv = select_sample_csv(sample_csv, file)
    aggregation = parse_aggregation(aggregation_json)
    execution_options = parse_execution_options(execution_options_json)
    execution_options = (execution_options or ExecutionOptions()).model_copy(
        update={"persist_artifacts": persist_artifacts}
    )
    overrides = parse_column_overrides(column_overrides_json)
    selected_rule_ids = parse_selected_rule_ids(selected_rule_ids_json)
    business_context, impact_options, impact_llm = _parse_business_impact_inputs(
        include_business_impact=include_business_impact,
        business_context_json=business_context_json,
        impact_options_json=impact_options_json,
        provider=impact_llm_provider,
        model=impact_llm_model,
        request_id=request_id,
    )
    async with stage_understanding_inputs(
        sample_csv=selected_csv,
        metadata_dictionary=metadata_dictionary,
        rule_files=rule_files,
        rules_archive=rules_archive,
    ) as staged:
        source = build_source_request(
            staged,
            run_id=run_id,
            dictionary_version=dictionary_version,
            persist_artifacts=persist_artifacts,
        )
        try:
            return await run_in_threadpool(
                run_end_to_end,
                source,
                LLMOptions(
                    enabled=llm_enabled,
                    provider=llm_provider,
                    model=llm_model,
                    request_id=request_id,
                ),
                aggregation=aggregation,
                execution_options=execution_options,
                column_overrides=overrides,
                selected_rule_ids=selected_rule_ids,
                include_business_impact=include_business_impact,
                business_context=business_context,
                impact_options=impact_options,
                impact_llm=impact_llm,
                business_impact_service=getattr(request.app.state, "business_impact_service", None),
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": getattr(exc, "code", "ENGINE_PROCESSING_FAILED"),
                    "message": getattr(
                        exc,
                        "message",
                        f"Engine processing failed ({type(exc).__name__}).",
                    ),
                },
            ) from exc


def _parse_business_impact_inputs(
    *, include_business_impact: bool, business_context_json: str | None,
    impact_options_json: str | None, provider: str | None, model: str | None, request_id: str,
) -> tuple[BusinessContext | None, BusinessImpactOptions | None, BusinessImpactLLMOptions | None]:
    """Validate optional multipart values without accepting source data or URLs."""

    if not include_business_impact:
        return None, None, None
    if not provider or not provider.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "IMPACT_LLM_SELECTION_REQUIRED", "message": "impact_llm_provider is required when include_business_impact=true."},
        )
    try:
        context = BusinessContext.model_validate(json.loads(business_context_json)) if business_context_json else None
        options = BusinessImpactOptions.model_validate(json.loads(impact_options_json or "{}"))
        llm = BusinessImpactLLMOptions(provider=provider, model=model, request_id=request_id)
        return context, options, llm
    except (json.JSONDecodeError, ValidationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "IMPACT_INPUT_INVALID", "message": "Business Impact context or options are invalid."},
        ) from exc
