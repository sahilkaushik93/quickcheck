"""Graph node that permits only citation-store-backed, supported claims."""

from __future__ import annotations

from dataclasses import dataclass

from app.business_impact.evidence.models import EvidenceFactPack
from app.business_impact.external_context.citation_store import CitationStore
from app.business_impact.external_context.models import ExternalContextBundle
from app.business_impact.insights.claim_validator import ClaimValidator
from app.business_impact.models import BusinessImpactWarning, TrustedInsightClaim


@dataclass(frozen=True, slots=True)
class CitationValidationNodeResult:
    """Validated claims plus an explicit review trigger for rejected content."""

    claims: tuple[TrustedInsightClaim, ...]
    warnings: tuple[BusinessImpactWarning, ...]
    requires_human_review: bool


class CitationValidationNode:
    """Runs deterministic citation and passage-support checks after the LLM node."""

    def __init__(self, validator: ClaimValidator) -> None:
        self._validator = validator

    def validate(self, claims: tuple[TrustedInsightClaim, ...], *, fact_pack: EvidenceFactPack, context: ExternalContextBundle, citation_store: CitationStore) -> CitationValidationNodeResult:
        result = self._validator.validate(claims, fact_pack=fact_pack, context=context, citation_store=citation_store)
        warnings = tuple(BusinessImpactWarning(code=code, message="A trusted-insight claim was rejected by citation or support validation.", component="validate_citations") for code in result.rejection_codes)
        return CitationValidationNodeResult(result.accepted, warnings, bool(result.rejected_claim_ids))


__all__ = ["CitationValidationNode", "CitationValidationNodeResult"]
