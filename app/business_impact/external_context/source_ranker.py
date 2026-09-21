"""Verify and deterministically rank connector-returned public source material."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from app.business_impact.external_context.base import ApprovedSource, ExternalContextPolicyError
from app.business_impact.external_context.models import PublicResearchTopic, PublicSourceDocument, PublicSourcePassage


@dataclass(frozen=True, slots=True)
class SourceRankerConfig:
    """Immutable source and citation policy used for document verification."""
    sources: Mapping[str, ApprovedSource]
    freshness_days: Mapping[str, int]
    maximum_passage_characters: int
    missing_publication_time_behavior: str
    stale_behavior: str

    @classmethod
    def from_files(cls, source_registry_path: str | Path, citation_policy_path: str | Path) -> "SourceRankerConfig":
        try:
            registry = json.loads(Path(source_registry_path).read_text(encoding="utf-8")); citation = json.loads(Path(citation_policy_path).read_text(encoding="utf-8"))
            sources = {str(item["connector_id"]): ApprovedSource(str(item["source_id"]), str(item["connector_id"]), str(item["publisher"]), frozenset(str(x).casefold() for x in item["allowed_hosts"]), frozenset(str(x).casefold() for x in item["allowed_schemes"]), tuple(str(x) for x in item["allowed_path_prefixes"]), frozenset(str(x) for x in item["allowed_document_types"]), str(item["trust_tier"])) for item in registry["sources"]}
            missing = str(citation["missing_publication_time"]["behavior"]); stale = str(citation["stale_source"]["behavior"])
            config = cls(sources, {str(key): int(value) for key, value in citation["freshness_days_by_document_type"].items()}, int(citation["passage_requirements"]["maximum_passage_characters"]), missing, stale)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ExternalContextPolicyError("SOURCE_RANKER_POLICY_LOAD_FAILED", "Source verification policy could not be loaded.") from exc
        if not config.sources or config.maximum_passage_characters < 1 or any(days < 1 for days in config.freshness_days.values()):
            raise ExternalContextPolicyError("SOURCE_RANKER_POLICY_INVALID", "Source verification policy has invalid limits.")
        return config


@dataclass(frozen=True, slots=True)
class VerifiedPublicSource:
    """A connector-returned document that passed allowlist and freshness checks."""
    document: PublicSourceDocument
    passages: tuple[PublicSourcePassage, ...]
    relevance_score: float
    freshness_status: str
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SourceRankingResult:
    verified_sources: tuple[VerifiedPublicSource, ...]
    rejected_document_ids: tuple[str, ...]
    warnings: tuple[str, ...]


class SourceRanker:
    """Validates connector provenance, URL, trust tier, document type and passages."""
    def __init__(self, config: SourceRankerConfig) -> None: self._config = config

    def rank(self, topic: PublicResearchTopic, documents: tuple[PublicSourceDocument, ...], passages: tuple[PublicSourcePassage, ...], *, now: datetime | None = None) -> SourceRankingResult:
        """Return deduplicated verified material in deterministic trust/relevance order."""
        now = now or datetime.now(timezone.utc); by_document: dict[str, list[PublicSourcePassage]] = {}
        for passage in passages: by_document.setdefault(passage.document_id, []).append(passage)
        verified: list[VerifiedPublicSource] = []; rejected: list[str] = []; warnings: list[str] = []; seen_urls: set[str] = set(); seen_hashes: set[str] = set()
        for document in sorted(documents, key=lambda item: item.document_id):
            try:
                source = self._source_for(document); source.validate_url(str(document.canonical_url)); self._verify_document(document, source)
                if str(document.canonical_url) in seen_urls or document.content_sha256 in seen_hashes: raise ExternalContextPolicyError("SOURCE_DUPLICATE", "Connector source duplicates an earlier URL or content hash.")
                selected, relevance = self._select_passages(topic, document, by_document.get(document.document_id, ()))
                if not selected: raise ExternalContextPolicyError("SOURCE_PASSAGE_REJECTED", "Document has no bounded relevant supporting passage.")
                freshness, source_warnings = self._freshness(document, now)
                seen_urls.add(str(document.canonical_url)); seen_hashes.add(document.content_sha256)
                verified.append(VerifiedPublicSource(document, selected, relevance, freshness, source_warnings)); warnings.extend(source_warnings)
            except ExternalContextPolicyError:
                rejected.append(document.document_id)
        verified.sort(key=lambda item: (_trust_rank(str(item.document.trust_tier)), -item.relevance_score, str(item.document.canonical_url)))
        return SourceRankingResult(tuple(verified), tuple(sorted(rejected)), tuple(sorted(set(warnings))))

    def _source_for(self, document: PublicSourceDocument) -> ApprovedSource:
        try: return self._config.sources[document.connector_id]
        except KeyError as exc: raise ExternalContextPolicyError("SOURCE_CONNECTOR_UNAPPROVED", "Document connector is not allowlisted.") from exc

    @staticmethod
    def _verify_document(document: PublicSourceDocument, source: ApprovedSource) -> None:
        if document.publisher != source.publisher or str(document.trust_tier) != source.trust_tier or document.document_type not in source.allowed_document_types:
            raise ExternalContextPolicyError("SOURCE_METADATA_INVALID", "Connector document metadata conflicts with its approved source registration.")

    def _select_passages(self, topic: PublicResearchTopic, document: PublicSourceDocument, candidates: list[PublicSourcePassage] | tuple[PublicSourcePassage, ...]) -> tuple[tuple[PublicSourcePassage, ...], float]:
        query_terms = {item.casefold() for item in re.findall(r"[A-Za-z]{4,}", topic.query_text)}
        scored: list[tuple[float, PublicSourcePassage]] = []
        for passage in candidates:
            if passage.document_id != document.document_id or len(passage.text) > self._config.maximum_passage_characters: continue
            terms = {item.casefold() for item in re.findall(r"[A-Za-z]{4,}", passage.text)}
            overlap = len(query_terms & terms) / len(query_terms) if query_terms else 0.0
            score = passage.relevance_score if passage.relevance_score is not None else overlap
            if score > 0: scored.append((score, passage))
        scored.sort(key=lambda item: (-item[0], item[1].passage_id))
        selected = tuple(item[1] for item in scored[:5])
        return selected, (scored[0][0] if scored else 0.0)

    def _freshness(self, document: PublicSourceDocument, now: datetime) -> tuple[str, tuple[str, ...]]:
        if document.publication_time is None: return "publication_time_missing", ("Verified public source has no publication time and cannot support time-sensitive claims.",)
        maximum_days = self._config.freshness_days.get(document.document_type)
        if maximum_days is None: return "freshness_unclassified", ("Verified public source has no configured freshness classification.",)
        age_days = (now - document.publication_time).total_seconds() / 86_400
        return ("fresh" if age_days <= maximum_days else "stale", () if age_days <= maximum_days else ("Verified public source is stale and cannot be the only citation for a current claim.",))


def _trust_rank(value: str) -> int: return {"primary_authority": 0, "primary_corporate": 1, "government": 2, "approved_secondary": 3}.get(value, 99)
