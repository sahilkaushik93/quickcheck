from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.config import settings


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="MVP APIs for structured LUMI transcript metadata DQ assessment.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
app.include_router(api_router)

# Public compatibility paths reuse the versioned endpoint functions so there
# is one implementation and one validation contract.
from app.api.routes.engine import engine as engine_endpoint
from app.api.routes.execution import execution as execution_endpoint
from app.api.routes.understanding import understanding as understanding_endpoint
from app.models.responses import EngineResponse, ExecutionEnvelope, UnderstandingEnvelope

app.add_api_route(
    "/UnDQ/transcript/understanding/profiling",
    understanding_endpoint,
    methods=["POST"],
    response_model=UnderstandingEnvelope,
    tags=["compatibility"],
)
app.add_api_route(
    "/UnDQ/transcript/execution",
    execution_endpoint,
    methods=["POST"],
    response_model=ExecutionEnvelope,
    tags=["compatibility"],
)
app.add_api_route(
    "/UnDQ/transcript/engine/",
    engine_endpoint,
    methods=["POST"],
    response_model=EngineResponse,
    tags=["compatibility"],
)
