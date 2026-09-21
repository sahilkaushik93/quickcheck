"""Bounded, mergeable and privacy-safe execution evidence collection."""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import Iterable, Mapping

from app.execution.aggregation.accumulators import InternalAggregationKey
from app.execution.models import AggregationKey, EvidenceCollection, RedactedEvidence
from app.understanding.models import JsonScalar, RuleExecutionStep


class EvidenceCollectorError(RuntimeError):
    """Safe structured evidence configuration or collection failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class EvidenceLimits:
    """Caps that make retained evidence independent of source row count."""

    maximum_per_rule_group: int
    maximum_total: int
    maximum_buckets: int
    maximum_metadata_fields: int = 8
    maximum_metadata_string_length: int = 128

    def __post_init__(self) -> None:
        if (
            self.maximum_per_rule_group < 0
            or self.maximum_total < 0
            or self.maximum_buckets < 0
        ):
            raise ValueError("evidence limits cannot be negative")
        if self.maximum_metadata_fields < 0:
            raise ValueError("maximum_metadata_fields cannot be negative")
        if self.maximum_metadata_string_length < 1:
            raise ValueError("maximum_metadata_string_length must be positive")


@dataclass(slots=True)
class _EvidenceBucket:
    rule_id: str
    key: InternalAggregationKey
    observed: int = 0
    items: list[RedactedEvidence] | None = None

    def __post_init__(self) -> None:
        if self.items is None:
            self.items = []


class EvidenceCollector:
    """Collect only salted row references and bounded redacted metadata."""

    _FORBIDDEN_METADATA_KEY = re.compile(
        r"(?:value|text|transcript|match|email|phone|card|account|customer|member|token|password|secret|identifier|raw)",
        re.IGNORECASE,
    )

    def __init__(self, *, salt: bytes, limits: EvidenceLimits) -> None:
        if not salt:
            raise EvidenceCollectorError(
                "EVIDENCE_SALT_REQUIRED",
                "A non-empty evidence fingerprint salt is required.",
            )
        self._salt = bytes(salt)
        self._limits = limits
        self._buckets: dict[tuple[str, InternalAggregationKey], _EvidenceBucket] = {}
        self._retained_total = 0
        self._observed_total = 0

    @property
    def observed_count(self) -> int:
        return self._observed_total

    @property
    def retained_count(self) -> int:
        return self._retained_total

    def observe_failure(
        self,
        *,
        step: RuleExecutionStep,
        aggregation_keys: Iterable[InternalAggregationKey],
        source_fingerprint: str,
        row_ordinal: int,
        failure_type: str,
        category: str | None = None,
        metadata: Mapping[str, JsonScalar] | None = None,
    ) -> None:
        """Record one failure for each applicable overall/group key."""

        if row_ordinal < 0:
            raise EvidenceCollectorError(
                "INVALID_ROW_ORDINAL", "Evidence row ordinal cannot be negative."
            )
        if not re.fullmatch(r"[a-f0-9]{64}", source_fingerprint):
            raise EvidenceCollectorError(
                "INVALID_SOURCE_FINGERPRINT",
                "Evidence requires a lowercase SHA-256 source fingerprint.",
            )
        failure_type = self._safe_label(failure_type, "failure_type")
        category = self._safe_label(category, "category") if category else None
        safe_metadata = self._sanitize_metadata(metadata or {})
        row_fingerprint = self._row_fingerprint(source_fingerprint, row_ordinal)
        for key in tuple(dict.fromkeys(aggregation_keys)):
            self._observed_total += 1
            bucket_key = (step.rule_id, key)
            bucket = self._buckets.get(bucket_key)
            if bucket is None:
                if len(self._buckets) >= self._limits.maximum_buckets:
                    continue
                bucket = _EvidenceBucket(rule_id=step.rule_id, key=key)
                self._buckets[bucket_key] = bucket
            bucket.observed += 1
            assert bucket.items is not None
            if (
                len(bucket.items) >= self._limits.maximum_per_rule_group
                or self._retained_total >= self._limits.maximum_total
            ):
                continue
            evidence_id = self._evidence_id(
                step.step_id,
                key,
                row_fingerprint,
                failure_type,
                category,
            )
            bucket.items.append(
                RedactedEvidence(
                    evidence_id=evidence_id,
                    rule_id=step.rule_id,
                    step_id=step.step_id,
                    aggregation_key=key.to_contract(),
                    row_fingerprint=row_fingerprint,
                    failure_type=failure_type,
                    category=category,
                    metadata=safe_metadata,
                    redacted=True,
                )
            )
            self._retained_total += 1

    def merge(self, other: "EvidenceCollector") -> None:
        """Merge compact worker evidence in deterministic chunk order."""

        if self._limits != other._limits or self._salt != other._salt:
            raise EvidenceCollectorError(
                "INCOMPATIBLE_EVIDENCE_COLLECTORS",
                "Evidence collectors must use identical limits and salt.",
            )
        self._observed_total += other._observed_total
        for bucket_key in sorted(other._buckets, key=self._bucket_sort_key):
            source = other._buckets[bucket_key]
            target = self._buckets.get(bucket_key)
            if target is None:
                if len(self._buckets) >= self._limits.maximum_buckets:
                    continue
                target = _EvidenceBucket(rule_id=source.rule_id, key=source.key)
                self._buckets[bucket_key] = target
            target.observed += source.observed
            assert source.items is not None and target.items is not None
            for item in source.items:
                if (
                    len(target.items) >= self._limits.maximum_per_rule_group
                    or self._retained_total >= self._limits.maximum_total
                ):
                    break
                if any(existing.evidence_id == item.evidence_id for existing in target.items):
                    continue
                target.items.append(item)
                self._retained_total += 1

    def finalize(self) -> tuple[EvidenceCollection, ...]:
        """Return deterministically ordered canonical evidence collections."""

        collections: list[EvidenceCollection] = []
        for bucket_key in sorted(self._buckets, key=self._bucket_sort_key):
            bucket = self._buckets[bucket_key]
            assert bucket.items is not None
            collections.append(
                EvidenceCollection(
                    rule_id=bucket.rule_id,
                    aggregation_key=bucket.key.to_contract(),
                    observed_failure_count=bucket.observed,
                    retained_count=len(bucket.items),
                    truncated=bucket.observed > len(bucket.items),
                    items=list(bucket.items),
                )
            )
        return tuple(collections)

    def _row_fingerprint(self, source_fingerprint: str, row_ordinal: int) -> str:
        digest = hmac.new(
            self._salt,
            f"{source_fingerprint}:{row_ordinal}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"sha256:{digest}"

    @staticmethod
    def _evidence_id(
        step_id: str,
        key: InternalAggregationKey,
        row_fingerprint: str,
        failure_type: str,
        category: str | None,
    ) -> str:
        identity = "|".join(
            (
                step_id,
                repr(key),
                row_fingerprint,
                failure_type,
                category or "",
            )
        )
        return "ev-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]

    def _sanitize_metadata(
        self, metadata: Mapping[str, JsonScalar]
    ) -> dict[str, JsonScalar]:
        if len(metadata) > self._limits.maximum_metadata_fields:
            raise EvidenceCollectorError(
                "EVIDENCE_METADATA_LIMIT_EXCEEDED",
                "Evidence metadata contains too many fields.",
            )
        safe: dict[str, JsonScalar] = {}
        for key, value in sorted(metadata.items(), key=lambda item: item[0].casefold()):
            if self._FORBIDDEN_METADATA_KEY.search(key):
                raise EvidenceCollectorError(
                    "UNSAFE_EVIDENCE_METADATA",
                    "Evidence metadata contains a forbidden field name.",
                )
            if isinstance(value, str):
                if len(value) > self._limits.maximum_metadata_string_length:
                    raise EvidenceCollectorError(
                        "EVIDENCE_METADATA_VALUE_TOO_LONG",
                        "Evidence metadata string exceeds the configured limit.",
                    )
                value = self._safe_label(value, key)
            safe[key] = value
        return safe

    @staticmethod
    def _safe_label(value: str, field: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 128:
            raise EvidenceCollectorError(
                "INVALID_EVIDENCE_LABEL",
                f"Evidence {field} must be a short non-empty label.",
            )
        if not re.fullmatch(r"[A-Za-z0-9_.:/ -]+", normalized):
            raise EvidenceCollectorError(
                "INVALID_EVIDENCE_LABEL",
                f"Evidence {field} contains unsupported characters.",
            )
        return normalized

    @staticmethod
    def _bucket_sort_key(
        item: tuple[str, InternalAggregationKey],
    ) -> tuple[object, ...]:
        rule_id, key = item
        return (
            rule_id.casefold(),
            0 if key.overall else 1,
            key.time_period or "",
            tuple((name, repr(value)) for name, value in key.dimensions),
        )


__all__ = ["EvidenceCollector", "EvidenceCollectorError", "EvidenceLimits"]
