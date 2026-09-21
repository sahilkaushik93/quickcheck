"""Create immutable citations from already verified connector-returned sources."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from app.business_impact.external_context.models import Citation
from app.business_impact.external_context.source_ranker import VerifiedPublicSource


class CitationStoreError(ValueError):
    """Safe error for invalid citation operations; never rewrites or echoes URLs."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code; self.message = message; super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class CitationStoreConfig:
    policy_version: str
    minimum_passages: int
    maximum_passages: int
    maximum_citations_per_claim: int
    maximum_citations: int
    require_connector_returned_url: bool

    @classmethod
    def from_file(cls, path: str | Path) -> "CitationStoreConfig":
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8")); requirements = document["citation_requirements"]; passages = document["passage_requirements"]
            config = cls(str(document["policy_version"]), int(passages["minimum_passages_per_citation"]), int(passages["maximum_passages_per_citation"]), int(requirements["maximum_citations_per_claim"]), int(requirements["maximum_claims_per_report"]) * int(requirements["maximum_citations_per_claim"]), bool(requirements["citation_must_reference_connector_returned_url"]))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise CitationStoreError("CITATION_POLICY_LOAD_FAILED", "Citation policy could not be loaded.") from exc
        if config.minimum_passages < 1 or config.maximum_passages < config.minimum_passages or config.maximum_citations < 1:
            raise CitationStoreError("CITATION_POLICY_INVALID", "Citation policy has invalid bounds.")
        return config


class CitationStore:
    """Request-scoped immutable citation registry whose URLs originate only in connectors."""
    def __init__(self, config: CitationStoreConfig, sources: tuple[VerifiedPublicSource, ...]) -> None:
        self._config = config; citations: dict[str, Citation] = {}
        seen_urls: set[str] = set(); seen_hashes: set[str] = set()
        for source in sources:
            document = source.document
            if str(document.canonical_url) in seen_urls or document.content_sha256 in seen_hashes: continue
            passage_ids = [item.passage_id for item in source.passages[:config.maximum_passages]]
            if len(passage_ids) < config.minimum_passages: continue
            citation_id = "cite-" + sha256((document.document_id + "|" + document.content_sha256).encode()).hexdigest()[:24]
            citations[citation_id] = Citation(citation_id=citation_id, document_id=document.document_id, passage_ids=passage_ids, publisher=document.publisher, title=document.title, canonical_url=document.canonical_url, trust_tier=document.trust_tier, publication_time=document.publication_time)
            seen_urls.add(str(document.canonical_url)); seen_hashes.add(document.content_sha256)
            if len(citations) >= config.maximum_citations: break
        self._citations = MappingProxyType(dict(sorted(citations.items())))

    @property
    def citations(self) -> tuple[Citation, ...]: return tuple(self._citations.values())

    def resolve(self, citation_id: str) -> Citation:
        try: return self._citations[citation_id]
        except KeyError as exc: raise CitationStoreError("CITATION_UNKNOWN", "Claim referenced a citation ID that was not issued by the validated citation store.") from exc

    def validate_references(self, citation_ids: list[str] | tuple[str, ...]) -> tuple[Citation, ...]:
        if len(citation_ids) > self._config.maximum_citations_per_claim: raise CitationStoreError("CITATION_LIMIT_EXCEEDED", "Claim exceeds the configured citation limit.")
        if len(set(citation_ids)) != len(citation_ids): raise CitationStoreError("CITATION_DUPLICATE", "Claim references the same citation more than once.")
        return tuple(self.resolve(item) for item in citation_ids)
