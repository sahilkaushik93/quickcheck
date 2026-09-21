"""Single-pass, deterministic extraction of shared transcript features."""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from app.execution.features.models import (
    ChunkFeatureStatistics,
    FeatureColumnBindings,
    FeatureConfigurationError,
    FeatureExtractionError,
    FeatureRequirements,
    RowFeatures,
    SpeakerTurnSummary,
    TranscriptFeatures,
)
from app.understanding.models import DomainAssignment, JsonScalar, RequiredCheck, RuleExecutionStep
from app.understanding.source_adapters.base import TabularChunk


_URL_PATTERN = re.compile(r"\b(?:https?://|www\.)\S+", re.IGNORECASE)
_EMAIL_LIKE_PATTERN = re.compile(r"\b\S+@\S+\b")


@dataclass(frozen=True, slots=True)
class CompiledPIIPattern:
    category: str
    pattern: re.Pattern[str]
    validator: str


@dataclass(frozen=True, slots=True)
class MistranslationPattern:
    category: str
    pattern: re.Pattern[str]


@dataclass(frozen=True, slots=True)
class FeatureExtractorConfig:
    """Immutable validated extraction settings assembled from JSON policies."""

    column_aliases: Mapping[str, tuple[str, ...]]
    token_pattern: re.Pattern[str]
    unicode_normalization: str
    case_sensitive_tokens: bool
    minimum_token_length: int
    maximum_tokens_per_transcript: int
    maximum_transcript_characters: int
    exclude_urls: bool
    exclude_email_like_tokens: bool
    redaction_markers: tuple[str, ...]
    speaker_roles: Mapping[str, frozenset[str]]
    speaker_case_sensitive: bool
    required_speaker_roles: frozenset[str]
    pii_patterns: tuple[CompiledPIIPattern, ...]
    mistranslation_patterns: tuple[MistranslationPattern, ...]

    @classmethod
    def from_mappings(
        cls,
        *,
        execution: Mapping[str, Any],
        language: Mapping[str, Any],
        speaker_tags: Mapping[str, Any],
        pii: Mapping[str, Any],
        mistranslations: Mapping[str, Any],
    ) -> "FeatureExtractorConfig":
        """Compile repository policies and expose only safe error messages."""

        try:
            aliases = {
                str(role): tuple(str(value) for value in values)
                for role, values in execution.get("column_resolution", {}).items()
            }
            token_policy = language.get("token_policy", {})
            detection = language.get("detection", {})
            token_pattern = re.compile(str(token_policy["token_pattern"]))
            normalization = str(detection.get("unicode_normalization", "NFKC"))
            unicodedata.normalize(normalization, "validation")

            role_case_sensitive = bool(speaker_tags.get("case_sensitive", False))
            roles: dict[str, frozenset[str]] = {}
            for role, labels in speaker_tags.get("canonical_roles", {}).items():
                normalized = (
                    str(label) if role_case_sensitive else str(label).casefold()
                    for label in labels
                )
                roles[str(role)] = frozenset(normalized)

            compiled_pii: list[CompiledPIIPattern] = []
            for category, definition in pii.get("categories", {}).items():
                if not definition.get("enabled", False):
                    continue
                validator = str(definition.get("validator", "none"))
                for pattern in definition.get("patterns", []):
                    compiled_pii.append(
                        CompiledPIIPattern(
                            category=str(category),
                            pattern=re.compile(str(pattern), re.IGNORECASE),
                            validator=validator,
                        )
                    )

            case_sensitive_mistranslations = bool(
                mistranslations.get("case_sensitive", False)
            )
            flags = 0 if case_sensitive_mistranslations else re.IGNORECASE
            whole_word = bool(mistranslations.get("whole_word_matching", True))
            compiled_mistranslations: list[MistranslationPattern] = []
            for entry in mistranslations.get("entries", []):
                if not entry.get("enabled", False):
                    continue
                category = str(entry.get("category", "uncategorized"))
                for suspected in entry.get("suspected_forms", []):
                    escaped = re.escape(str(suspected))
                    expression = rf"(?<!\w){escaped}(?!\w)" if whole_word else escaped
                    compiled_mistranslations.append(
                        MistranslationPattern(category, re.compile(expression, flags))
                    )

            bounded = execution.get("bounded_state", {})
            return cls(
                column_aliases=MappingProxyType(aliases),
                token_pattern=token_pattern,
                unicode_normalization=normalization,
                case_sensitive_tokens=bool(detection.get("case_sensitive", False)),
                minimum_token_length=max(
                    1, int(detection.get("minimum_token_length", 1))
                ),
                maximum_tokens_per_transcript=min(
                    int(token_policy.get("maximum_tokens_per_transcript", 10_000)),
                    int(bounded.get("max_tokens_inspected_per_transcript", 10_000)),
                ),
                maximum_transcript_characters=int(
                    bounded.get("max_transcript_characters_inspected", 1_000_000)
                ),
                exclude_urls=bool(token_policy.get("exclude_urls", True)),
                exclude_email_like_tokens=bool(
                    token_policy.get("exclude_email_like_tokens", True)
                ),
                redaction_markers=tuple(
                    str(value)
                    for value in pii.get("false_positive_controls", {}).get(
                        "redaction_markers", []
                    )
                ),
                speaker_roles=MappingProxyType(roles),
                speaker_case_sensitive=role_case_sensitive,
                required_speaker_roles=frozenset(
                    str(value)
                    for value in speaker_tags.get("validation", {}).get(
                        "required_roles", []
                    )
                ),
                pii_patterns=tuple(compiled_pii),
                mistranslation_patterns=tuple(compiled_mistranslations),
            )
        except (KeyError, TypeError, ValueError, re.error) as exc:
            raise FeatureConfigurationError(
                "INVALID_FEATURE_CONFIGURATION",
                f"Feature extraction configuration is invalid ({type(exc).__name__}).",
            ) from exc

    def __post_init__(self) -> None:
        if self.maximum_tokens_per_transcript < 1:
            raise FeatureConfigurationError(
                "INVALID_TOKEN_LIMIT", "Maximum tokens per transcript must be positive."
            )
        if self.maximum_transcript_characters < 1:
            raise FeatureConfigurationError(
                "INVALID_TEXT_LIMIT", "Maximum inspected characters must be positive."
            )
        unknown_validators = {
            item.validator
            for item in self.pii_patterns
            if item.validator
            not in {"none", "digit_count", "luhn", "ssn_structure"}
        }
        if unknown_validators:
            raise FeatureConfigurationError(
                "UNSUPPORTED_PII_VALIDATOR",
                "PII configuration references an unsupported deterministic validator.",
            )


