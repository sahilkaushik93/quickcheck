import os
from pathlib import Path

from fastapi import APIRouter, Response, status

from app.core.config import settings
from app.models.responses import HealthResponse


router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="healthy",
        service=settings.app_name,
        version=settings.app_version,
        checks={"api": "available"},
    )


@router.get("/ready", response_model=HealthResponse)
def ready(response: Response) -> HealthResponse:
    """Report runtime dependencies without exposing provider credentials."""

    from app.execution.handlers.registry import HandlerRegistry

    root = Path(__file__).resolve().parents[3]
    registry = HandlerRegistry()
    registered = registry.registered_check_names()
    required = [
        root / "config/understanding/profiling_config.json",
        root / "config/understanding/domain_ontology.json",
        root / "config/understanding/signal_policies.json",
        root / "config/execution/execution_config.json",
        root / "config/business_impact/scoring_policy.json",
        root / "config/business_impact/rule_metric_catalog.json",
        root / "config/business_impact/confidence_policy.json",
        root / "config/business_impact/source_registry.json",
        root / "config/business_impact/query_policy.json",
        root / "config/business_impact/citation_policy.json",
        root / "config/business_impact/disclosure_policy.json",
        root / "config/business_impact/graph_policy.json",
        root / "config/business_impact/prompts/trusted_business_insight.v1.txt",
    ]
    missing = [str(path.relative_to(root)) for path in required if not path.is_file()]
    ready_state = not missing and len(registered) == 7
    if not ready_state:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    provider = os.getenv("LLM_PROVIDER", "launchpad")
    provider_configured = {
        "ollama": bool(os.getenv("OLLAMA_API_URL", "http://127.0.0.1:11434/api/generate")),
        "launchpad": bool(os.getenv("LAUNCHPAD_API_URL", "http://127.0.0.1:8080/generate/single")),
        "doc_intelligence": bool(os.getenv("DOC_INTELLIGENCE_API_URL")),
    }.get(provider, False)
    return HealthResponse(
        status="ready" if ready_state else "not_ready",
        service=settings.app_name,
        version=settings.app_version,
        checks={
            "configuration": "available" if not missing else "missing",
            "missing_configuration": missing,
            "rule_registry": "available" if len(registered) == 7 else "incomplete",
            "registered_rule_count": len(registered),
            "registered_rules": list(registered),
            "ontology": "available" if required[1].is_file() else "missing",
            "llm_provider": provider,
            "llm_provider_configured": provider_configured,
            "evidence_salt_configured": bool(os.getenv("UNDQ_EVIDENCE_HASH_SALT")),
            "business_impact": "configuration_available" if all(
                path.is_file() for path in required if "business_impact" in str(path)
            ) else "configuration_missing",
            "business_impact_connector_registry": "available" if (root / "config/business_impact/source_registry.json").is_file() else "missing",
            "business_impact_source_policy": "available" if (root / "config/business_impact/query_policy.json").is_file() else "missing",
            "business_impact_prompt_version": "trusted_business_insight.v1" if (root / "config/business_impact/prompts/trusted_business_insight.v1.txt").is_file() else "missing",
        },
    )
