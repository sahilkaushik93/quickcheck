"""Retrieve only approved public material through configured connectors."""

from __future__ import annotations

from dataclasses import dataclass

from app.business_impact.external_context.base import ConnectorResponse, ExternalContextPolicyError
from app.business_impact.external_context.models import PublicResearchTopic, PublicSourceDocument, PublicSourcePassage, RetrievalProvenance
from app.business_impact.external_context.query_guard import PublicQueryGuard
from app.business_impact.external_context.registry import ConnectorRegistry


@dataclass(frozen=True, slots=True)
class RetrievedPublicMaterial:
    """Unverified connector output held separately from internal fact packs."""

    retrievals: tuple[RetrievalProvenance, ...]
    documents: tuple[PublicSourceDocument, ...]
    passages: tuple[PublicSourcePassage, ...]
    warnings: tuple[str, ...]


class PublicContextRetriever:
    """Performs bounded retrieval without accepting an internal run/result object."""

    def __init__(self, registry: ConnectorRegistry, query_guard: PublicQueryGuard, *, maximum_topics: int, maximum_connectors_per_topic: int) -> None:
        if min(maximum_topics, maximum_connectors_per_topic) < 1:
            raise ValueError("retrieval limits must be positive")
        self._registry = registry
        self._guard = query_guard
        self._maximum_topics = maximum_topics
        self._maximum_connectors_per_topic = maximum_connectors_per_topic

    def retrieve(self, topics: tuple[PublicResearchTopic, ...]) -> RetrievedPublicMaterial:
        """Call only registered, topic-approved connectors in stable order."""

        retrievals: list[RetrievalProvenance] = []
        documents: list[PublicSourceDocument] = []
        passages: list[PublicSourcePassage] = []
        warnings: list[str] = []
        for topic in sorted(topics, key=lambda item: item.topic_id)[: self._maximum_topics]:
            self._guard.validate_topic(topic)
            for connector_id in sorted(topic.approved_source_ids)[: self._maximum_connectors_per_topic]:
                try:
                    connector = self._registry.connector_for(connector_id)
                    response = connector.retrieve(topic, source=self._registry.source_for(connector_id), limits=self._registry.limits)
                    self._append_response(response, retrievals, documents, passages)
                except ExternalContextPolicyError as exc:
                    warnings.append(f"Public retrieval unavailable for configured connector {connector_id}: {exc.code}.")
        return RetrievedPublicMaterial(tuple(retrievals), tuple(documents), tuple(passages), tuple(sorted(set(warnings))))

    @staticmethod
    def _append_response(response: ConnectorResponse, retrievals: list[RetrievalProvenance], documents: list[PublicSourceDocument], passages: list[PublicSourcePassage]) -> None:
        retrievals.append(response.retrieval)
        documents.extend(response.documents)
        passages.extend(response.passages)


__all__ = ["PublicContextRetriever", "RetrievedPublicMaterial"]
