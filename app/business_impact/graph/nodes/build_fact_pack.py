"""Build a bounded, sanitized fact pack from completed Understanding/Execution."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Iterable

from app.business_impact.evidence.models import EvidenceFactPack, InternalFact, InternalFactKind, NormalizedEvidence
from app.business_impact.evidence.normalizer import EvidenceNormalizer
from app.business_impact.models import BusinessImpactRunRequest
from app.execution.rule_ids import canonical_rule_id


@dataclass(frozen=True, slots=True)
class FactPackBuildResult:
    """Fact pack plus the retained normalized evidence for graph state."""

    fact_pack: EvidenceFactPack
    normalized_evidence: tuple[NormalizedEvidence, ...]
    warnings: tuple[str, ...]


class FactPackBuilder:
    """Creates deterministic aggregate facts; it never opens the input source."""

    def __init__(self, evidence_normalizer: EvidenceNormalizer, *, maximum_internal_facts: int) -> None:
        if maximum_internal_facts < 1:
            raise ValueError("maximum_internal_facts must be positive")
        self._normalizer = evidence_normalizer
        self._maximum_internal_facts = maximum_internal_facts

    def build(self, request: BusinessImpactRunRequest) -> FactPackBuildResult:
        """Derive redacted aggregate facts from existing result contracts only."""

        execution = request.execution
        normalization = self._normalizer.normalize(execution)
        facts = list(self._metric_facts(request))
        facts.extend(item.fact for item in normalization.normalized_evidence)
        facts.sort(key=lambda item: item.fact_id)
        retained = facts[: self._maximum_internal_facts]
        dropped = len(facts) - len(retained)
        pack = EvidenceFactPack(
            pack_id="factpack-" + _digest(request.run_id + "|" + execution.run_id),
            run_id=request.run_id,
            facts=retained,
            evidence=list(normalization.normalized_evidence),
            truncated=normalization.truncated or dropped > 0,
            dropped_fact_count=dropped,
        )
        warnings = list(normalization.warnings)
        if dropped:
            warnings.append("Internal fact pack was capped by graph policy.")
        return FactPackBuildResult(pack, normalization.normalized_evidence, tuple(sorted(set(warnings))))

    def _metric_facts(self, request: BusinessImpactRunRequest) -> Iterable[InternalFact]:
        execution = request.execution
        for result in sorted(execution.rule_results, key=lambda item: (canonical_rule_id(item.rule_id), item.step_id)):
            rule_id = canonical_rule_id(result.rule_id)
            for group_index, group in enumerate(result.groups):
                for metric in sorted(group.metrics, key=lambda item: item.name.casefold()):
                    seed = f"{execution.run_id}|{result.step_id}|{group_index}|{metric.name}"
                    yield InternalFact(
                        fact_id="fact-" + _digest(seed),
                        kind=InternalFactKind.EXECUTION_METRIC,
                        title=f"{rule_id} {metric.name} metric"[:240],
                        summary=(f"Observed {metric.name} for {rule_id} in an approved aggregate scope.")[:2000],
                        rule_id=rule_id,
                        metric_name=metric.name,
                        metric_value=metric.value,
                        unit=metric.unit,
                        aggregation_key=group.aggregation_key,
                        source_run_id=execution.run_id,
                        source_contract_version=execution.contract_version,
                        source_reference_id=result.step_id,
                    )


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()[:24]


__all__ = ["FactPackBuildResult", "FactPackBuilder"]
