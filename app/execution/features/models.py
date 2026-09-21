"""Typed, runtime-only contracts for shared transcript chunk features.

These contracts intentionally describe transient execution state, not public
API or artifact payloads. They never contain a full transcript, a source row,
an interaction/customer identifier, or a raw PII match.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Iterator, Mapping, Protocol, TypeAlias

from app.understanding.models import JsonScalar


class FeatureConfigurationError(ValueError):
    """Safe configuration/column error suitable for API translation."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        column: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.column = column

    def as_dict(self) -> dict[str, str]:
        result = {"code": self.code, "message": self.message}
        if self.column is not None:
            result["column"] = self.column
        return result


class FeatureExtractionError(RuntimeError):
    """Safe exception raised when bounded feature extraction cannot continue."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def as_dict(self) -> dict[str, str | bool]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }


@dataclass(frozen=True, slots=True)
class FeatureRequirements:
    """Features required by the currently approved execution-plan steps."""

    tokenize: bool = False
    inspect_speaker_tags: bool = False
    inspect_pii: bool = False
    inspect_mistranslations: bool = False
    calculate_speech_statistics: bool = False

    @property
    def transcript_required(self) -> bool:
        return any(
            (
                self.tokenize,
                self.inspect_speaker_tags,
                self.inspect_pii,
                self.inspect_mistranslations,
                self.calculate_speech_statistics,
            )
        )


@dataclass(frozen=True, slots=True)
class FeatureColumnBindings:
    """Resolved physical columns required by extraction and handlers."""

    transcript: str | None = None
    duration: str | None = None
    transcript_length: str | None = None
    topic: str | None = None
    projection_columns: tuple[str, ...] = ()

    def required_columns(self) -> tuple[str, ...]:
        ordered = (
            self.transcript,
            self.duration,
            self.transcript_length,
            self.topic,
            *self.projection_columns,
        )
        return tuple(dict.fromkeys(value for value in ordered if value is not None))


@dataclass(frozen=True, slots=True)
class SpeakerTurnSummary:
    """Canonical speaker structure without turn text or raw labels."""

    total_turns: int = 0
    agent_turns: int = 0
    customer_turns: int = 0
    system_turns: int = 0
    unknown_label_count: int = 0
    empty_turn_count: int = 0
    required_roles_present: bool = False


CategoryCounts: TypeAlias = tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class TranscriptFeatures:
    """Compact derived properties for one transcript cell.

    ``normalized_tokens`` contains only bounded alphabetic tokens from text
    after configured PII regions, URLs, emails and redaction markers have been
    masked. It is transient and must never be persisted or returned by an API.
    """

    present: bool
    inspected_character_count: int = 0
    declared_or_observed_character_count: int = 0
    token_count: int = 0
    alphabetic_character_count: int = 0
    non_ascii_alphabetic_character_count: int = 0
    normalized_tokens: tuple[str, ...] = ()
    tokens_truncated: bool = False
    text_truncated: bool = False
    speaker: SpeakerTurnSummary = SpeakerTurnSummary()
    pii_category_counts: CategoryCounts = ()
    mistranslation_category_counts: CategoryCounts = ()

    @property
    def pii_candidate_count(self) -> int:
        return sum(count for _, count in self.pii_category_counts)

    @property
    def mistranslation_count(self) -> int:
        return sum(count for _, count in self.mistranslation_category_counts)

    @property
    def non_ascii_alphabetic_rate(self) -> float:
        if self.alphabetic_character_count == 0:
            return 0.0
        return self.non_ascii_alphabetic_character_count / self.alphabetic_character_count


@dataclass(frozen=True, slots=True)
class RowFeatures:
    """One transient row projection shared by every active rule handler."""

    row_ordinal: int
    chunk_row_index: int
    transcript: TranscriptFeatures
    values: Mapping[str, JsonScalar] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def value(self, column: str) -> JsonScalar:
        return self.values.get(column)


@dataclass(slots=True)
class ChunkFeatureStatistics:
    """Operational extraction counts populated as the one-shot stream runs."""

    rows_emitted: int = 0
    missing_transcript_rows: int = 0
    text_truncated_rows: int = 0
    token_truncated_rows: int = 0
    pii_candidate_rows: int = 0
    invalid_scalar_values: int = 0

    def merge(self, other: "ChunkFeatureStatistics") -> None:
        self.rows_emitted += other.rows_emitted
        self.missing_transcript_rows += other.missing_transcript_rows
        self.text_truncated_rows += other.text_truncated_rows
        self.token_truncated_rows += other.token_truncated_rows
        self.pii_candidate_rows += other.pii_candidate_rows
        self.invalid_scalar_values += other.invalid_scalar_values


class ChunkFeatureStream(Protocol):
    """Single-consumption iterable returned for one bounded source chunk."""

    @property
    def chunk_index(self) -> int: ...

    @property
    def row_count(self) -> int: ...

    @property
    def statistics(self) -> ChunkFeatureStatistics: ...

    @property
    def consumed(self) -> bool: ...

    def __iter__(self) -> Iterator[RowFeatures]: ...


__all__ = [
    "CategoryCounts",
    "ChunkFeatureStatistics",
    "ChunkFeatureStream",
    "FeatureColumnBindings",
    "FeatureConfigurationError",
    "FeatureExtractionError",
    "FeatureRequirements",
    "RowFeatures",
    "SpeakerTurnSummary",
    "TranscriptFeatures",
]
