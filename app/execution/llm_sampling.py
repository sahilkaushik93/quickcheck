"""One-shot, privacy-aware LLM enrichment over a bounded transcript sample.

This module is deliberately outside deterministic handlers. It performs a
second streaming pass, retains only the lowest deterministic hashes per
stratum, redacts common PII, makes exactly one provider request, and returns
aggregate inference only. The result is advisory and never changes rule
outcomes or approves rules.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from app.models.responses import LLMOptions


_REDACTIONS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("EMAIL", re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)),
    ("SSN", re.compile(r"\b\d{3}-?\d{2}-?\d{4}\b")),
    ("PHONE", re.compile(r"(?<!\d)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}(?!\d)")),
    ("LONG_NUMBER", re.compile(r"(?<!\d)\d{8,19}(?!\d)")),
)


@dataclass(frozen=True, slots=True)
class SamplingPolicy:
    maximum_samples: int
    maximum_samples_per_stratum: int
    maximum_characters_per_sample: int
    maximum_prompt_characters: int

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SamplingPolicy":
        policy = cls(
            maximum_samples=int(value.get("maximum_samples", 24)),
            maximum_samples_per_stratum=int(value.get("maximum_samples_per_stratum", 4)),
            maximum_characters_per_sample=int(value.get("maximum_characters_per_sample", 1200)),
            maximum_prompt_characters=int(value.get("maximum_prompt_characters", 30000)),
        )
        if min(
            policy.maximum_samples,
            policy.maximum_samples_per_stratum,
            policy.maximum_characters_per_sample,
            policy.maximum_prompt_characters,
        ) < 1:
            raise ValueError("LLM sampling limits must be positive")
        return policy


@dataclass(frozen=True, slots=True)
class _Sample:
    score: int
    fingerprint: str
    stratum: str
    text: str


def enrich_sampled_transcripts(
    csv_path: Path,
    *,
    llm: LLMOptions,
    source_fingerprint: str,
    column_aliases: Mapping[str, list[str]],
    column_overrides: Mapping[str, str],
    policy_document: Mapping[str, Any],
    chunk_size: int,
) -> dict[str, Any]:
    """Return one advisory LLM assessment, or an explicit unavailable result."""

    if not llm.enabled:
        return {
            "status": "disabled",
            "method": "deterministic_hash_stratified_sample",
            "sample_size": 0,
            "affects_rule_outcomes": False,
        }
    try:
        policy = SamplingPolicy.from_mapping(policy_document)
        samples, rows_seen, bindings = _select_samples(
            csv_path,
            source_fingerprint=source_fingerprint,
            aliases=column_aliases,
            overrides=column_overrides,
            policy=policy,
            chunk_size=chunk_size,
        )
        if not samples:
            return _unavailable("No non-empty transcript was available for sampling.", rows_seen)
        prompt, prompt_sample_count = _build_prompt(samples, policy.maximum_prompt_characters)
        # Import lazily so deterministic execution remains usable when an LLM
        # provider's optional HTTP dependencies are not installed.
        from app.llm_provider import generate_text

        response = generate_text(
            prompt,
            request_id=llm.request_id,
            provider_name=llm.provider,
            model=llm.model,
        )
        assessment = _parse_json(response)
        return {
            "status": "completed",
            "method": "deterministic_hash_stratified_sample",
            "population_rows_seen": rows_seen,
            "sample_size": prompt_sample_count,
            "sample_fraction": round(prompt_sample_count / rows_seen, 8) if rows_seen else 0.0,
            "strata": sorted({sample.stratum for sample in samples[:prompt_sample_count]}),
            "resolved_columns": bindings,
            "provider": llm.provider,
            "model": llm.model,
            "single_request": True,
            "advisory_only": True,
            "affects_rule_outcomes": False,
            "assessment": assessment,
            "limitations": [
                "Inference is based on a bounded sample, not the full population.",
                "Sample text was PII-redacted and truncated before provider submission.",
                "LLM findings are suggestions and require governed review.",
            ],
        }
    except Exception as exc:
        if llm.failure_mode == "raise":
            raise
        return _unavailable(f"LLM enrichment failed ({type(exc).__name__}).", 0)


def _select_samples(
    csv_path: Path,
    *,
    source_fingerprint: str,
    aliases: Mapping[str, list[str]],
    overrides: Mapping[str, str],
    policy: SamplingPolicy,
    chunk_size: int,
) -> tuple[list[_Sample], int, dict[str, str | None]]:
    header = list(pd.read_csv(csv_path, nrows=0).columns)
    bindings = {
        role: _resolve_column(role, header, aliases, overrides)
        for role in ("transcript", "interaction_direction", "interaction_date")
    }
    transcript_column = bindings["transcript"]
    if transcript_column is None:
        return [], 0, bindings
    usecols = sorted({value for value in bindings.values() if value is not None})
    heaps: dict[str, list[tuple[int, str, _Sample]]] = {}
    rows_seen = 0
    for chunk in pd.read_csv(
        csv_path,
        usecols=usecols,
        chunksize=max(1, chunk_size),
        dtype=object,
        keep_default_na=True,
        on_bad_lines="skip",
    ):
        positions = {column: index for index, column in enumerate(chunk.columns)}
        transcript_position = positions[transcript_column]
        direction_position = positions.get(bindings["interaction_direction"]) if bindings["interaction_direction"] else None
        date_position = positions.get(bindings["interaction_date"]) if bindings["interaction_date"] else None
        for row in chunk.itertuples(index=False, name=None):
            ordinal = rows_seen
            rows_seen += 1
            raw = row[transcript_position]
            if pd.isna(raw) or not str(raw).strip():
                continue
            direction = _normalized_scalar(row[direction_position]) if direction_position is not None else "__ALL__"
            month = _month(row[date_position]) if date_position is not None else "__ALL_TIME__"
            stratum = f"{direction}|{month}"
            fingerprint = hashlib.sha256(
                f"{source_fingerprint}|{ordinal}".encode("utf-8")
            ).hexdigest()
            score = int(fingerprint[:16], 16)
            redacted = _redact(str(raw))[: policy.maximum_characters_per_sample]
            sample = _Sample(score, f"sha256:{fingerprint}", stratum, redacted)
            heap = heaps.setdefault(stratum, [])
            item = (-score, fingerprint, sample)
            if len(heap) < policy.maximum_samples_per_stratum:
                heapq.heappush(heap, item)
            elif score < -heap[0][0]:
                heapq.heapreplace(heap, item)
    selected = sorted(
        (item[2] for heap in heaps.values() for item in heap),
        key=lambda item: (item.score, item.fingerprint),
    )[: policy.maximum_samples]
    return selected, rows_seen, bindings


def _resolve_column(
    role: str,
    columns: list[str],
    aliases: Mapping[str, list[str]],
    overrides: Mapping[str, str],
) -> str | None:
    index = {column.casefold(): column for column in columns}
    explicit = overrides.get(role)
    if explicit:
        return index.get(explicit.casefold())
    for candidate in aliases.get(role, []):
        if candidate.casefold() in index:
            return index[candidate.casefold()]
    return None


def _redact(text: str) -> str:
    result = text
    for label, pattern in _REDACTIONS:
        result = pattern.sub(f"[{label}_REDACTED]", result)
    return " ".join(result.split())


def _normalized_scalar(value: object) -> str:
    if value is None or pd.isna(value):
        return "__UNKNOWN__"
    return str(value).strip().casefold() or "__UNKNOWN__"


def _month(value: object) -> str:
    parsed = pd.to_datetime(value, errors="coerce", utc=True)
    return "__UNKNOWN_TIME__" if pd.isna(parsed) else parsed.strftime("%Y-%m")


def _build_prompt(samples: list[_Sample], maximum: int) -> tuple[str, int]:
    instruction = (
        "You are a governed transcript data-quality reviewer. Analyze only the supplied "
        "redacted sample. Return strict JSON, no markdown. Do not reproduce transcript text, "
        "PII, or sample identifiers. Required top-level keys: language_assessment, "
        "speaker_tag_assessment, spelling_assessment, mistranscription_assessment, "
        "topic_assessment, business_inference, limitations. For each assessment include "
        "finding, confidence from 0 to 1, and aggregate counts where supportable. Allowed "
        "language labels are English, Spanish, Mixed, Other, Gibberish. Separate spelling "
        "from grammar, punctuation, capitalization, proper names, and domain terms. Treat "
        "the output as sampled inference, never a population fact. Samples: "
    )
    payload: list[dict[str, str]] = []
    for item in samples:
        candidate = [*payload, {"sample_id": item.fingerprint, "stratum": item.stratum, "text": item.text}]
        serialized = json.dumps(candidate, ensure_ascii=True, separators=(",", ":"))
        if len(instruction) + len(serialized) > maximum:
            break
        payload = candidate
    if not payload:
        available = max(1, maximum - len(instruction) - 200)
        item = samples[0]
        payload = [{"sample_id": item.fingerprint, "stratum": item.stratum, "text": item.text[:available]}]
    serialized = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    return instruction + serialized, len(payload)


def _parse_json(value: str) -> dict[str, Any]:
    text = value.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S)
    document = json.loads(text)
    if not isinstance(document, dict):
        raise ValueError("LLM response must be a JSON object")
    return document


def _unavailable(reason: str, rows_seen: int) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "method": "deterministic_hash_stratified_sample",
        "population_rows_seen": rows_seen,
        "sample_size": 0,
        "advisory_only": True,
        "affects_rule_outcomes": False,
        "reason": reason,
    }


__all__ = ["SamplingPolicy", "enrich_sampled_transcripts"]
