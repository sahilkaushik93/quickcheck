"""Application service adapting ``UnderstandingCoordinator`` to API models."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final

from app.models.responses import LLMOptions, ServiceResult, UnderstandingResult
from app.understanding.coordinator import (
    UnderstandingCoordinator,
    UnderstandingCoordinatorError,
    UnderstandingPaths,
    UnderstandingRunRequest,
)
from app.understanding.enrichment.llm_enrichment import LLMEnrichmentOptions
from app.understanding.models import UnderstandingInputProvenance, UnderstandingOutput


_PROFILE_CAPABILITIES: Final[frozenset[str]] = frozenset(
    {
        "profile.row_count",
        "profile.null_count",
        "profile.bounded_frequencies",
    }
)

# A capability is advertised only when its handler imports successfully and
# every policy file required by that deterministic implementation exists. This
# keeps Understanding independent of source column names while preventing a
# plan from promising functionality that the deployed Execution package cannot
# provide.
_HANDLER_CAPABILITIES: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "fill_rate": frozenset(),
        "topic_distribution": frozenset(),
        "agent_speaker_tag_validation": frozenset(
            {"execution.transcript_stream", "execution.speaker_tag_parser"}
        ),
        "non_english_spelling": frozenset(
            {
                "execution.transcript_stream",
                "execution.language_detector",
                "execution.spell_checker",
            }
        ),
        "mistranslated_rate": frozenset(
            {"execution.transcript_stream", "execution.mistranslation_lexicon"}
        ),
        "pii_detection": frozenset(
            {"execution.transcript_stream", "execution.pii_detector"}
        ),
        "speech_per_duration_rate": frozenset(
            {
                "execution.transcript_stream",
                "execution.duration_pairing",
                "execution.word_counter",
            }
        ),
    }
)

_HANDLER_POLICY_FILES: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "agent_speaker_tag_validation": frozenset({"speaker_tags.json"}),
        "non_english_spelling": frozenset(
            {"language_policy.json", "business_glossary.json"}
        ),
        "mistranslated_rate": frozenset({"mistranslation_dictionary.json"}),
        "pii_detection": frozenset({"pii_patterns.json"}),
        "speech_per_duration_rate": frozenset({"execution_config.json"}),
    }
)


class UnderstandingServiceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class UnderstandingSourceRequest:
    """Safe descriptor for a locally accessible, request-scoped CSV source."""

    csv_path: Path
    original_filename: str
    source_id: str
    metadata_dictionary_path: Path | None = None
    rules_directory: Path | None = None
    input_provenance: UnderstandingInputProvenance | None = None
    run_id: str | None = None
    source_version: str | None = None
    dictionary_version: str | None = None
    capabilities: frozenset[str] | None = None
    persist_artifacts: bool = True


def run_understanding(
    source: UnderstandingSourceRequest | str | Path,
    llm: LLMOptions | None = None,
) -> UnderstandingResult:
    """Execute the real Understanding workflow and return the public contract."""

    request = _normalize_source(source)
    llm = llm or LLMOptions()
    paths = _understanding_paths(request.rules_directory)
    metadata_path = request.metadata_dictionary_path or _default_metadata_dictionary()
    coordinator = UnderstandingCoordinator(
        paths,
        llm_options=LLMEnrichmentOptions(
            enabled=llm.enabled,
            provider=llm.provider,
            model=llm.model,
            request_id=llm.request_id,
            failure_mode=llm.failure_mode,
        ),
    )
    try:
        result = coordinator.run_csv(
            UnderstandingRunRequest(
                csv_path=request.csv_path,
                metadata_dictionary_path=metadata_path,
                source_id=request.source_id,
                display_name=request.original_filename,
                input_provenance=request.input_provenance,
                run_id=request.run_id,
                source_version=request.source_version,
                dictionary_version=request.dictionary_version,
                capabilities=(
                    request.capabilities
                    if request.capabilities is not None
                    else _configured_capabilities()
                ),
                persist_artifacts=request.persist_artifacts,
            )
        )
        return _to_public_result(result)
    except UnderstandingCoordinatorError as exc:
        raise UnderstandingServiceError(exc.code, exc.message) from exc
    except UnderstandingServiceError:
        raise
    except Exception as exc:
        raise UnderstandingServiceError(
            "UNDERSTANDING_SERVICE_FAILED",
            f"Understanding processing failed ({type(exc).__name__}).",
        ) from exc


def profile_csv(
    source: UnderstandingSourceRequest | str | Path,
    llm: LLMOptions | None = None,
) -> ServiceResult:
    """Run the shared workflow once and return its Profile compatibility view."""

    return run_understanding(source, llm).Profile


def prepare_rules(
    source: UnderstandingSourceRequest | str | Path,
    llm: LLMOptions | None = None,
) -> ServiceResult:
    """Run the shared workflow once and return its Rules compatibility view."""

    return run_understanding(source, llm).Rules


def _to_public_result(result: UnderstandingOutput) -> UnderstandingResult:
    planned_rules = [decision.rule_id for decision in result.rule_applicability]
    selected_rules = [
        decision.rule_id
        for decision in result.rule_applicability
        if decision.status == "applicable"
    ]
    execution_ready_rules = [step.rule_id for step in result.execution_plan.steps]
    implementation_pending_rules = list(
        result.execution_plan.implementation_pending_rules
    )
    not_applicable_rules = [
        decision.rule_id
        for decision in result.rule_applicability
        if decision.status == "not_applicable"
    ]
    rules_requiring_review = [
        decision.rule_id
        for decision in result.rule_applicability
        if decision.status == "needs_review"
    ]
    profile = ServiceResult(
        status=result.status,
        message="Understanding profiling completed.",
        input_file=result.source.display_name,
        details={
            "run_id": result.run_id,
            "row_count": result.profile.row_count,
            "column_count": result.profile.column_count,
            "chunks_processed": result.profile.chunks_processed,
            "elapsed_seconds": result.profile.elapsed_seconds,
            "columns": [column.model_dump(mode="json") for column in result.profile.columns],
            "input_provenance": (
                result.input_provenance.model_dump(mode="json")
                if result.input_provenance is not None
                else None
            ),
        },
    )
    rules = ServiceResult(
        status=result.status,
        message="Rule applicability and execution planning completed.",
        input_file=result.source.display_name,
        details={
            "planned_rules": planned_rules,
            # applicable_rules is retained as a compatibility alias. It now
            # correctly means selected for this dataset, independent of whether
            # an execution handler is currently available.
            "applicable_rules": selected_rules,
            "selected_rules": selected_rules,
            "execution_ready_rules": execution_ready_rules,
            "implementation_pending_rules": implementation_pending_rules,
            "not_applicable_rules": not_applicable_rules,
            "rules_requiring_review": rules_requiring_review,
            # Deprecated compatibility field. Prefer
            # implementation_pending_rules in new consumers.
            "blocked_rules": implementation_pending_rules,
            "plan_id": result.execution_plan.plan_id,
            "plan_status": result.execution_plan.status,
            "execution_steps": [
                step.model_dump(mode="json") for step in result.execution_plan.steps
            ],
            "registry_fingerprint": result.execution_plan.registry_fingerprint,
            "rule_file_count": (
                result.input_provenance.rule_registry.file_count
                if result.input_provenance is not None
                else len(planned_rules)
            ),
        },
    )
    return UnderstandingResult(
        Profile=profile,
        Rules=rules,
        run_id=result.run_id,
        status=result.status,
        input_provenance=result.input_provenance,
        metadata_knowledge_base=result.metadata_knowledge_base,
        domains=result.domains,
        relationships=result.relationships,
        dq_signals=result.dq_signals,
        rule_applicability=result.rule_applicability,
        execution_plan=result.execution_plan,
        llm_insights=result.llm_insights,
        artifact_references=result.artifact_references,
        warnings=result.warnings,
    )


def _normalize_source(source: UnderstandingSourceRequest | str | Path) -> UnderstandingSourceRequest:
    if isinstance(source, UnderstandingSourceRequest):
        return source
    path = Path(source).expanduser()
    if not path.is_absolute() and not path.is_file():
        candidate = _repository_root() / "data" / "input" / path.name
        if candidate.is_file():
            path = candidate
    path = path.resolve()
    return UnderstandingSourceRequest(
        csv_path=path,
        original_filename=path.name,
        source_id=f"csv:{path.name}",
    )


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _understanding_paths(rules_directory: Path | None = None) -> UnderstandingPaths:
    paths = UnderstandingPaths.from_repository_root(_repository_root())
    configured_output = os.getenv("UNDQ_UNDERSTANDING_OUTPUT_ROOT")
    selected_rules = rules_directory.expanduser().resolve() if rules_directory else paths.rules_directory
    if not configured_output:
        return UnderstandingPaths(
            profiling_config=paths.profiling_config,
            domain_ontology=paths.domain_ontology,
            signal_policies=paths.signal_policies,
            rules_directory=selected_rules,
            output_root=paths.output_root,
        )
    return UnderstandingPaths(
        profiling_config=paths.profiling_config,
        domain_ontology=paths.domain_ontology,
        signal_policies=paths.signal_policies,
        rules_directory=selected_rules,
        output_root=Path(configured_output).expanduser().resolve(),
    )


def _default_metadata_dictionary() -> Path:
    configured = os.getenv("UNDQ_METADATA_DICTIONARY_PATH")
    if configured:
        path = Path(configured).expanduser().resolve()
        if path.is_file():
            return path
        raise UnderstandingServiceError(
            "METADATA_DICTIONARY_NOT_FOUND",
            "Configured metadata dictionary does not exist.",
        )
    data_input = _repository_root() / "data" / "input"
    for name in ("lumi_metadata_dictionary.xlsx", "lumi_metadata_dictionary(1).xlsx"):
        candidate = data_input / name
        if candidate.is_file():
            return candidate
    raise UnderstandingServiceError(
        "METADATA_DICTIONARY_NOT_FOUND",
        "No default LUMI metadata dictionary was found.",
    )


def _configured_capabilities() -> frozenset[str]:
    """Return capabilities verified for this deployed repository.

    Handler capability names are derived from the execution registry rather
    than maintained as an unrelated static allow-list. Feature capabilities
    are exposed only when the corresponding handler is importable and its
    required configuration assets exist. Environment variables may add custom
    capabilities or explicitly disable deployed capabilities for controlled
    rollouts.
    """

    capabilities = set(_PROFILE_CAPABILITIES)
    execution_config = _repository_root() / "config" / "execution"

    try:
        from app.execution.handlers.registry import (  # local capability bridge
            HandlerRegistry,
            HandlerRegistryError,
        )

        registry = HandlerRegistry()
        for check_name in sorted(registry.registered_check_names()):
            try:
                registry.resolve(check_name)
            except (HandlerRegistryError, ImportError, AttributeError, TypeError):
                continue

            required_files = _HANDLER_POLICY_FILES.get(check_name, frozenset())
            if any(not (execution_config / name).is_file() for name in required_files):
                continue

            capabilities.add(f"handler.{check_name}")
            capabilities.update(_HANDLER_CAPABILITIES.get(check_name, frozenset()))
    except (ImportError, AttributeError, TypeError):
        # A deployment without the Execution package remains a valid
        # Understanding-only deployment. Its rules are correctly reported as
        # implementation-pending instead of failing service startup.
        pass

    configured = os.getenv("UNDQ_EXECUTION_CAPABILITIES", "")
    capabilities.update(
        value.strip() for value in configured.split(",") if value.strip()
    )
    disabled = {
        value.strip()
        for value in os.getenv("UNDQ_DISABLED_EXECUTION_CAPABILITIES", "").split(",")
        if value.strip()
    }
    capabilities.difference_update(disabled)
    return frozenset(capabilities)
