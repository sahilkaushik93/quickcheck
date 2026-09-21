"""Parse and strictly validate the one structured trusted-insight LLM response."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from app.business_impact.models import InsightClaimType, TrustedInsightClaim
from app.business_impact.insights.prompt_builder import TrustedInsightPrompt


class TrustedInsightResponseError(ValueError):
    """Safe response rejection; it never returns the raw model response."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class TrustedInsightResponse:
    claims: tuple[TrustedInsightClaim, ...]


class TrustedInsightResponseParser:
    """Reject fabricated identifiers, URLs and prohibited control fields."""

    _CODE_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)
    _FORBIDDEN_KEYS = frozenset({"url", "urls", "source_url", "canonical_url", "trust_tier", "score", "confidence", "weight", "threshold", "impact_level"})

    def __init__(self, *, maximum_claims: int) -> None:
        if maximum_claims < 1:
            raise ValueError("maximum_claims must be positive")
        self._maximum_claims = maximum_claims

    def parse(self, raw_text: str, allowed: TrustedInsightPrompt) -> TrustedInsightResponse:
        """Validate JSON and all claim references against prompt-issued IDs."""

        content = raw_text.strip()
        match = self._CODE_FENCE.fullmatch(content)
        if match:
            content = match.group(1).strip()
        try:
            data = json.loads(content)
        except (TypeError, json.JSONDecodeError) as exc:
            raise TrustedInsightResponseError("LLM_RESPONSE_INVALID_JSON", "Trusted insight response was not valid JSON.") from exc
        if not isinstance(data, Mapping) or set(data) != {"claims"} or not isinstance(data["claims"], list):
            raise TrustedInsightResponseError("LLM_RESPONSE_SCHEMA_INVALID", "Trusted insight response must contain only a claims list.")
        if len(data["claims"]) > self._maximum_claims:
            raise TrustedInsightResponseError("LLM_RESPONSE_LIMIT_EXCEEDED", "Trusted insight response exceeds the configured claim limit.")
        claims: list[TrustedInsightClaim] = []
        for item in data["claims"]:
            if not isinstance(item, Mapping) or self._contains_forbidden_key(item):
                raise TrustedInsightResponseError("LLM_RESPONSE_POLICY_REJECTED", "Trusted insight response contains prohibited output fields.")
            try:
                claim = TrustedInsightClaim.model_validate(item)
            except Exception as exc:
                raise TrustedInsightResponseError("LLM_RESPONSE_SCHEMA_INVALID", "Trusted insight response contains an invalid claim.") from exc
            if not set(claim.internal_fact_ids).issubset(allowed.internal_fact_ids):
                raise TrustedInsightResponseError("LLM_FACT_REFERENCE_INVALID", "Trusted insight response referenced an unissued internal fact ID.")
            if not set(claim.external_citation_ids).issubset(allowed.citation_ids):
                raise TrustedInsightResponseError("LLM_CITATION_REFERENCE_INVALID", "Trusted insight response referenced an unissued citation ID.")
            if claim.claim_type == InsightClaimType.FACT and not claim.external_citation_ids:
                raise TrustedInsightResponseError("LLM_FACT_CITATION_MISSING", "External factual claims require verified citations.")
            claims.append(claim)
        if len({claim.claim_id for claim in claims}) != len(claims):
            raise TrustedInsightResponseError("LLM_CLAIM_ID_DUPLICATE", "Trusted insight response contains duplicate claim IDs.")
        return TrustedInsightResponse(tuple(claims))

    def _contains_forbidden_key(self, value: Mapping[str, Any]) -> bool:
        for key, item in value.items():
            if str(key).casefold() in self._FORBIDDEN_KEYS:
                return True
            if isinstance(item, Mapping) and self._contains_forbidden_key(item):
                return True
            if isinstance(item, list) and any(isinstance(child, Mapping) and self._contains_forbidden_key(child) for child in item):
                return True
        return False


__all__ = ["TrustedInsightResponse", "TrustedInsightResponseError", "TrustedInsightResponseParser"]
