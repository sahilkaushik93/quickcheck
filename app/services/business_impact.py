"""API-facing service for the governed Business Impact coordinator."""

from __future__ import annotations

from hashlib import sha256

from app.business_impact.coordinator import BusinessImpactCoordinator
from app.business_impact.models import BusinessImpactRunRequest, LLMSelection
from app.models.responses import BusinessImpactRequest


class BusinessImpactServiceError(RuntimeError):
    """Safe API-service failure with a stable code and no source-data details."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class BusinessImpactService:
    """Adapts the public JSON handoff to the sole Business Impact coordinator."""

    def __init__(self, coordinator: BusinessImpactCoordinator) -> None:
        self._coordinator = coordinator

    def analyze(self, payload: BusinessImpactRequest):
        """Execute only against supplied canonical outputs; never reopen a source."""

        try:
            return self._coordinator.run(
                BusinessImpactRunRequest(
                    run_id=_run_id(payload),
                    understanding=payload.understanding,
                    execution=payload.execution,
                    business_context=payload.business_context,
                    persist_artifacts=payload.options.persist_artifacts,
                    llm=LLMSelection(provider=payload.llm.provider, model=payload.llm.model, request_id=payload.llm.request_id),
                )
            )
        except Exception as exc:
            raise BusinessImpactServiceError(
                getattr(exc, "code", "BUSINESS_IMPACT_PROCESSING_FAILED"),
                getattr(exc, "message", "Business Impact processing failed."),
            ) from exc


def _run_id(payload: BusinessImpactRequest) -> str:
    seed = "|".join((payload.llm.request_id, payload.understanding.run_id, payload.execution.run_id))
    return "impact-" + sha256(seed.encode("utf-8")).hexdigest()[:24]


__all__ = ["BusinessImpactService", "BusinessImpactServiceError"]
