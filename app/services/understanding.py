from app.models.responses import ServiceResult, UnderstandingResult


def profile_csv(filename: str) -> ServiceResult:
    return ServiceResult(
        message="Understanding profiling API is working.",
        input_file=filename,
        details={
            "planned_outputs": [
                "schema_analysis",
                "statistical_analysis",
                "domain_categorization",
                "data_quality_signals",
                "metadata_knowledge_base",
            ]
        },
    )


def prepare_rules(filename: str) -> ServiceResult:
    return ServiceResult(
        message="Understanding rules API is working.",
        input_file=filename,
        details={
            "planned_rules": [
                "fill_rate",
                "topic_distribution",
                "agent_speaker_tag_validation",
                "non_english_spelling",
                "mistranslated_rate",
                "pii_detection",
                "speech_per_duration_rate",
            ]
        },
    )


def run_understanding(filename: str) -> UnderstandingResult:
    return UnderstandingResult(
        Profile=profile_csv(filename),
        Rules=prepare_rules(filename),
    )