class FeatureExtractor:
    """Build a one-shot feature stream for one bounded ``TabularChunk``."""

    _CHECK_REQUIREMENTS: Mapping[str, FeatureRequirements] = MappingProxyType(
        {
            RequiredCheck.FILL_RATE.value: FeatureRequirements(),
            RequiredCheck.TOPIC_DISTRIBUTION.value: FeatureRequirements(),
            RequiredCheck.AGENT_SPEAKER_TAG_VALIDATION.value: FeatureRequirements(
                inspect_speaker_tags=True
            ),
            RequiredCheck.NON_ENGLISH_SPELLING.value: FeatureRequirements(tokenize=True),
            RequiredCheck.MISTRANSLATED_RATE.value: FeatureRequirements(
                tokenize=True, inspect_mistranslations=True
            ),
            RequiredCheck.PII_DETECTION.value: FeatureRequirements(inspect_pii=True),
            RequiredCheck.SPEECH_PER_DURATION_RATE.value: FeatureRequirements(
                calculate_speech_statistics=True
            ),
        }
    )

    def __init__(self, config: FeatureExtractorConfig) -> None:
        self._config = config

    def requirements_for_steps(
        self, steps: Sequence[RuleExecutionStep]
    ) -> FeatureRequirements:
        """Union requirements for approved runnable plan steps only."""

        required = [
            self._CHECK_REQUIREMENTS.get(str(step.check_name), FeatureRequirements())
            for step in steps
        ]
        return FeatureRequirements(
            tokenize=any(item.tokenize for item in required),
            inspect_speaker_tags=any(item.inspect_speaker_tags for item in required),
            inspect_pii=any(item.inspect_pii for item in required),
            inspect_mistranslations=any(item.inspect_mistranslations for item in required),
            calculate_speech_statistics=any(
                item.calculate_speech_statistics for item in required
            ),
        )

    def resolve_bindings(
        self,
        *,
        column_names: Sequence[str],
        requirements: FeatureRequirements,
        domains: Sequence[DomainAssignment] = (),
        overrides: Mapping[str, str] | None = None,
        projection_columns: Sequence[str] = (),
    ) -> FeatureColumnBindings:
        """Resolve physical feature columns without hardcoded LUMI assumptions."""

        index = {name.casefold(): name for name in column_names}
        if len(index) != len(column_names):
            raise FeatureConfigurationError(
                "AMBIGUOUS_SOURCE_SCHEMA",
                "Source columns must be unique when compared case-insensitively.",
            )
        override_map = {
            role.casefold(): column for role, column in (overrides or {}).items()
        }
        semantic: dict[str, list[str]] = {}
        for assignment in domains:
            for key in (
                assignment.semantic_type,
                assignment.domain,
                assignment.subdomain,
            ):
                if key:
                    semantic.setdefault(key.casefold(), []).append(assignment.column_name)

        transcript = self._resolve_role(
            "transcript", index, semantic, override_map, required=requirements.transcript_required
        )
        duration = self._resolve_role(
            "duration",
            index,
            semantic,
            override_map,
            required=requirements.calculate_speech_statistics,
        )
        transcript_length = self._resolve_role(
            "transcript_length", index, semantic, override_map, required=False
        )
        topic = self._resolve_role("topic", index, semantic, override_map, required=False)
        resolved_projection: list[str] = []
        for column in projection_columns:
            actual = index.get(column.casefold())
            if actual is None:
                raise FeatureConfigurationError(
                    "PROJECTION_COLUMN_NOT_FOUND",
                    "A required aggregation or rule column is absent from the chunk schema.",
                    column=column,
                )
            resolved_projection.append(actual)
        return FeatureColumnBindings(
            transcript=transcript,
            duration=duration,
            transcript_length=transcript_length,
            topic=topic,
            projection_columns=tuple(dict.fromkeys(resolved_projection)),
        )

    def extract_chunk(
        self,
        chunk: TabularChunk,
        *,
        bindings: FeatureColumnBindings,
        requirements: FeatureRequirements,
    ) -> "_ChunkFeatureStream":
        """Return a lazy, single-use stream backed by the current chunk."""

        missing = set(bindings.required_columns()) - set(chunk.column_names)
        if missing:
            raise FeatureExtractionError(
                "CHUNK_SCHEMA_MISMATCH",
                "The chunk is missing one or more resolved feature columns.",
            )
        return _ChunkFeatureStream(self, chunk, bindings, requirements)

    def _resolve_role(
        self,
        role: str,
        columns: Mapping[str, str],
        semantic: Mapping[str, list[str]],
        overrides: Mapping[str, str],
        *,
        required: bool,
    ) -> str | None:
        if role.casefold() in overrides:
            requested = overrides[role.casefold()]
            actual = columns.get(requested.casefold())
            if actual is None:
                raise FeatureConfigurationError(
                    "FEATURE_OVERRIDE_NOT_FOUND",
                    "An explicit feature-column override is absent from the schema.",
                    column=requested,
                )
            return actual
        semantic_matches = sorted(
            {
                columns[value.casefold()]
                for value in semantic.get(role.casefold(), [])
                if value.casefold() in columns
            },
            key=str.casefold,
        )
        if len(semantic_matches) == 1:
            return semantic_matches[0]
        if len(semantic_matches) > 1:
            raise FeatureConfigurationError(
                "AMBIGUOUS_FEATURE_COLUMN",
                f"Semantic role {role!r} matches multiple columns; provide an override.",
            )
        for alias in self._config.column_aliases.get(role, ()):
            actual = columns.get(alias.casefold())
            if actual is not None:
                return actual
        if required:
            raise FeatureConfigurationError(
                "REQUIRED_FEATURE_COLUMN_NOT_FOUND",
                f"Required semantic role {role!r} could not be resolved.",
            )
        return None

    def _extract_transcript(
        self, raw_value: object, requirements: FeatureRequirements
    ) -> TranscriptFeatures:
        text = self._text_value(raw_value)
        if text is None:
            return TranscriptFeatures(present=False)
        observed_length = len(text)
        inspected = text[: self._config.maximum_transcript_characters]
        text_truncated = observed_length > len(inspected)

        pii_counts: Counter[str] = Counter()
        pii_spans: list[tuple[int, int]] = []
        if requirements.inspect_pii or requirements.tokenize:
            for definition in self._config.pii_patterns:
                for match in definition.pattern.finditer(inspected):
                    if self._valid_pii(match.group(0), definition.validator):
                        if requirements.inspect_pii:
                            pii_counts[definition.category] += 1
                        pii_spans.append(match.span())

        mistranslation_counts: Counter[str] = Counter()
        if requirements.inspect_mistranslations:
            for definition in self._config.mistranslation_patterns:
                count = sum(1 for _ in definition.pattern.finditer(inspected))
                if count:
                    mistranslation_counts[definition.category] += count

        speaker = (
            self._speaker_summary(inspected)
            if requirements.inspect_speaker_tags
            else SpeakerTurnSummary()
        )
        alphabetic = sum(character.isalpha() for character in inspected)
        non_ascii = sum(character.isalpha() and not character.isascii() for character in inspected)
        tokens: tuple[str, ...] = ()
        token_count = 0
        tokens_truncated = False
        if requirements.tokenize:
            token_source = self._mask_sensitive_regions(inspected, pii_spans)
            if self._config.exclude_urls:
                token_source = _URL_PATTERN.sub(" ", token_source)
            if self._config.exclude_email_like_tokens:
                token_source = _EMAIL_LIKE_PATTERN.sub(" ", token_source)
            for marker in self._config.redaction_markers:
                token_source = token_source.replace(marker, " ")
            retained: list[str] = []
            for match in self._config.token_pattern.finditer(token_source):
                token = unicodedata.normalize(
                    self._config.unicode_normalization, match.group(0)
                )
                if not self._config.case_sensitive_tokens:
                    token = token.casefold()
                if len(token) < self._config.minimum_token_length:
                    continue
                token_count += 1
                if len(retained) < self._config.maximum_tokens_per_transcript:
                    retained.append(token)
                else:
                    tokens_truncated = True
            tokens = tuple(retained)

        return TranscriptFeatures(
            present=True,
            inspected_character_count=len(inspected),
            declared_or_observed_character_count=observed_length,
            token_count=token_count,
            alphabetic_character_count=alphabetic,
            non_ascii_alphabetic_character_count=non_ascii,
            normalized_tokens=tokens,
            tokens_truncated=tokens_truncated,
            text_truncated=text_truncated,
            speaker=speaker,
            pii_category_counts=tuple(sorted(pii_counts.items())),
            mistranslation_category_counts=tuple(
                sorted(mistranslation_counts.items())
            ),
        )

    def _speaker_summary(self, text: str) -> SpeakerTurnSummary:
        role_counts: Counter[str] = Counter()
        unknown = 0
        empty = 0
        total = 0
        label_to_role = {
            label: role
            for role, labels in self._config.speaker_roles.items()
            for label in labels
        }
        # Known labels are detected at line starts *or inline*. Nexidia exports
        # commonly serialize ``Agent: ... Customer: ...`` on one physical line.
        # Restricting inline matching to the governed label ontology avoids
        # treating ordinary colon-bearing prose as a speaker turn.
        labels = sorted(label_to_role, key=len, reverse=True)
        known_pattern = re.compile(
            rf"(?:^|(?<=\n)|(?<=\r)|(?<=\s))(?:\[\s*)?"
            rf"(?P<label>{'|'.join(re.escape(item) for item in labels)})"
            rf"(?:\s*\])?\s*(?:[:\-])\s*",
            0 if self._config.speaker_case_sensitive else re.I,
        )
        matches = list(known_pattern.finditer(text))
        for index, match in enumerate(matches):
            total += 1
            label = match.group("label").strip()
            if not self._config.speaker_case_sensitive:
                label = label.casefold()
            role = label_to_role.get(label)
            if role is None:
                unknown += 1
            else:
                role_counts[role] += 1
            body_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            if not text[match.end():body_end].strip():
                empty += 1
        return SpeakerTurnSummary(
            total_turns=total,
            agent_turns=role_counts["agent"],
            customer_turns=role_counts["customer"],
            system_turns=role_counts["system"],
            unknown_label_count=unknown,
            empty_turn_count=empty,
            required_roles_present=self._config.required_speaker_roles.issubset(
                {role for role, count in role_counts.items() if count > 0}
            ),
        )

    @staticmethod
    def _mask_sensitive_regions(text: str, spans: Sequence[tuple[int, int]]) -> str:
        if not spans:
            return text
        characters = list(text)
        for start, end in spans:
            characters[start:end] = " " * (end - start)
        return "".join(characters)

    @staticmethod
    def _valid_pii(value: str, validator: str) -> bool:
        if validator == "none":
            return True
        digits = "".join(character for character in value if character.isdigit())
        if validator == "digit_count":
            return 10 <= len(digits) <= 11
        if validator == "luhn":
            if not 13 <= len(digits) <= 19:
                return False
            total = 0
            parity = len(digits) % 2
            for index, character in enumerate(digits):
                number = int(character)
                if index % 2 == parity:
                    number *= 2
                    if number > 9:
                        number -= 9
                total += number
            return total % 10 == 0
        if validator == "ssn_structure":
            if len(digits) != 9:
                return False
            area, group, serial = digits[:3], digits[3:5], digits[5:]
            return area not in {"000", "666"} and not area.startswith("9") and group != "00" and serial != "0000"
        return False

    @staticmethod
    def _text_value(value: object) -> str | None:
        if value is None:
            return None
        try:
            if value != value:
                return None
        except (TypeError, ValueError):
            pass
        text = str(value)
        return text if text.strip() else None

    @staticmethod
    def _safe_scalar(value: object) -> tuple[JsonScalar, bool]:
        if value is None:
            return None, False
        try:
            if value != value:
                return None, False
        except (TypeError, ValueError):
            pass
        if isinstance(value, (str, bool, int)):
            return value, False
        if isinstance(value, float):
            return (value, False) if math.isfinite(value) else (None, True)
        if hasattr(value, "item"):
            try:
                return FeatureExtractor._safe_scalar(value.item())
            except (TypeError, ValueError, OverflowError):
                return None, True
        return str(value), False


