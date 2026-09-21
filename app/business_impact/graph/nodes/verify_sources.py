"""Verify, rank and cite public material after connector retrieval."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from app.business_impact.external_context.citation_store import CitationStore
from app.business_impact.external_context.models import ExternalContextBundle, PublicResearchTopic
from app.business_impact.external_context.source_ranker import SourceRanker
from app.business_impact.graph.nodes.retrieve_context import RetrievedPublicMaterial


@dataclass(frozen=True, slots=True)
class SourceVerificationResult:
    """Verified public context suitable for a later fact-pack/LLM handoff."""

    external_context: ExternalContextBundle
    warnings: tuple[str, ...]


class SourceVerificationNode:
    """Ranks one generic topic at a time and retains only issued citations."""

    def __init__(self, ranker: SourceRanker, *, maximum_documents: int, maximum_passages: int, maximum_citations: int) -> None:
        if min(maximum_documents, maximum_passages, maximum_citations) < 1:
            raise ValueError("source verification limits must be positive")
        self._ranker = ranker
        self._maximum_documents = maximum_documents
        self._maximum_passages = maximum_passages
        self._maximum_citations = maximum_citations

    def verify(self, topic: PublicResearchTopic, material: RetrievedPublicMaterial, citation_store: CitationStore) -> SourceVerificationResult:
        """Deduplicate and verify connector material without changing its URLs."""

        ranking = self._ranker.rank(topic, material.documents, material.passages)
        verified = ranking.verified_sources[: self._maximum_documents]
        allowed_documents = [item.document for item in verified]
        allowed_ids = {item.document.document_id for item in verified}
        allowed_passages = [passage for item in verified for passage in item.passages if passage.document_id in allowed_ids][: self._maximum_passages]
        citations = list(citation_store.citations)[: self._maximum_citations]
        citations = [citation for citation in citations if citation.document_id in allowed_ids]
        bundle = ExternalContextBundle(
            bundle_id="external-context-" + sha256(topic.topic_id.encode("utf-8")).hexdigest()[:24],
            retrievals=list(material.retrievals),
            documents=allowed_documents,
            passages=allowed_passages,
            citations=citations,
            truncated=len(verified) < len(ranking.verified_sources) or len(allowed_passages) < sum(len(item.passages) for item in verified),
            warnings=list(sorted(set(material.warnings + ranking.warnings))),
        )
        return SourceVerificationResult(bundle, tuple(bundle.warnings))


__all__ = ["SourceVerificationNode", "SourceVerificationResult"]
