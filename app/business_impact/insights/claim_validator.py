"""Deterministically validate LLM claims against issued facts and citations."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from app.business_impact.evidence.models import EvidenceFactPack
from app.business_impact.external_context.citation_store import CitationStore, CitationStoreError
from app.business_impact.external_context.models import ExternalContextBundle
from app.business_impact.models import InsightClaimType, TrustedInsightClaim


@dataclass(frozen=True, slots=True)
class ClaimValidationResult:
    """Accepted claims and safe rejection codes, in deterministic input order."""

    accepted: tuple[TrustedInsightClaim, ...]
    rejected_claim_ids: tuple[str, ...]
    rejection_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ClaimValidationPolicy:
    maximum_claims: int
    minimum_regulatory_citations: int
    require_external_fact_citation: bool

    @classmethod
    def from_file(cls, path: str | Path) -> "ClaimValidationPolicy":
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))["citation_requirements"]
            return cls(int(data["maximum_claims_per_report"]), int(data["minimum_verified_citations_for_regulatory_claim"]), bool(data["external_factual_claim_requires_verified_citation"]))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError("CLAIM_VALIDATION_POLICY_LOAD_FAILED") from exc


class ClaimValidator:
    """Rejects unsupported claims; it never repairs or invents support."""

    _WORD = re.compile(r"[a-zA-Z]{4,}")
    _STOPWORDS = frozenset({"that", "this", "with", "from", "have", "will", "should", "could", "their", "there", "where", "which", "into", "only", "than", "been", "were", "about", "using", "under", "does", "does", "such"})
    _PROHIBITED_OUTCOME = re.compile(r"\b(breach|penalt(?:y|ies)|fine[ds]?|customer\s+(?:loss|harm)|financial\s+loss|revenue\s+loss)\b", re.IGNORECASE)
    _REGULATORY = re.compile(r"\b(regulator(?:y)?|compliance|enforcement|supervisory)\b", re.IGNORECASE)

    def __init__(self, policy: ClaimValidationPolicy) -> None:
        self._policy = policy

    def validate(self, claims: Iterable[TrustedInsightClaim], *, fact_pack: EvidenceFactPack, context: ExternalContextBundle, citation_store: CitationStore) -> ClaimValidationResult:
        """Validate support and classify only citation-store-issued claims as accepted."""

        fact_ids = {item.fact_id for item in fact_pack.facts}
        passage_by_id = {item.passage_id: item.text for item in context.passages}
        claims_list = list(claims)
        if len(claims_list) > self._policy.maximum_claims:
            return ClaimValidationResult((), tuple(item.claim_id for item in claims_list), ("CLAIM_LIMIT_EXCEEDED",))
        accepted: list[TrustedInsightClaim] = []
        rejected: list[str] = []
        codes: list[str] = []
        for claim in claims_list:
            code = self._validate_one(claim, fact_ids, passage_by_id, citation_store)
            if code is None:
                accepted.append(claim)
            else:
                rejected.append(claim.claim_id)
                codes.append(code)
        return ClaimValidationResult(tuple(accepted), tuple(rejected), tuple(sorted(set(codes))))

    def _validate_one(self, claim: TrustedInsightClaim, fact_ids: set[str], passage_by_id: Mapping[str, str], citation_store: CitationStore) -> str | None:
        if not set(claim.internal_fact_ids).issubset(fact_ids):
            return "CLAIM_INTERNAL_FACT_UNKNOWN"
        try:
            citations = citation_store.validate_references(claim.external_citation_ids)
        except CitationStoreError:
            return "CLAIM_CITATION_UNKNOWN_OR_INVALID"
        if claim.claim_type == InsightClaimType.FACT and self._policy.require_external_fact_citation and not citations:
            return "CLAIM_EXTERNAL_FACT_CITATION_MISSING"
        if self._PROHIBITED_OUTCOME.search(claim.statement):
            return "CLAIM_PROHIBITED_UNSUPPORTED_OUTCOME"
        if self._REGULATORY.search(claim.statement):
            if len(citations) < self._policy.minimum_regulatory_citations or not claim.requires_human_review:
                return "CLAIM_REGULATORY_REVIEW_OR_CITATION_MISSING"
        if citations and not self._has_passage_support(claim.statement, citations, passage_by_id):
            return "CLAIM_CITATION_PASSAGE_UNSUPPORTED"
        if claim.claim_type in {InsightClaimType.INFERENCE, InsightClaimType.HYPOTHESIS} and not claim.correlation_not_causation:
            return "CLAIM_CAUSAL_LANGUAGE_NOT_ALLOWED"
        return None

    def _has_passage_support(self, statement: str, citations: Iterable[object], passage_by_id: Mapping[str, str]) -> bool:
        terms = {item.casefold() for item in self._WORD.findall(statement) if item.casefold() not in self._STOPWORDS}
        if not terms:
            return False
        supported_terms: set[str] = set()
        for citation in citations:
            for passage_id in citation.passage_ids:
                supported_terms.update(item.casefold() for item in self._WORD.findall(passage_by_id.get(passage_id, "")))
        return bool(terms & supported_terms)


__all__ = ["ClaimValidationPolicy", "ClaimValidationResult", "ClaimValidator"]
