"""Multipart API for standalone deterministic Execution runs."""

from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from starlette.concurrency import run_in_threadpool

from app.api.routes._execution_inputs import (
    parse_aggregation,
    parse_column_overrides,
    parse_execution_options,
    parse_selected_rule_ids,
    parse_understanding,
    stage_execution_source,
)
from app.core.config import settings
from app.models.responses import ComponentStatusResponse, ExecutionEnvelope, LLMOptions
from app.services.execution import execute_rules


router = APIRouter(prefix=settings.api_prefix, tags=["execution"])


@router.get("/execution", response_model=ComponentStatusResponse)
def execution_status() -> ComponentStatusResponse:
    return ComponentStatusResponse(
        component="execution",
        message="Execution API is healthy and accepts a verified CSV plus Understanding output.",
        processing_method="POST multipart/form-data",
    )


@router.post("/execution", response_model=ExecutionEnvelope)
async def execution(
    sample_csv: UploadFile = File(...),
    understanding_json: str = Form(...),
    aggregation_json: str = Form("{}"),
    execution_options_json: str | None = Form(None),
    column_overrides_json: str = Form("{}"),
    selected_rule_ids_json: str | None = Form(None),
    source_id: str | None = Form(None),
    source_version: str | None = Form(None),
    llm_enabled: bool = Form(False),
    llm_provider: str = Form("launchpad"),
    llm_model: str | None = Form(None),
    request_id: str = Form("1"),
) -> ExecutionEnvelope:
    understanding = parse_understanding(understanding_json)
    aggregation = parse_aggregation(aggregation_json)
    options = parse_execution_options(execution_options_json)
    overrides = parse_column_overrides(column_overrides_json)
    selected_rule_ids = parse_selected_rule_ids(selected_rule_ids_json)
    async with stage_execution_source(
        sample_csv,
        source_id=source_id,
        source_version=source_version,
    ) as source:
        try:
            result = await run_in_threadpool(
                execute_rules,
                understanding,
                source,
                aggregation=aggregation,
                options=options,
                column_overrides=overrides,
                selected_rule_ids=selected_rule_ids,
                llm=LLMOptions(
                    enabled=llm_enabled,
                    provider=llm_provider,
                    model=llm_model,
                    request_id=request_id,
                ),
            )
            return ExecutionEnvelope(understanding=understanding, execution=result)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": getattr(exc, "code", "EXECUTION_PROCESSING_FAILED"),
                    "message": getattr(
                        exc,
                        "message",
                        f"Execution processing failed ({type(exc).__name__}).",
                    ),
                },
            ) from exc
