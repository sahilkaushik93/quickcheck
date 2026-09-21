"""Map verified public context to internal conditions inside the trusted boundary."""

from __future__ import annotations

from dataclasses import dataclass

from app.business_impact.external_context.models import ExternalContextBundle
from app.business_impact.graph.nodes.research_planner import ResearchCondition


@dataclass(frozen=True, slots=True)
class ContextMapping:
    """Non-causal linkage; it makes no financial, regulatory or customer claim."""

    canonical_rule_id: str
    condition_id: str
    citation_ids: tuple[str, ...]
    statement: str
    correlation_not_causation: bool = True


class ContextMapper:
    """Creates bounded internal mappings only after sources are verified."""

    def __init__(self, *, maximum_mappings: int = 20) -> None:
        if maximum_mappings < 1:
            raise ValueError("maximum_mappings must be positive")
        self._maximum_mappings = maximum_mappings

    def map(self, conditions: tuple[ResearchCondition, ...], context: ExternalContextBundle) -> tuple[ContextMapping, ...]:
        """Attach issued citation IDs to conditions without asserting causality."""

        citation_ids = tuple(sorted(item.citation_id for item in context.citations))
        if not citation_ids:
            return ()
        mappings: list[ContextMapping] = []
        for condition in sorted(set(conditions), key=lambda item: (item.canonical_rule_id, item.condition_id))[: self._maximum_mappings]:
            mappings.append(ContextMapping(
                canonical_rule_id=condition.canonical_rule_id,
                condition_id=condition.condition_id,
                citation_ids=citation_ids,
                statement="Verified public context is relevant to this approved technical condition; the association is non-causal.",
            ))
        return tuple(mappings)


__all__ = ["ContextMapper", "ContextMapping"]
