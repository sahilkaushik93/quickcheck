"""The sole complete orchestrator for trusted Business Impact assessments."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.business_impact.evidence.models import EvidenceFactPack, NormalizedEvidence
from app.business_impact.external_context.models import Citation, ExternalContextBundle
from app.business_impact.impact.models import GovernedRecommendation, ImpactHypothesis, RootCauseCandidate
from app.business_impact.models import (
    BusinessImpactError, BusinessImpactOutput, BusinessImpactRunRequest, BusinessImpactStatus,
    BusinessImpactWarning, TrustedInsightClaim,
)
from app.business_impact.scoring.models import (
    AggregateQualityScore, AssessmentConfidence, NormalizedMetricScore, RuleQualityScore,
)
from app.business_impact.storage.artifact_writer import BusinessImpactArtifactWriter, ArtifactWriteError


class BusinessImpactCoordinatorError(RuntimeError):
    """Safe coordinator error whose code is suitable for an API response."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class DeterministicAssessment:
    """Sanitized output from evidence, scoring and impact stages before any LLM call."""

    fact_pack: EvidenceFactPack
    normalized_evidence: tuple[NormalizedEvidence, ...]
    normalized_metric_scores: tuple[NormalizedMetricScore, ...]
    rule_scores: tuple[RuleQualityScore, ...]
    aggregate_score: AggregateQualityScore | None
    assessment_confidence: AssessmentConfidence | None
    impact_hypotheses: tuple[ImpactHypothesis, ...]
    root_cause_candidates: tuple[RootCauseCandidate, ...]
    recommendations: tuple[GovernedRecommendation, ...]
    warnings: tuple[BusinessImpactWarning, ...] = ()
    errors: tuple[BusinessImpactError, ...] = ()


@dataclass(frozen=True, slots=True)
class TrustedGraphAssessment:
    """Validated post-retrieval/LLM graph result; URLs only come from citations."""

    status: BusinessImpactStatus
    external_context: ExternalContextBundle | None
    citations: tuple[Citation, ...]
    claims: tuple[TrustedInsightClaim, ...]
    requires_human_review: bool = False
    warnings: tuple[BusinessImpactWarning, ...] = ()
    errors: tuple[BusinessImpactError, ...] = ()


class DeterministicAssessmentRunner(Protocol):
    """Runs deterministic stages without any LLM, connector, source read or rerun."""

    def run(self, request: BusinessImpactRunRequest) -> DeterministicAssessment: ...


class TrustedGraphRunner(Protocol):
    """Runs the pre-assembled graph using only sanitized deterministic output."""

    def run(self, request: BusinessImpactRunRequest, deterministic: DeterministicAssessment) -> TrustedGraphAssessment: ...


class BusinessImpactCoordinator:
    """Validate → deterministic assessment → trusted graph → output → artifacts.

    The coordinator intentionally accepts explicit runners: each is constructed
    from policy-loaded components by application composition, while this class
    remains the single runtime boundary that determines completion semantics.
    """

    def __init__(self, deterministic_runner: DeterministicAssessmentRunner, graph_runner: TrustedGraphRunner,
                 artifact_writer: BusinessImpactArtifactWriter | None = None) -> None:
        self._deterministic_runner = deterministic_runner
        self._graph_runner = graph_runner
        self._artifact_writer = artifact_writer

    def run(self, request: BusinessImpactRunRequest) -> BusinessImpactOutput:
        """Run the full layer once, retaining deterministic work on graph failure."""

        try:
            self._validate(request)
            deterministic = self._deterministic_runner.run(request)
        except BusinessImpactCoordinatorError:
            raise
        except Exception as exc:
            raise BusinessImpactCoordinatorError("BUSINESS_IMPACT_DETERMINISTIC_FAILED", "Deterministic Business Impact stages failed.") from exc
        try:
            graph = self._graph_runner.run(request, deterministic)
        except Exception:
            graph = TrustedGraphAssessment(
                status=BusinessImpactStatus.PARTIAL, external_context=None, citations=(), claims=(),
                errors=(BusinessImpactError(code="TRUSTED_GRAPH_FAILED", message="Trusted insight graph failed; deterministic evidence and scores remain available.", retryable=False, component="coordinator"),),
            )
        status = self._status(graph, deterministic)
        output = BusinessImpactOutput(
            run_id=request.run_id, understanding_run_id=request.understanding.run_id,
            execution_run_id=request.execution.run_id, status=status, fact_pack=deterministic.fact_pack,
            normalized_evidence=list(deterministic.normalized_evidence),
            normalized_metric_scores=list(deterministic.normalized_metric_scores), rule_scores=list(deterministic.rule_scores),
            aggregate_score=deterministic.aggregate_score, assessment_confidence=deterministic.assessment_confidence,
            impact_hypotheses=list(deterministic.impact_hypotheses), root_cause_candidates=list(deterministic.root_cause_candidates),
            external_context=graph.external_context, citations=list(graph.citations), insight_claims=list(graph.claims),
            recommendations=list(deterministic.recommendations), llm_provider=request.llm.provider if graph.claims else None,
            llm_model=request.llm.model if graph.claims else None,
            warnings=list((*deterministic.warnings, *graph.warnings)), errors=list((*deterministic.errors, *graph.errors)),
        )
        if request.persist_artifacts:
            if self._artifact_writer is None:
                raise BusinessImpactCoordinatorError("ARTIFACT_WRITER_NOT_CONFIGURED", "Artifact persistence was requested but is not configured.")
            try:
                output = output.model_copy(update={"artifact_references": self._artifact_writer.write(output)})
            except ArtifactWriteError as exc:
                output = output.model_copy(update={"status": BusinessImpactStatus.PARTIAL, "errors": [*output.errors, BusinessImpactError(code=exc.code, message=exc.message, retryable=False, component="artifact_writer")]})
        return output

    @staticmethod
    def _validate(request: BusinessImpactRunRequest) -> None:
        if request.understanding.run_id != request.execution.understanding_run_id:
            raise BusinessImpactCoordinatorError("HANDOFF_PROVENANCE_INVALID", "Understanding and Execution run provenance does not match.")
        if not request.llm.provider:
            raise BusinessImpactCoordinatorError("LLM_SELECTION_REQUIRED", "A configured enterprise LLM provider is required for a complete report.")

    @staticmethod
    def _status(graph: TrustedGraphAssessment, deterministic: DeterministicAssessment) -> BusinessImpactStatus:
        if graph.status == BusinessImpactStatus.FAILED:
            return BusinessImpactStatus.PARTIAL if deterministic.fact_pack.facts else BusinessImpactStatus.FAILED
        if not deterministic.fact_pack.facts:
            return BusinessImpactStatus.INSUFFICIENT_CONTEXT
        if deterministic.errors or graph.errors:
            return BusinessImpactStatus.PARTIAL
        if graph.requires_human_review or graph.status in {BusinessImpactStatus.PARTIAL, BusinessImpactStatus.INSUFFICIENT_CONTEXT}:
            return BusinessImpactStatus.PARTIAL
        if graph.claims and not graph.errors:
            return BusinessImpactStatus.COMPLETED_WITH_WARNINGS if (*deterministic.warnings, *graph.warnings) else BusinessImpactStatus.COMPLETED
        return BusinessImpactStatus.PARTIAL


__all__ = [
    "BusinessImpactCoordinator", "BusinessImpactCoordinatorError", "DeterministicAssessment",
    "DeterministicAssessmentRunner", "TrustedGraphAssessment", "TrustedGraphRunner",
]
