"""FastAPI endpoints for the complete uploaded Understanding workflow."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from starlette.concurrency import run_in_threadpool

from app.api.routes._understanding_inputs import build_source_request, select_sample_csv
from app.api.routes._uploads import stage_understanding_inputs
from app.core.config import settings
from app.models.responses import (
    ComponentStatusResponse,
    LLMOptions,
    ServiceResult,
    UnderstandingEnvelope,
)
from app.services.understanding import (
    UnderstandingServiceError,
    prepare_rules,
    profile_csv,
    run_understanding,
)
from app.services.understanding import UnderstandingSourceRequest


router = APIRouter(prefix=f"{settings.api_prefix}/understanding", tags=["understanding"])


@router.get("", response_model=ComponentStatusResponse)
def understanding_status() -> ComponentStatusResponse:
    return ComponentStatusResponse(
        component="understanding",
        message="Understanding API is healthy and accepts sample CSV, metadata dictionary and rule registry uploads.",
        processing_method="POST multipart/form-data",
    )


@router.post("", response_model=UnderstandingEnvelope)
async def understanding(
    sample_csv: UploadFile | None = File(None),
    metadata_dictionary: UploadFile = File(...),
    rule_files: list[UploadFile] | None = File(None),
    rules_archive: UploadFile | None = File(None),
    file: UploadFile | None = File(None, description="Deprecated alias for sample_csv."),
    dictionary_version: str | None = Form(None),
    run_id: str | None = Form(None),
    persist_artifacts: bool = Form(True),
    llm_enabled: bool = Form(False),
    llm_provider: str = Form("launchpad"),
    llm_model: str | None = Form(None),
    request_id: str = Form("1"),
) -> UnderstandingEnvelope:
    selected_csv = select_sample_csv(sample_csv, file)
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
        result = await _run_service(
            run_understanding,
            source,
            LLMOptions(
                enabled=llm_enabled,
                provider=llm_provider,
                model=llm_model,
                request_id=request_id,
            ),
        )
    return UnderstandingEnvelope(understanding=result)


@router.post("/profiling", response_model=ServiceResult)
async def profiling(
    sample_csv: UploadFile | None = File(None),
    metadata_dictionary: UploadFile = File(...),
    rule_files: list[UploadFile] | None = File(None),
    rules_archive: UploadFile | None = File(None),
    file: UploadFile | None = File(None, description="Deprecated alias for sample_csv."),
    dictionary_version: str | None = Form(None),
) -> ServiceResult:
    selected_csv = select_sample_csv(sample_csv, file)
    async with stage_understanding_inputs(
        sample_csv=selected_csv,
        metadata_dictionary=metadata_dictionary,
        rule_files=rule_files,
        rules_archive=rules_archive,
    ) as staged:
        return await _run_service(
            profile_csv,
            build_source_request(staged, dictionary_version=dictionary_version, persist_artifacts=False),
            LLMOptions(enabled=False),
        )


@router.post("/rules", response_model=ServiceResult)
async def rules(
    sample_csv: UploadFile | None = File(None),
    metadata_dictionary: UploadFile = File(...),
    rule_files: list[UploadFile] | None = File(None),
    rules_archive: UploadFile | None = File(None),
    file: UploadFile | None = File(None, description="Deprecated alias for sample_csv."),
    dictionary_version: str | None = Form(None),
) -> ServiceResult:
    selected_csv = select_sample_csv(sample_csv, file)
    async with stage_understanding_inputs(
        sample_csv=selected_csv,
        metadata_dictionary=metadata_dictionary,
        rule_files=rule_files,
        rules_archive=rules_archive,
    ) as staged:
        return await _run_service(
            prepare_rules,
            build_source_request(staged, dictionary_version=dictionary_version, persist_artifacts=False),
            LLMOptions(enabled=False),
        )


async def _run_service(
    function: Callable[..., Any],
    source: UnderstandingSourceRequest,
    llm: LLMOptions,
) -> Any:
    try:
        return await run_in_threadpool(function, source, llm)
    except UnderstandingServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
