"""Governed human-review interrupt and decision contracts for trusted insights.

This node never publishes a critical report itself.  A future LangGraph builder
must checkpoint the returned requirement and call ``interrupt_for_review`` at
the configured interrupt point; this file deliberately has no database or
global in-memory review queue.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from typing import Iterable

from app.business_impact.impact.models import GovernedRecommendation
from app.business_impact.models import BusinessImpactWarning, TrustedInsightClaim
from app.business_impact.scoring.models import AggregateQualityScore, AssessmentConfidence, ScoreBand


class ReviewDecisionType(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    REVISE = "revise"


class ReviewPublicationStatus(str, Enum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVISION_REQUIRED = "revision_required"


@dataclass(frozen=True, slots=True)
class ReviewRequirement:
    """Sanitized pause payload; the token is an opaque correlation identifier."""

    requires_review: bool
    review_token: str | None
    reason_codes: tuple[str, ...]
    publication_status: ReviewPublicationStatus


@dataclass(frozen=True, slots=True)
class ReviewDecision:
    """Bounded reviewer action to persist only through the graph checkpointer."""

    review_token: str
    decision: ReviewDecisionType
    reviewer_id: str
    comment: str | None
    decided_at: datetime

    def __post_init__(self) -> None:
        if not self.review_token.startswith("review-"):
            raise ValueError("invalid review token")
        if not self.reviewer_id or len(self.reviewer_id) > 128:
            raise ValueError("reviewer_id must be a bounded non-empty identifier")
        if self.comment is not None and len(self.comment) > 1_000:
            raise ValueError("review comment exceeds the configured bound")


class HumanReviewNode:
    """Derives review requirements and maps review decisions to publication state."""

    _REGULATORY = ("regulatory", "compliance", "enforcement", "supervisory")

    def __init__(self, *, low_confidence_threshold: float = 0.6) -> None:
        if not 0.0 < low_confidence_threshold <= 1.0:
            raise ValueError("low_confidence_threshold must be within (0, 1]")
        self._low_confidence_threshold = low_confidence_threshold

    def require_review(
        self,
        *,
        run_id: str,
        aggregate_score: AggregateQualityScore | None,
        assessment_confidence: AssessmentConfidence | None,
        claims: Iterable[TrustedInsightClaim],
        recommendations: Iterable[GovernedRecommendation],
        warnings: Iterable[BusinessImpactWarning],
    ) -> ReviewRequirement:
        """Determine whether graph execution must pause before publication."""

        reasons: set[str] = set()
        if aggregate_score is not None and aggregate_score.band == ScoreBand.CRITICAL:
            reasons.add("CRITICAL_DETERMINISTIC_SCORE_BAND")
        if assessment_confidence is None or assessment_confidence.value is None or assessment_confidence.value < self._low_confidence_threshold:
            reasons.add("ASSESSMENT_CONFIDENCE_BELOW_THRESHOLD")
        for claim in claims:
            lowered = claim.statement.casefold()
            if claim.requires_human_review:
                reasons.add("CLAIM_REQUIRES_HUMAN_REVIEW")
            if any(term in lowered for term in self._REGULATORY):
                reasons.add("EXTERNAL_REGULATORY_CLAIM")
        if any(item.generated_by_llm and item.catalogue_id is None for item in recommendations):
            reasons.add("LLM_RECOMMENDATION_NOT_IN_CATALOGUE")
        for warning in warnings:
            if warning.code in {"CLAIM_CITATION_UNKNOWN_OR_INVALID", "CLAIM_CITATION_PASSAGE_UNSUPPORTED", "CLAIM_DISCLOSURE_PATTERN_FORBIDDEN"}:
                reasons.add("POLICY_OR_CITATION_WARNING")
        if not reasons:
            return ReviewRequirement(False, None, (), ReviewPublicationStatus.NOT_REQUIRED)
        ordered = tuple(sorted(reasons))
        token = "review-" + sha256((run_id + "|" + "|".join(ordered)).encode("utf-8")).hexdigest()[:24]
        return ReviewRequirement(True, token, ordered, ReviewPublicationStatus.PENDING)

    def apply_decision(self, requirement: ReviewRequirement, decision: ReviewDecision) -> ReviewPublicationStatus:
        """Verify the decision token and derive the only permissible status."""

        if not requirement.requires_review or requirement.review_token != decision.review_token:
            raise ValueError("review decision does not match a pending review requirement")
        return {
            ReviewDecisionType.APPROVE: ReviewPublicationStatus.APPROVED,
            ReviewDecisionType.REJECT: ReviewPublicationStatus.REJECTED,
            ReviewDecisionType.REVISE: ReviewPublicationStatus.REVISION_REQUIRED,
        }[decision.decision]

    @staticmethod
    def interrupt_for_review(requirement: ReviewRequirement) -> object:
        """Issue a LangGraph interrupt when installed, otherwise return its payload.

        Returning the payload allows an API-only MVP to surface
        ``requires_review=true`` without silently publishing the result.
        """

        if not requirement.requires_review:
            return requirement
        payload = {"review_token": requirement.review_token, "reason_codes": list(requirement.reason_codes), "publication_status": requirement.publication_status.value}
        try:
            from langgraph.types import interrupt  # type: ignore[import-not-found]
        except ImportError:
            return payload
        return interrupt(payload)


__all__ = [
    "HumanReviewNode",
    "ReviewDecision",
    "ReviewDecisionType",
    "ReviewPublicationStatus",
    "ReviewRequirement",
]
