"""Build one bounded LLM prompt from sanitized facts and verified passages."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.business_impact.evidence.models import EvidenceFactPack
from app.business_impact.external_context.models import Citation, ExternalContextBundle


class PromptBuildError(ValueError):
    """Safe prompt-build failure without echoing protected prompt content."""


@dataclass(frozen=True, slots=True)
class TrustedInsightPrompt:
    """Bounded prompt with the allowable reference IDs retained for validation."""

    text: str
    internal_fact_ids: frozenset[str]
    citation_ids: frozenset[str]


class TrustedInsightPromptBuilder:
    """Constructs a compact LLM payload; it never includes source URLs or rows."""

    def __init__(self, template_path: str | Path, *, maximum_facts: int, maximum_passages: int, maximum_characters: int) -> None:
        if min(maximum_facts, maximum_passages, maximum_characters) < 1:
            raise ValueError("prompt limits must be positive")
        try:
            self._template = Path(template_path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise PromptBuildError("Trusted-insight prompt template could not be loaded.") from exc
        if not self._template:
            raise PromptBuildError("Trusted-insight prompt template is empty.")
        self._maximum_facts = maximum_facts
        self._maximum_passages = maximum_passages
        self._maximum_characters = maximum_characters

    def build(self, fact_pack: EvidenceFactPack, context: ExternalContextBundle) -> TrustedInsightPrompt:
        """Build a JSON-backed prompt using only allowed facts and citations."""

        citations = {item.citation_id: item for item in context.citations}
        selected_facts = sorted(fact_pack.facts, key=lambda item: item.fact_id)[: self._maximum_facts]
        selected_passages = sorted(context.passages, key=lambda item: item.passage_id)[: self._maximum_passages]
        passage_payload = []
        for passage in selected_passages:
            matching_citations = sorted(
                citation_id for citation_id, citation in citations.items() if passage.passage_id in citation.passage_ids
            )
            if matching_citations:
                passage_payload.append({"citation_ids": matching_citations, "passage_id": passage.passage_id, "text": passage.text})
        payload = {
            "internal_facts": [
                {"fact_id": fact.fact_id, "kind": str(fact.kind), "summary": fact.summary, "rule_id": fact.rule_id, "metric_name": fact.metric_name, "metric_value": fact.metric_value, "unit": fact.unit}
                for fact in selected_facts
            ],
            "verified_public_passages": passage_payload,
            "approved_recommendation_notice": "Only catalogue-backed recommendations may be treated as approved; every other recommendation requires human review.",
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        text = self._template + "\n\nSANITIZED_INPUT_JSON:\n" + encoded
        if len(text) > self._maximum_characters:
            raise PromptBuildError("Bounded trusted-insight prompt exceeds configured character limit.")
        return TrustedInsightPrompt(
            text=text,
            internal_fact_ids=frozenset(item.fact_id for item in selected_facts),
            citation_ids=frozenset(citations),
        )


__all__ = ["PromptBuildError", "TrustedInsightPrompt", "TrustedInsightPromptBuilder"]
