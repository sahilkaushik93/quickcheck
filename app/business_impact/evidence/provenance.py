"""Execution-to-Business-Impact evidence provenance helpers."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from app.business_impact.evidence.models import EvidenceProvenance
from app.execution.models import ExecutionOutput, RuleExecutionResult
from app.execution.rule_ids import canonical_rule_id, normalize_rule_id


class EvidenceProvenanceError(ValueError):
    """Safe provenance validation error suitable for service translation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class RuleEvidenceLineage:
    """Structured non-sensitive execution lineage for one runtime rule."""

    runtime_rule_id: str
    canonical_rule_id: str
    rule_version: str
    execution_plan_id: str
    registry_fingerprint: str | None


class EvidenceProvenanceBuilder:
    """Build deterministic provenance from an already-computed Execution output.

    This class intentionally accepts only ``ExecutionOutput``.  It never has a
    file path, source adapter, row iterator, transcript, or raw evidence value.
    """

    _SHA256 = re.compile(r"^[a-f0-9]{64}$")

    def __init__(self, execution: ExecutionOutput) -> None:
        self._execution = execution
        self._validate_execution_identity()
        self._results = self._index_rule_results(execution.rule_results)

    def lineage_for(self, runtime_rule_id: str) -> RuleEvidenceLineage:
        """Return canonical rule and plan lineage for one evidence collection."""

        try:
            normalized_runtime = normalize_rule_id(runtime_rule_id)
        except ValueError as exc:
            raise EvidenceProvenanceError(
                "UNKNOWN_EXECUTION_RULE_ID",
                "Execution evidence contains an unsupported rule identifier.",
            ) from exc
        result = self._results.get(normalized_runtime)
        if result is None:
            raise EvidenceProvenanceError(
                "EVIDENCE_RULE_RESULT_MISSING",
                "Execution evidence has no corresponding rule result.",
            )
        return RuleEvidenceLineage(
            runtime_rule_id=normalized_runtime,
            canonical_rule_id=canonical_rule_id(normalized_runtime),
            rule_version=result.rule_version,
            execution_plan_id=self._execution.execution_plan_id,
            registry_fingerprint=self._execution.registry_fingerprint,
        )

    def build(self, runtime_rule_id: str, *, collection_reference: str) -> EvidenceProvenance:
        """Create a compact, deterministic provenance contract for one collection."""

        lineage = self.lineage_for(runtime_rule_id)
        if not collection_reference or len(collection_reference) > 160:
            raise EvidenceProvenanceError(
                "INVALID_EVIDENCE_COLLECTION_REFERENCE",
                "Evidence collection reference must be a bounded non-empty identifier.",
            )
        digest_input = "|".join(
            (
                self._execution.run_id,
                lineage.execution_plan_id,
                lineage.runtime_rule_id,
                lineage.rule_version,
                collection_reference,
            )
        )
        provenance_id = "prov-" + hashlib.sha256(
            digest_input.encode("utf-8")
        ).hexdigest()[:24]
        registry = lineage.registry_fingerprint or "unavailable"
        transformation = (
            "execution_evidence_normalization"
            f";plan={lineage.execution_plan_id}"
            f";rule={lineage.runtime_rule_id}@{lineage.rule_version}"
            f";canonical={lineage.canonical_rule_id}"
            f";registry={registry}"
        )
        return EvidenceProvenance(
            provenance_id=provenance_id,
            source_layer="execution",
            source_run_id=self._execution.run_id,
            source_contract_version=self._execution.contract_version,
            source_artifact_reference=None,
            transformation=transformation,
        )

    def _validate_execution_identity(self) -> None:
        source_fingerprint = self._execution.source.fingerprint
        if not self._SHA256.fullmatch(source_fingerprint):
            raise EvidenceProvenanceError(
                "INVALID_EXECUTION_SOURCE_FINGERPRINT",
                "Execution evidence requires a valid source fingerprint.",
            )
        registry = self._execution.registry_fingerprint
        if registry is not None and not self._SHA256.fullmatch(registry):
            raise EvidenceProvenanceError(
                "INVALID_RULE_REGISTRY_FINGERPRINT",
                "Execution evidence contains an invalid rule-registry fingerprint.",
            )

    @staticmethod
    def _index_rule_results(
        results: list[RuleExecutionResult],
    ) -> dict[str, RuleExecutionResult]:
        indexed: dict[str, RuleExecutionResult] = {}
        for result in results:
            try:
                runtime_id = normalize_rule_id(result.rule_id)
            except ValueError as exc:
                raise EvidenceProvenanceError(
                    "UNKNOWN_RULE_RESULT_ID",
                    "Execution result contains an unsupported rule identifier.",
                ) from exc
            if runtime_id in indexed:
                raise EvidenceProvenanceError(
                    "DUPLICATE_RULE_RESULT_ID",
                    "Execution output contains duplicate runtime rule results.",
                )
            indexed[runtime_id] = result
        return indexed


__all__ = [
    "EvidenceProvenanceBuilder",
    "EvidenceProvenanceError",
    "RuleEvidenceLineage",
]
