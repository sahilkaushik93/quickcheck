from fastapi import APIRouter

"""Top-level API registration for structured-data DQ and Business Impact."""

from app.api.routes import artifacts, business_impact, engine, execution, health, llm, understanding


api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(llm.router)
# The unprefixed status endpoint remains browser-friendly; the prefixed router
# exposes the canonical POST /api/v1/undq/transcript/business-impact endpoint.
api_router.include_router(business_impact.browser_router)
api_router.include_router(engine.router)
api_router.include_router(understanding.router)
api_router.include_router(execution.router)
api_router.include_router(business_impact.router)
api_router.include_router(artifacts.router)
