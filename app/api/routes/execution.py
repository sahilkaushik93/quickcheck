from fastapi import APIRouter

from app.core.config import settings
from app.models.responses import ComponentStatusResponse, ExecutionEnvelope, ExecutionRequest
from app.services.execution import execute_rules


router = APIRouter(prefix=settings.api_prefix, tags=["execution"])


@router.get("/execution", response_model=ComponentStatusResponse)
def execution_status() -> ComponentStatusResponse:
    return ComponentStatusResponse(
        component="execution",
        message="Transcript DQ execution API is healthy and ready to accept an understanding payload.",
        processing_method="POST",
    )


@router.post("/execution", response_model=ExecutionEnvelope)
async def execution(payload: ExecutionRequest) -> ExecutionEnvelope:
    return ExecutionEnvelope(
        understanding=payload.understanding,
        execution=execute_rules(payload.understanding, payload.llm),
    )
