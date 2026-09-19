from fastapi import APIRouter, File, UploadFile

from app.api.routes._uploads import validate_csv
from app.core.config import settings
from app.models.responses import ComponentStatusResponse, ServiceResult, UnderstandingEnvelope
from app.services.understanding import prepare_rules, profile_csv, run_understanding


router = APIRouter(
    prefix=f"{settings.api_prefix}/understanding",
    tags=["understanding"],
)


@router.get("", response_model=ComponentStatusResponse)
def understanding_status() -> ComponentStatusResponse:
    return ComponentStatusResponse(
        component="understanding",
        message="Understanding API is healthy and ready to profile a CSV.",
        processing_method="POST",
    )


@router.post("", response_model=UnderstandingEnvelope)
async def understanding(file: UploadFile = File(...)) -> UnderstandingEnvelope:
    validate_csv(file)
    return UnderstandingEnvelope(
        understanding=run_understanding(file.filename or "uploaded.csv")
    )


@router.post("/profiling", response_model=ServiceResult)
async def profiling(file: UploadFile = File(...)) -> ServiceResult:
    validate_csv(file)
    return profile_csv(file.filename or "uploaded.csv")


@router.post("/rules", response_model=ServiceResult)
async def rules(file: UploadFile = File(...)) -> ServiceResult:
    validate_csv(file)
    return prepare_rules(file.filename or "uploaded.csv")
