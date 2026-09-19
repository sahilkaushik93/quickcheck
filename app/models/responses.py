from typing import Any

from pydantic import BaseModel, Field


class ServiceResult(BaseModel):
    status: str = "working"
    message: str
    input_file: str
    details: dict[str, Any] = Field(default_factory=dict)


class UnderstandingResult(BaseModel):
    Profile: ServiceResult
    Rules: ServiceResult


class UnderstandingEnvelope(BaseModel):
    understanding: UnderstandingResult


class LLMOptions(BaseModel):
    provider: str = "launchpad"
    model: str | None = None
    request_id: str = "1"


class ExecutionResult(BaseModel):
    Observed_Results: ServiceResult


class ExecutionRequest(BaseModel):
    understanding: UnderstandingResult
    llm: LLMOptions = Field(default_factory=LLMOptions)


class ExecutionEnvelope(BaseModel):
    understanding: UnderstandingResult
    execution: ExecutionResult


class BusinessImpactResult(BaseModel):
    scores: ServiceResult
    evidences: ServiceResult
    impact_insights: ServiceResult


class BusinessImpactRequest(BaseModel):
    understanding: UnderstandingResult
    execution: ExecutionResult
    business_context: dict[str, Any] = Field(default_factory=dict)
    llm: LLMOptions = Field(default_factory=LLMOptions)


class BusinessImpactEnvelope(BaseModel):
    understanding: UnderstandingResult
    execution: ExecutionResult
    business_impact: BusinessImpactResult


class EngineResponse(BusinessImpactEnvelope):
    pass


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str


class ComponentStatusResponse(BaseModel):
    status: str = "healthy"
    component: str
    message: str
    processing_method: str
    documentation: str = "/docs"
