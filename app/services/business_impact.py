from app.models.responses import BusinessImpactRequest, BusinessImpactResult, ServiceResult


def analyze_business_impact(payload: BusinessImpactRequest) -> BusinessImpactResult:
    filename = payload.understanding.Profile.input_file
    execution_status = payload.execution.Observed_Results.status
    return BusinessImpactResult(
        scores=ServiceResult(
            message="DQ scoring service is working.",
            input_file=filename,
            details={
                "result_state": "stub",
                "execution_status_received": execution_status,
                "llm_provider": payload.llm.provider,
                "llm_model": payload.llm.model,
            },
        ),
        evidences=ServiceResult(
            message="DQ evidence service is working.",
            input_file=filename,
            details={
                "mask_sensitive_values": True,
                "observed_results_received": True,
                "result_state": "stub",
            },
        ),
        impact_insights=ServiceResult(
            message="Business-impact agent API is working.",
            input_file=filename,
            details={
                "business_context": payload.business_context,
                "request_id": payload.llm.request_id,
                "result_state": "stub",
            },
        ),
    )
