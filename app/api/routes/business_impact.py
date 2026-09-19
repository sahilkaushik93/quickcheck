from fastapi import APIRouter

from app.core.config import settings
from app.models.responses import (
    BusinessImpactEnvelope,
    BusinessImpactRequest,
    ComponentStatusResponse,
)
from app.services.business_impact import analyze_business_impact


router = APIRouter(prefix=settings.api_prefix, tags=["business-impact"])
browser_router = APIRouter(tags=["component-status"])


def status_response() -> ComponentStatusResponse:
    return ComponentStatusResponse(
        component="business-impact-agent",
        message="Business-impact agent API is healthy and ready to assess execution results.",
        processing_method="POST",
    )


@browser_router.get("/business-impact", response_model=ComponentStatusResponse)
def root_business_impact_status() -> ComponentStatusResponse:
    return status_response()


@router.get("/business-impact", response_model=ComponentStatusResponse)
def business_impact_status() -> ComponentStatusResponse:
    return status_response()


@router.post("/business-impact", response_model=BusinessImpactEnvelope)
async def business_impact(payload: BusinessImpactRequest) -> BusinessImpactEnvelope:
    return BusinessImpactEnvelope(
        understanding=payload.understanding,
        execution=payload.execution,
        business_impact=analyze_business_impact(payload),
    )
