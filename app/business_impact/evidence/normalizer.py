"""Bounded normalization of already-redacted Execution evidence."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from app.business_impact.evidence.models import (
    EvidenceSensitivity,
    InternalFact,
    InternalFactKind,
    NormalizedEvidence,
)
from app.business_impact.evidence.provenance import (
    EvidenceProvenanceBuilder,
    EvidenceProvenanceError,
)
from app.execution.models import EvidenceCollection, ExecutionOutput
from app.execution.rule_ids import canonical_rule_id


class EvidenceNormalizationError(ValueError):
    """Safe normalization error that never includes evidence values."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class EvidenceNormalizerConfig:
    """Configuration-derived bounds for normalized internal evidence."""

    maximum_normalized_evidence: int
    maximum_fact_summary_characters: int
    maximum_evidence_summary_characters: int
    required_redaction: bool
    allow_raw_transcript: bool
    allow_raw_source_row: bool
    allow_raw_pii_match: bool

    @classmethod
    def from_mapping(cls, document: Mapping[str, Any]) -> "EvidenceNormalizerConfig":
        try:
            policy = document["internal_fact_pack"]
            config = cls(
                maximum_normalized_evidence=int(policy["maximum_normalized_evidence"]),
                maximum_fact_summary_characters=int(policy["maximum_fact_summary_characters"]),
                maximum_evidence_summary_characters=int(
                    policy["maximum_evidence_summary_characters"]
                ),
                required_redaction=bool(policy["required_redaction"]),
                allow_raw_transcript=bool(policy["allow_raw_transcript"]),
                allow_raw_source_row=bool(policy["allow_raw_source_row"]),
                allow_raw_pii_match=bool(policy["allow_raw_pii_match"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise EvidenceNormalizationError(
                "INVALID_EVIDENCE_POLICY",
                "Evidence policy is missing required normalization limits.",
            ) from exc
        if config.maximum_normalized_evidence < 1:
            raise EvidenceNormalizationError(
                "INVALID_EVIDENCE_POLICY",
                "maximum_normalized_evidence must be positive.",
            )
        if min(
            config.maximum_fact_summary_characters,
            config.maximum_evidence_summary_characters,
        ) < 64:
            raise EvidenceNormalizationError(
                "INVALID_EVIDENCE_POLICY",
                "Evidence summary limits must be at least 64 characters.",
            )
        if (
            config.allow_raw_transcript
            or config.allow_raw_source_row
            or config.allow_raw_pii_match
        ):
            raise EvidenceNormalizationError(
                "UNSAFE_EVIDENCE_POLICY",
                "Business Impact evidence policy must prohibit raw source, transcript and PII data.",
            )
        return config

    @classmethod
    def from_file(cls, path: Path) -> "EvidenceNormalizerConfig":
        """Load immutable normalizer controls from the approved JSON policy."""

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EvidenceNormalizationError(
                "EVIDENCE_POLICY_LOAD_FAILED",
                "Evidence policy could not be loaded as valid JSON.",
            ) from exc
        if not isinstance(payload, Mapping):
            raise EvidenceNormalizationError(
                "INVALID_EVIDENCE_POLICY",
                "Evidence policy root must be an object.",
            )
        return cls.from_mapping(payload)


@dataclass(frozen=True, slots=True)
class EvidenceNormalizationResult:
    """Bounded normalized records plus explicit truncation diagnostics."""

    normalized_evidence: tuple[NormalizedEvidence, ...]
    source_collection_count: int
    normalized_collection_count: int
    dropped_collection_count: int
    truncated: bool
    warnings: tuple[str, ...] = ()


class EvidenceNormalizer:
    """Normalize aggregate execution evidence without accessing the input source."""

    _ROW_FINGERPRINT = re.compile(r"^sha256:[a-f0-9]{64}$")
    _FORBIDDEN_METADATA = re.compile(
        r"(?:value|text|transcript|match|email|phone|card|account|customer|"
        r"identifier|password|secret|token|raw)",
        re.IGNORECASE,
    )

    def __init__(self, config: EvidenceNormalizerConfig) -> None:
        self._config = config

    def normalize(self, execution: ExecutionOutput) -> EvidenceNormalizationResult:
        """Convert compact execution evidence collections to safe fact records.

        Collections are sorted deterministically by canonical rule and aggregate
        key.  No row is read, and no row fingerprint or item metadata is copied
        into the returned fact payload.
        """

        builder = EvidenceProvenanceBuilder(execution)
        self._validate_summary_counts(execution)
        ordered = sorted(
            execution.evidence,
            key=lambda item: (
                canonical_rule_id(item.rule_id),
                self._aggregation_sort_key(item),
            ),
        )
        retained = ordered[: self._config.maximum_normalized_evidence]
        normalized = tuple(
            self._normalize_collection(execution, builder, collection, index)
            for index, collection in enumerate(retained)
        )
        dropped = len(ordered) - len(retained)
        warnings: list[str] = []
        if dropped:
            warnings.append(
                "Normalized evidence was capped by the approved evidence policy."
            )
        return EvidenceNormalizationResult(
            normalized_evidence=normalized,
            source_collection_count=len(ordered),
            normalized_collection_count=len(normalized),
            dropped_collection_count=dropped,
            truncated=bool(dropped),
            warnings=tuple(warnings),
        )

    def _normalize_collection(
        self,
        execution: ExecutionOutput,
        builder: EvidenceProvenanceBuilder,
        collection: EvidenceCollection,
        index: int,
    ) -> NormalizedEvidence:
        self._validate_collection(collection)
        lineage = builder.lineage_for(collection.rule_id)
        reference = self._collection_reference(collection, index)
        provenance = builder.build(collection.rule_id, collection_reference=reference)
        fact_id = "fact-" + self._digest(
            f"{execution.run_id}|{lineage.canonical_rule_id}|{reference}"
        )
        evidence_id = "bie-" + self._digest(f"{fact_id}|{provenance.provenance_id}")
        scope = self._scope_label(collection)
        summary = (
            f"{lineage.canonical_rule_id} recorded "
            f"{collection.observed_failure_count} aggregate failure observation(s) "
            f"for {scope}; {collection.retained_count} bounded evidence item(s) were retained."
        )
        summary = summary[: self._config.maximum_evidence_summary_characters]
        metric_value = {
            "observed_failure_count": collection.observed_failure_count,
            "retained_count": collection.retained_count,
            "source_truncated": collection.truncated,
            "normalization_position": index,
        }
        fact = InternalFact(
            fact_id=fact_id,
            kind=InternalFactKind.EXECUTION_EVIDENCE_SUMMARY,
            title=(
                f"{lineage.canonical_rule_id} redacted execution evidence summary"
            )[: self._config.maximum_fact_summary_characters],
            summary=summary,
            rule_id=lineage.canonical_rule_id,
            metric_name="observed_failure_count",
            metric_value=metric_value,
            unit="count",
            aggregation_key=collection.aggregation_key,
            source_run_id=execution.run_id,
            source_contract_version=execution.contract_version,
            source_reference_id=reference,
            sensitivity=EvidenceSensitivity.INTERNAL_REDACTED,
            redacted=True,
        )
        return NormalizedEvidence(
            evidence_id=evidence_id,
            fact=fact,
            provenance=provenance,
            supporting_fact_ids=[],
        )

    def _validate_summary_counts(self, execution: ExecutionOutput) -> None:
        observed = sum(item.observed_failure_count for item in execution.evidence)
        retained = sum(item.retained_count for item in execution.evidence)
        if retained != execution.summary.evidence_retained:
            raise EvidenceNormalizationError(
                "EXECUTION_EVIDENCE_RETAINED_COUNT_MISMATCH",
                "Execution evidence retained count is inconsistent with its summary.",
            )
        if observed > execution.summary.evidence_observed:
            raise EvidenceNormalizationError(
                "EXECUTION_EVIDENCE_OBSERVED_COUNT_MISMATCH",
                "Execution evidence observed count exceeds its summary.",
            )

    def _validate_collection(self, collection: EvidenceCollection) -> None:
        for item in collection.items:
            if not item.redacted or not self._ROW_FINGERPRINT.fullmatch(item.row_fingerprint):
                raise EvidenceNormalizationError(
                    "UNSAFE_EXECUTION_EVIDENCE",
                    "Execution evidence must contain valid redacted fingerprints only.",
                )
            if any(self._FORBIDDEN_METADATA.search(key) for key in item.metadata):
                raise EvidenceNormalizationError(
                    "UNSAFE_EXECUTION_EVIDENCE_METADATA",
                    "Execution evidence metadata contains a forbidden field.",
                )

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _aggregation_sort_key(collection: EvidenceCollection) -> str:
        key = collection.aggregation_key
        return json.dumps(
            {
                "overall": key.overall,
                "time_period": key.time_period,
                "dimensions": key.dimensions,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    def _collection_reference(self, collection: EvidenceCollection, index: int) -> str:
        return "evidence-collection-" + self._digest(
            f"{collection.rule_id}|{self._aggregation_sort_key(collection)}|{index}"
        )

    @staticmethod
    def _scope_label(collection: EvidenceCollection) -> str:
        key = collection.aggregation_key
        if key.overall:
            return "the overall population"
        parts = ["an approved aggregate group"]
        if key.time_period:
            parts.append("with a time period")
        if key.dimensions:
            parts.append("with approved dimensions")
        return " ".join(parts)


__all__ = [
    "EvidenceNormalizationError",
    "EvidenceNormalizationResult",
    "EvidenceNormalizer",
    "EvidenceNormalizerConfig",
]