class _ChunkFeatureStream:
    """Concrete single-use stream; it never retains emitted row features."""

    def __init__(
        self,
        extractor: FeatureExtractor,
        chunk: TabularChunk,
        bindings: FeatureColumnBindings,
        requirements: FeatureRequirements,
    ) -> None:
        self._extractor = extractor
        self._chunk = chunk
        self._bindings = bindings
        self._requirements = requirements
        self._statistics = ChunkFeatureStatistics()
        self._consumed = False
        self._columns = {
            column: chunk.column_values(column)
            for column in bindings.required_columns()
        }

    @property
    def chunk_index(self) -> int:
        return self._chunk.chunk_index

    @property
    def row_count(self) -> int:
        return self._chunk.row_count

    @property
    def statistics(self) -> ChunkFeatureStatistics:
        return self._statistics

    @property
    def consumed(self) -> bool:
        return self._consumed

    def __iter__(self) -> Iterator[RowFeatures]:
        if self._consumed:
            raise FeatureExtractionError(
                "FEATURE_STREAM_ALREADY_CONSUMED",
                "A chunk feature stream can only be consumed once.",
            )
        self._consumed = True
        transcript_values = (
            self._columns[self._bindings.transcript]
            if self._bindings.transcript is not None
            else None
        )
        projection = tuple(
            dict.fromkeys(
                (
                    self._bindings.duration,
                    self._bindings.transcript_length,
                    self._bindings.topic,
                    *self._bindings.projection_columns,
                )
            )
        )
        projection = tuple(value for value in projection if value is not None)
        try:
            for index in range(self._chunk.row_count):
                raw_transcript = transcript_values[index] if transcript_values is not None else None
                transcript = self._extractor._extract_transcript(
                    raw_transcript, self._requirements
                )
                values: dict[str, JsonScalar] = {}
                for column in projection:
                    safe_value, invalid = self._extractor._safe_scalar(
                        self._columns[column][index]
                    )
                    values[column] = safe_value
                    if invalid:
                        self._statistics.invalid_scalar_values += 1
                self._statistics.rows_emitted += 1
                if not transcript.present:
                    self._statistics.missing_transcript_rows += 1
                if transcript.text_truncated:
                    self._statistics.text_truncated_rows += 1
                if transcript.tokens_truncated:
                    self._statistics.token_truncated_rows += 1
                if transcript.pii_candidate_count:
                    self._statistics.pii_candidate_rows += 1
                yield RowFeatures(
                    row_ordinal=self._chunk.row_offset + index,
                    chunk_row_index=index,
                    transcript=transcript,
                    values=MappingProxyType(values),
                )
        except FeatureExtractionError:
            raise
        except Exception as exc:
            raise FeatureExtractionError(
                "FEATURE_EXTRACTION_FAILED",
                f"Chunk feature extraction failed ({type(exc).__name__}).",
            ) from exc


__all__ = ["FeatureExtractor", "FeatureExtractorConfig"]
