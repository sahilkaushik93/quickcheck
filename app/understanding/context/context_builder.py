"""Orchestrate metadata, domain and relationship context construction."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.understanding.context.domain_classifier import DomainClassifier
from app.understanding.context.metadata_loader import LoadedMetadata, LumiMetadataLoader
from app.understanding.context.relationship_discovery import RelationshipDiscoverer
from app.understanding.models import (
    DatasetProfile,
    DomainAssignment,
    MetadataKnowledgeBase,
    RelationshipCandidate,
)


class ContextBuildError(RuntimeError):
    """Safe orchestration error without row values or transcript content."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ContextBundle:
    """Internal hand-off consumed by signal detection and rule applicability."""

    metadata: MetadataKnowledgeBase
    domains: tuple[DomainAssignment, ...]
    relationships: tuple[RelationshipCandidate, ...]
    warnings: tuple[str, ...] = ()


class ContextBuilder:
    """Build deterministic context after profiling, with no additional data scan."""

    def __init__(
        self,
        metadata_loader: LumiMetadataLoader,
        domain_classifier: DomainClassifier,
        relationship_discoverer: RelationshipDiscoverer,
    ) -> None:
        self._metadata_loader = metadata_loader
        self._domain_classifier = domain_classifier
        self._relationship_discoverer = relationship_discoverer

    def build(
        self,
        profile: DatasetProfile,
        metadata_dictionary_path: str | Path,
        *,
        dictionary_version: str | None = None,
    ) -> ContextBundle:
        try:
            loaded = self._metadata_loader.load(
                metadata_dictionary_path,
                source_columns={column.column_name for column in profile.columns},
                dictionary_version=dictionary_version,
            )
            return self.build_from_loaded(profile, loaded)
        except ContextBuildError:
            raise
        except Exception as exc:
            raise ContextBuildError(
                "CONTEXT_BUILD_FAILED",
                f"Context construction failed ({type(exc).__name__}).",
            ) from exc

    def build_from_loaded(
        self,
        profile: DatasetProfile,
        metadata: LoadedMetadata,
    ) -> ContextBundle:
        domains = self._domain_classifier.classify(profile, metadata)
        relationships = self._relationship_discoverer.discover(profile, domains)
        warnings = tuple(metadata.knowledge_base.warnings)
        return ContextBundle(
            metadata=metadata.knowledge_base,
            domains=tuple(domains),
            relationships=tuple(relationships),
            warnings=warnings,
        )

