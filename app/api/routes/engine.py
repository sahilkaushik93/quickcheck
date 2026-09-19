from fastapi import APIRouter, File, Form, UploadFile

from app.api.routes._uploads import validate_csv
from app.core.config import settings
from app.models.responses import ComponentStatusResponse, EngineResponse, LLMOptions
from app.services.engine import run_end_to_end


router = APIRouter(prefix=settings.api_prefix, tags=["engine"])


@router.get("/engine", response_model=ComponentStatusResponse)
def engine_status() -> ComponentStatusResponse:
    return ComponentStatusResponse(
        component="end-to-end-engine",
        message="End-to-end transcript DQ engine API is healthy and ready to process a CSV.",
        processing_method="POST",
    )


@router.post("/engine", response_model=EngineResponse)
async def engine(
    file: UploadFile = File(...),
    llm_provider: str = Form("launchpad"),
    llm_model: str | None = Form(None),
    request_id: str = Form("1"),
) -> EngineResponse:
    validate_csv(file)
    return run_end_to_end(
        file.filename or "uploaded.csv",
        LLMOptions(provider=llm_provider, model=llm_model, request_id=request_id),
    )
