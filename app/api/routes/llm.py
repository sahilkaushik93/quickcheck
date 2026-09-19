from fastapi import APIRouter

from app.llm_provider import PROVIDERS


router = APIRouter(prefix="/api/v1/undq/llm", tags=["llm-providers"])


@router.get("/providers")
def list_providers() -> dict[str, object]:
    return {
        "status": "healthy",
        "available_providers": sorted(PROVIDERS),
        "selection": "Set llm.provider in a JSON request or LLM_PROVIDER in the environment.",
    }
