"""Deterministic weighted semantic-domain classification."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.understanding.context.metadata_loader import LoadedMetadata, MetadataRecord
from app.understanding.models import (
    ColumnProfile,
    ConfidenceLevel,
    DatasetProfile,
    DomainAssignment,
    ProvenanceType,
)


class DomainClassificationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DomainClassifierConfig:
    minimum_confidence: float
    high_confidence: float
    ambiguity_margin: float
    unmatched_domain: str
    weights: dict[str, float]
    domains: tuple[dict[str, Any], ...]

    @classmethod
    def from_mapping(cls, ontology: dict[str, Any]) -> "DomainClassifierConfig":
        classification = ontology.get("classification", {})
        domains = tuple(ontology.get("domains", []))
        if not domains:
            raise DomainClassificationError("Domain ontology contains no domains.")
        weights = {str(k): float(v) for k, v in classification.get("weights", {}).items()}
        if not weights or sum(weights.values()) <= 0:
            raise DomainClassificationError("Domain ontology contains invalid weights.")
        return cls(
            minimum_confidence=float(classification.get("minimum_confidence", 0.55)),
            high_confidence=float(classification.get("high_confidence", 0.8)),
            ambiguity_margin=float(classification.get("ambiguity_margin", 0.1)),
            unmatched_domain=str(classification.get("unmatched_domain", "unknown")),
            weights=weights,
            domains=domains,
        )


class DomainClassifier:
    """Classify every profile column using explainable weighted evidence."""

    def __init__(self, config: DomainClassifierConfig) -> None:
        self._config = config

    def classify(
        self,
        profile: DatasetProfile,
        metadata: LoadedMetadata,
    ) -> list[DomainAssignment]:
        metadata_by_column = metadata.by_column
        all_tokens = {
            token
            for column in profile.columns
            for token in self._tokens(column.column_name)
        }
        return [
            self._classify_column(column, metadata_by_column.get(column.column_name.casefold()), all_tokens)
            for column in profile.columns
        ]

    def _classify_column(
        self,
        column: ColumnProfile,
        metadata: MetadataRecord | None,
        all_tokens: set[str],
    ) -> DomainAssignment:
        candidates = [self._score(column, metadata, domain, all_tokens) for domain in self._config.domains]
        candidates.sort(key=lambda item: (-item[0], str(item[1].get("domain", ""))))
        best_score, best, features = candidates[0]
        second_score = candidates[1][0] if len(candidates) > 1 else 0.0
        ambiguous = best_score - second_score < self._config.ambiguity_margin
        matched = best_score >= self._config.minimum_confidence
        domain_name = str(best.get("domain")) if matched else self._config.unmatched_domain
        semantic = self._semantic_type(column, metadata, best) if matched else None
        provenance = [ProvenanceType.CONFIGURATION, ProvenanceType.DETERMINISTIC]
        if metadata:
            provenance.append(ProvenanceType.METADATA)
        if column.detected_patterns or column.inferred_type_confidence:
            provenance.append(ProvenanceType.STATISTICAL)
        confidence_level = (
            ConfidenceLevel.HIGH
            if best_score >= self._config.high_confidence and not ambiguous
            else ConfidenceLevel.MEDIUM
            if matched
            else ConfidenceLevel.LOW
        )
        return DomainAssignment(
            column_name=column.column_name,
            domain=domain_name,
            semantic_type=semantic,
            confidence=round(best_score, 6),
            confidence_level=confidence_level,
            provenance=list(dict.fromkeys(provenance)),
            matched_features=features,
            rationale=(
                f"Matched {domain_name} using {', '.join(features) or 'insufficient deterministic evidence'}."
            ),
            requires_review=not matched or ambiguous,
        )

    def _score(
        self,
        column: ColumnProfile,
        metadata: MetadataRecord | None,
        domain: dict[str, Any],
        all_tokens: set[str],
    ) -> tuple[float, dict[str, Any], list[str]]:
        name_tokens = self._tokens(column.column_name)
        description_tokens = self._tokens(
            " ".join(filter(None, [metadata.business_name if metadata else None, metadata.description if metadata else None]))
        )
        domain_name_tokens = set(map(str.casefold, domain.get("name_tokens", [])))
        phrase_tokens = self._tokens(" ".join(map(str, domain.get("description_phrases", []))))
        pattern_tokens = self._tokens(" ".join(column.detected_patterns))
        hint_tokens = self._tokens(" ".join(map(str, domain.get("value_hints", []))))
        logical_types = {str(value).casefold() for value in domain.get("logical_types", [])}

        effective_type = (
            metadata.logical_type.value
            if metadata and metadata.logical_type != "unknown"
            else str(column.inferred_data_type)
        )
        components = {
            "column_name": self._evidence_match(name_tokens, domain_name_tokens),
            "metadata_description": self._evidence_match(description_tokens, phrase_tokens | domain_name_tokens),
            "observed_pattern": self._evidence_match(pattern_tokens, hint_tokens | domain_name_tokens, target_hits=1),
            "logical_data_type": 1.0 if effective_type.casefold() in logical_types else 0.0,
            "related_columns": self._overlap(all_tokens, domain_name_tokens),
        }
        total_weight = sum(self._config.weights.values())
        score = sum(self._config.weights.get(name, 0.0) * value for name, value in components.items()) / total_weight
        features = [name for name, value in components.items() if value > 0]
        return score, domain, features

    @staticmethod
    def _semantic_type(
        column: ColumnProfile,
        metadata: MetadataRecord | None,
        domain: dict[str, Any],
    ) -> str | None:
        semantic_types = [str(value) for value in domain.get("semantic_types", [])]
        if not semantic_types:
            return None
        evidence = DomainClassifier._tokens(
            " ".join(filter(None, [column.column_name, metadata.business_name if metadata else None, metadata.description if metadata else None]))
        )
        ranked = sorted(
            semantic_types,
            key=lambda value: (-DomainClassifier._overlap(evidence, DomainClassifier._tokens(value)), value),
        )
        return ranked[0]

    @staticmethod
    def _tokens(value: str) -> set[str]:
        return set(re.findall(r"[a-z0-9]+", value.casefold()))

    @staticmethod
    def _overlap(left: set[str], right: set[str]) -> float:
        if not left or not right:
            return 0.0
        return len(left & right) / min(len(left), len(right))

    @staticmethod
    def _evidence_match(left: set[str], right: set[str], target_hits: int = 2) -> float:
        """Reward a small number of strong ontology matches without long-text dilution."""

        if not left or not right:
            return 0.0
        return min(1.0, len(left & right) / max(1, target_hits))
