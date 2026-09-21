"""Canonical workbook rule identifiers and backward-compatible aliases."""

from __future__ import annotations

from types import MappingProxyType


RUNTIME_TO_CANONICAL = MappingProxyType({
    "agent_speaker_tag_validation": "speaker_tag_validation",
    "topic_distribution": "topic_drift",
    "speech_per_duration_rate": "transcript_duration_vs_size",
    "fill_rate": "fill_rate",
    "mistranslated_rate": "non_english_mistranscription",
    "pii_detection": "privacy_detection",
    "non_english_spelling": "spelling_validation",
})

ALIASES = MappingProxyType({
    "speaker_tag_validation": "agent_speaker_tag_validation",
    "agent_speaker_tag_validation": "agent_speaker_tag_validation",
    "topic_drift": "topic_distribution",
    "topic_distribution": "topic_distribution",
    "transcript_duration_vs_size": "speech_per_duration_rate",
    "speech_per_duration_rate": "speech_per_duration_rate",
    "fill_rate": "fill_rate",
    "non_english_mistranscription": "mistranslated_rate",
    "mistranslated_rate": "mistranslated_rate",
    "privacy_detection": "pii_detection",
    "pii_detection": "pii_detection",
    "spelling_validation": "non_english_spelling",
    "non_english_spelling": "non_english_spelling",
})


def normalize_rule_id(value: str) -> str:
    """Return the deployed registry ID for a canonical or legacy alias."""

    normalized = value.strip().casefold().replace("-", "_").replace(" ", "_")
    try:
        return ALIASES[normalized]
    except KeyError as exc:
        raise ValueError(f"Unsupported rule identifier: {value!r}") from exc


def canonical_rule_id(value: str) -> str:
    """Return the workbook-facing identifier for a runtime rule ID."""

    runtime = normalize_rule_id(value)
    return RUNTIME_TO_CANONICAL[runtime]


__all__ = ["ALIASES", "RUNTIME_TO_CANONICAL", "canonical_rule_id", "normalize_rule_id"]
