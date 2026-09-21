"""End-to-end coordinator for the transcript Understanding Layer MVP."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.understanding.context.context_builder import ContextBuilder
from app.understanding.context.domain_classifier import DomainClassifier, DomainClassifierConfig
from app.understanding.context.metadata_loader import LumiMetadataLoader
from app.understanding.context.relationship_discovery import (
    RelationshipDiscoverer,
    RelationshipDiscoveryConfig,
)
from app.understanding.enrichment.llm_enrichment import LLMEnricher, LLMEnrichmentOptions
from app.understanding.models import (
    ProcessingLimits,
    SourceMetadata,
    SourceType,
    UnderstandingInputProvenance,
    UnderstandingOutput,
)
from app.understanding.profiling.accumulators import AccumulatorOptions
from app.understanding.profiling.profiler import ProfilerOptions, StreamingProfiler
from app.understanding.rules.applicability import CapabilityCatalog, RuleApplicabilityResolver
from app.understanding.rules.plan_builder import ExecutionPlanBuilder
from app.understanding.rules.registry import RuleRegistry
from app.understanding.signals.detector import SignalDetector
from app.understanding.source_adapters.csv_adapter import CSVReadOptions, CSVSourceAdapter
from app.understanding.storage.artifact_writer import UnderstandingArtifactWriter


class UnderstandingCoordinatorError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class UnderstandingPaths:
    profiling_config: Path
    domain_ontology: Path
    signal_policies: Path
    rules_directory: Path
    output_root: Path

    @classmethod
    def from_repository_root(cls, root: str | Path) -> "UnderstandingPaths":
        root_path = Path(root).expanduser().resolve()
        config = root_path / "config" / "understanding"
        return cls(
            profiling_config=config / "profiling_config.json",
            domain_ontology=config / "domain_ontology.json",
            signal_policies=config / "signal_policies.json",
            rules_directory=config / "rules",
            output_root=root_path / "data" / "understanding_layer",
        )


@dataclass(frozen=True, slots=True)
class UnderstandingRunRequest:
    csv_path: Path
    metadata_dictionary_path: Path
    source_id: str
    display_name: str
    input_provenance: UnderstandingInputProvenance | None = None
    run_id: str | None = None
    source_version: str | None = None
    dictionary_version: str | None = None
    capabilities: frozenset[str] = frozenset(
        {"profile.row_count", "profile.null_count", "profile.bounded_frequencies"}
    )
    persist_artifacts: bool = True


class UnderstandingCoordinator:
    """Execute profile → context → signals → rules → plan → LLM → artifacts."""

    def __init__(
        self,
        paths: UnderstandingPaths,
        *,
        llm_options: LLMEnrichmentOptions | None = None,
    ) -> None:
        self._paths = paths
        self._profiling_config = self._read_json(paths.profiling_config)
        ontology = self._read_json(paths.domain_ontology)
        policies = self._read_json(paths.signal_policies)
        runtime = self._profiling_config["runtime"]
        bounded = self._profiling_config["bounded_state"]
        statistics = self._profiling_config["statistics"]
        self._limits = ProcessingLimits(
            chunk_size=int(runtime["chunk_size_rows"]),
            max_distinct_values=int(bounded["max_distinct_values_per_column"]),
            max_top_values=int(bounded["max_top_values_per_column"]),
            max_samples_per_column=int(bounded["max_safe_samples_per_column"]),
            max_evidence_items=int(bounded["max_evidence_items_per_signal"]),
            max_relationship_pairs=int(bounded["max_relationship_pairs"]),
        )
        self._csv_options = CSVReadOptions.from_mapping(self._profiling_config["csv_reader"])
        self._profiler = StreamingProfiler(
            limits=self._limits,
            accumulator_options=AccumulatorOptions.from_mapping(bounded, statistics),
            options=ProfilerOptions.from_mapping(runtime),
        )
        self._context_builder = ContextBuilder(
            metadata_loader=LumiMetadataLoader(),
            domain_classifier=DomainClassifier(DomainClassifierConfig.from_mapping(ontology)),
            relationship_discoverer=RelationshipDiscoverer(
                RelationshipDiscoveryConfig.from_mapping(self._profiling_config["relationship_discovery"])
            ),
        )
        self._signal_detector = SignalDetector(policies)
        self._registry = RuleRegistry(paths.rules_directory)
        self._applicability = RuleApplicabilityResolver()
        self._plan_builder = ExecutionPlanBuilder()
        self._enricher = LLMEnricher(llm_options or LLMEnrichmentOptions(enabled=False))
        self._artifact_writer = UnderstandingArtifactWriter(paths.output_root)

    def run_csv(self, request: UnderstandingRunRequest) -> UnderstandingOutput:
        run_id = request.run_id or f"undq-{uuid.uuid4().hex}"
        try:
            csv_path = request.csv_path.expanduser().resolve()
            source = SourceMetadata(
                source_id=request.source_id,
                source_type=SourceType.CSV,
                display_name=request.display_name,
                format="csv",
                encoding=self._csv_options.encoding,
                delimiter=self._csv_options.delimiter,
                declared_size_bytes=csv_path.stat().st_size if csv_path.is_file() else None,
                metadata_dictionary_name=(
                    request.input_provenance.metadata_dictionary.filename
                    if request.input_provenance is not None
                    else request.metadata_dictionary_path.name
                ),
                source_version=request.source_version,
                fingerprint=(
                    request.input_provenance.sample_csv.sha256
                    if request.input_provenance is not None
                    else None
                ),
            )
            with CSVSourceAdapter(csv_path, source, self._limits, self._csv_options) as adapter:
                profile = self._profiler.profile(adapter)

            context = self._context_builder.build(
                profile,
                request.metadata_dictionary_path,
                dictionary_version=request.dictionary_version,
            )
            signal_result = self._signal_detector.evaluate(profile, context)
            snapshot = self._registry.load()
            input_provenance = request.input_provenance
            if input_provenance is not None:
                input_provenance = input_provenance.model_copy(
                    update={
                        "rule_registry": input_provenance.rule_registry.model_copy(
                            update={"canonical_fingerprint": snapshot.fingerprint}
                        )
                    }
                )
            decisions = self._applicability.resolve(
                snapshot,
                profile,
                context,
                CapabilityCatalog(request.capabilities),
            )
            plan = self._plan_builder.build(snapshot, decisions, run_id=run_id)
            enrichment = self._enricher.enrich(
                profile,
                context,
                list(signal_result.signals),
                plan,
            )
            availability_warnings = [
                f"Metric unavailable for {item.check_name}: {item.reason}"
                for item in signal_result.metric_availability
                if not item.available
            ]
            warnings = list(
                dict.fromkeys(
                    [
                        *profile.warnings,
                        *context.warnings,
                        *signal_result.warnings,
                        *availability_warnings,
                        *snapshot.warnings,
                        *plan.warnings,
                        *enrichment.warnings,
                    ]
                )
            )
            status = "completed_with_warnings" if warnings or plan.blocked_rules else "completed"
            result = UnderstandingOutput(
                run_id=run_id,
                status=status,
                source=source,
                input_provenance=input_provenance,
                profile=profile,
                metadata_knowledge_base=context.metadata,
                domains=list(context.domains),
                relationships=list(context.relationships),
                dq_signals=list(signal_result.signals),
                rule_applicability=decisions,
                execution_plan=plan,
                llm_insights=list(enrichment.insights),
                warnings=warnings,
            )
            if request.persist_artifacts:
                result = self._artifact_writer.write(result).result
            return result
        except UnderstandingCoordinatorError:
            raise
        except Exception as exc:
            code = getattr(exc, "code", "UNDERSTANDING_RUN_FAILED")
            message = getattr(
                exc,
                "message",
                f"Understanding workflow failed ({type(exc).__name__}).",
            )
            raise UnderstandingCoordinatorError(
                str(code),
                str(message),
            ) from exc

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise UnderstandingCoordinatorError(
                "INVALID_UNDERSTANDING_CONFIG",
                f"Understanding configuration {path.name!r} could not be loaded.",
            ) from exc
        if not isinstance(document, dict):
            raise UnderstandingCoordinatorError(
                "INVALID_UNDERSTANDING_CONFIG",
                f"Understanding configuration {path.name!r} must contain a JSON object.",
            )
        return document
