from fastapi import APIRouter, HTTPException, Request, status
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.models.responses import (
    BusinessImpactEnvelope,
    BusinessImpactRequest,
    ComponentStatusResponse,
)
from app.services.business_impact import BusinessImpactService, BusinessImpactServiceError


router = APIRouter(prefix=settings.api_prefix, tags=["business-impact"])
browser_router = APIRouter(tags=["component-status"])


def status_response() -> ComponentStatusResponse:
    return ComponentStatusResponse(
        component="business-impact-agent",
        status="healthy",
        message="Business Impact accepts canonical Understanding and Execution outputs.",
        processing_method="deterministic scoring plus governed trusted-insight graph",
    )


@browser_router.get("/business-impact", response_model=ComponentStatusResponse)
def root_business_impact_status() -> ComponentStatusResponse:
    return status_response()


@router.get("/business-impact", response_model=ComponentStatusResponse)
def business_impact_status() -> ComponentStatusResponse:
    return status_response()


@router.post("/business-impact", response_model=BusinessImpactEnvelope)
async def business_impact(request: Request, payload: BusinessImpactRequest) -> BusinessImpactEnvelope:
    """Run Business Impact from supplied compact contracts, never source data."""

    service = getattr(request.app.state, "business_impact_service", None)
    if not isinstance(service, BusinessImpactService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "BUSINESS_IMPACT_NOT_CONFIGURED", "message": "Business Impact service composition is not configured."},
        )
    try:
        result = await run_in_threadpool(service.analyze, payload)
    except BusinessImpactServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    return BusinessImpactEnvelope(
        request_id=payload.llm.request_id,
        status=str(result.status),
        business_impact=result,
    )
