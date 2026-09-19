from app.models.responses import BusinessImpactRequest, EngineResponse, LLMOptions
from app.services.business_impact import analyze_business_impact
from app.services.execution import execute_rules
from app.services.understanding import run_understanding


def run_end_to_end(filename: str, llm: LLMOptions | None = None) -> EngineResponse:
    """Compose reusable layer services without making HTTP calls internally."""
    understanding = run_understanding(filename)
    llm = llm or LLMOptions()
    execution = execute_rules(understanding, llm)
    business_impact = analyze_business_impact(
        BusinessImpactRequest(
            understanding=understanding,
            execution=execution,
            business_context={"invoked_by": "end_to_end_engine"},
            llm=llm,
        )
    )
    return EngineResponse(
        understanding=understanding,
        execution=execution,
        business_impact=business_impact,
    )
