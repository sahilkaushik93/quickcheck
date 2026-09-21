"""Discover explainable relationship candidates from bounded profile state."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.understanding.models import (
    ColumnProfile,
    DatasetProfile,
    DomainAssignment,
    EvidenceReference,
    RelationshipCandidate,
    RelationshipType,
)


@dataclass(frozen=True, slots=True)
class RelationshipDiscoveryConfig:
    max_relationship_pairs: int = 250
    max_evidence_items: int = 5
    identifier_uniqueness_threshold: float = 0.995
    categorical_max_distinct_ratio: float = 0.05
    transcript_duration_minimum_coverage: float = 0.8

    @classmethod
    def from_mapping(cls, values: dict[str, object]) -> "RelationshipDiscoveryConfig":
        return cls(
            max_relationship_pairs=int(values.get("max_relationship_pairs", 250)),
            max_evidence_items=int(values.get("max_relationship_evidence_items", 5)),
            identifier_uniqueness_threshold=float(values.get("identifier_uniqueness_threshold", 0.995)),
            categorical_max_distinct_ratio=float(values.get("categorical_max_distinct_ratio", 0.05)),
            transcript_duration_minimum_coverage=float(values.get("transcript_duration_minimum_coverage", 0.8)),
        )


class RelationshipDiscoverer:
    """Generate candidates only when available aggregate evidence supports them."""

    def __init__(self, config: RelationshipDiscoveryConfig) -> None:
        self._config = config

    def discover(
        self,
        profile: DatasetProfile,
        domains: list[DomainAssignment],
    ) -> list[RelationshipCandidate]:
        domain_by_column = {item.column_name.casefold(): item.domain for item in domains}
        candidates: list[RelationshipCandidate] = []
        for column in profile.columns:
            candidates.extend(self._single_column_candidates(column, domain_by_column))
        candidates.extend(self._transcript_duration_candidates(profile, domain_by_column))
        candidates.extend(self._speaker_candidates(profile, domain_by_column))
        unique = {candidate.relationship_id: candidate for candidate in candidates}
        ranked = sorted(unique.values(), key=lambda item: (-item.confidence, item.relationship_id))
        return ranked[: self._config.max_relationship_pairs]

    def _single_column_candidates(
        self,
        column: ColumnProfile,
        domains: dict[str, str],
    ) -> list[RelationshipCandidate]:
        if not column.non_null_count or column.distinct_count is None:
            return []
        ratio = column.distinct_count / column.non_null_count
        results: list[RelationshipCandidate] = []
        domain = domains.get(column.column_name.casefold(), "unknown")
        if ratio >= self._config.identifier_uniqueness_threshold:
            results.append(
                self._candidate(
                    RelationshipType.IDENTIFIER,
                    [column.column_name],
                    [],
                    min(1.0, ratio),
                    "exact_distinct_ratio",
                    f"Column is near-unique with a distinct ratio of {ratio:.4f}.",
                    [self._evidence("distinct_ratio", ratio, [column.column_name])],
                )
            )
        if 1 < column.distinct_count and ratio <= self._config.categorical_max_distinct_ratio:
            results.append(
                self._candidate(
                    RelationshipType.CATEGORICAL_DIMENSION,
                    [column.column_name],
                    [],
                    min(0.95, 1.0 - ratio),
                    "exact_distinct_ratio",
                    f"Column behaves as a reusable categorical dimension in domain {domain}.",
                    [self._evidence("distinct_ratio", ratio, [column.column_name])],
                )
            )
        return results

    def _transcript_duration_candidates(
        self,
        profile: DatasetProfile,
        domains: dict[str, str],
    ) -> list[RelationshipCandidate]:
        transcripts = [c for c in profile.columns if domains.get(c.column_name.casefold()) == "conversation.transcript"]
        durations = [c for c in profile.columns if domains.get(c.column_name.casefold()) == "interaction.duration"]
        results: list[RelationshipCandidate] = []
        for transcript in transcripts:
            for duration in durations:
                coverage = min(transcript.fill_rate, duration.fill_rate)
                if coverage < self._config.transcript_duration_minimum_coverage:
                    continue
                results.append(
                    self._candidate(
                        RelationshipType.TRANSCRIPT_DURATION,
                        [transcript.column_name],
                        [duration.column_name],
                        min(0.9, coverage),
                        "domain_and_coverage",
                        "Transcript and duration columns have compatible domains and sufficient joint coverage for speech-rate validation.",
                        [self._evidence("minimum_fill_rate", coverage, [transcript.column_name, duration.column_name])],
                    )
                )
        return results

    def _speaker_candidates(
        self,
        profile: DatasetProfile,
        domains: dict[str, str],
    ) -> list[RelationshipCandidate]:
        speakers = [c for c in profile.columns if domains.get(c.column_name.casefold()) == "conversation.speaker"]
        tagged = [c for c in profile.columns if "speaker_labels" in c.detected_patterns]
        return [
            self._candidate(
                RelationshipType.SPEAKER_STRUCTURE,
                [transcript.column_name],
                [speaker.column_name],
                min(0.9, transcript.fill_rate, speaker.fill_rate),
                "speaker_pattern_and_domain",
                "Speaker labels were detected in transcript aggregates and a speaker-domain column is available.",
                [self._evidence("speaker_pattern", True, [transcript.column_name, speaker.column_name])],
            )
            for transcript in tagged
            for speaker in speakers
            if transcript.column_name != speaker.column_name
        ]

    def _candidate(
        self,
        kind: RelationshipType,
        source: list[str],
        target: list[str],
        confidence: float,
        method: str,
        rationale: str,
        evidence: list[EvidenceReference],
    ) -> RelationshipCandidate:
        identity = "|".join([str(kind), *sorted(source), "->", *sorted(target)])
        relationship_id = "rel-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        return RelationshipCandidate(
            relationship_id=relationship_id,
            relationship_type=kind,
            source_columns=source,
            target_columns=target,
            confidence=round(confidence, 6),
            method=method,
            evidence=evidence[: self._config.max_evidence_items],
            rationale=rationale,
            requires_review=True,
        )

    @staticmethod
    def _evidence(metric: str, value: object, columns: list[str]) -> EvidenceReference:
        return EvidenceReference(
            evidence_type="aggregate_metric",
            summary=f"Aggregate {metric} supports this candidate.",
            metric_name=metric,
            metric_value=value,  # type: ignore[arg-type]
            column_names=columns,
            redacted=True,
        )

