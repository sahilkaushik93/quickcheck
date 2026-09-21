"""Last deterministic disclosure guard for claims and output-ready fact packs."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Pattern

from app.business_impact.evidence.models import EvidenceFactPack
from app.business_impact.models import BusinessImpactWarning, TrustedInsightClaim


@dataclass(frozen=True, slots=True)
class DisclosurePolicy:
    maximum_serialized_bytes: int
    forbidden_patterns: tuple[Pattern[str], ...]

    @classmethod
    def from_file(cls, path: str | Path) -> "DisclosurePolicy":
        try:
            block = json.loads(Path(path).read_text(encoding="utf-8"))["internal_fact_pack"]
            return cls(int(block["maximum_serialized_bytes"]), tuple(re.compile(str(item)) for item in block["forbidden_key_patterns"]))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError("DISCLOSURE_POLICY_LOAD_FAILED") from exc


@dataclass(frozen=True, slots=True)
class PolicyGuardResult:
    claims: tuple[TrustedInsightClaim, ...]
    warnings: tuple[BusinessImpactWarning, ...]
    requires_human_review: bool


class PolicyGuard:
    """Reject unsafe output; does not redact it and then present it as valid."""

    _URL = re.compile(r"https?://|www\.", re.IGNORECASE)
    _PII = re.compile(r"(?:\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b|\b\d{3}[- ]?\d{2}[- ]?\d{4}\b|\b(?:\d[ -]?){13,19}\b|\b\+?\d{1,3}[ -]?\d{10}\b)")
    _INTERNAL = re.compile(r"\b(?:undq|quickcheck|lumi|nexidia|transcript|csv|dataframe)\b", re.IGNORECASE)
    _CAUSAL = re.compile(r"\b(caused|causes|resulted in|led to|because of)\b", re.IGNORECASE)

    def __init__(self, policy: DisclosurePolicy) -> None:
        self._policy = policy

    def validate(self, fact_pack: EvidenceFactPack, claims: Iterable[TrustedInsightClaim]) -> PolicyGuardResult:
        """Confirm fact pack bounds then keep only claims free of forbidden content."""

        serialized = fact_pack.model_dump_json().encode("utf-8")
        if len(serialized) > self._policy.maximum_serialized_bytes:
            return PolicyGuardResult((), (BusinessImpactWarning(code="FACT_PACK_DISCLOSURE_LIMIT_EXCEEDED", message="Sanitized fact pack exceeded the configured disclosure limit.", component="policy_guard"),), True)
        accepted: list[TrustedInsightClaim] = []
        rejected_codes: set[str] = set()
        for claim in claims:
            code = self._claim_violation(claim)
            if code is None:
                accepted.append(claim)
            else:
                rejected_codes.add(code)
        warnings = tuple(BusinessImpactWarning(code=code, message="A trusted-insight claim was rejected by disclosure policy.", component="policy_guard") for code in sorted(rejected_codes))
        return PolicyGuardResult(tuple(accepted), warnings, bool(rejected_codes))

    def _claim_violation(self, claim: TrustedInsightClaim) -> str | None:
        statement = claim.statement
        if self._URL.search(statement):
            return "CLAIM_DIRECT_URL_FORBIDDEN"
        if self._PII.search(statement):
            return "CLAIM_PII_OR_IDENTIFIER_FORBIDDEN"
        if self._INTERNAL.search(statement):
            return "CLAIM_INTERNAL_TERMINOLOGY_FORBIDDEN"
        if self._CAUSAL.search(statement) and not claim.correlation_not_causation:
            return "CLAIM_CAUSAL_CONCLUSION_FORBIDDEN"
        if any(pattern.search(statement) for pattern in self._policy.forbidden_patterns):
            return "CLAIM_DISCLOSURE_PATTERN_FORBIDDEN"
        return None


__all__ = ["DisclosurePolicy", "PolicyGuard", "PolicyGuardResult"]
