from fastapi import APIRouter

from app.api.routes import business_impact, engine, execution, health, llm, understanding


api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(llm.router)
api_router.include_router(business_impact.browser_router)
api_router.include_router(engine.router)
api_router.include_router(understanding.router)
api_router.include_router(execution.router)
api_router.include_router(business_impact.router)
