"""Contracts for allowlisted public-source retrieval and validated citations."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import Field, HttpUrl, field_validator, model_validator

from app.business_impact.models import BusinessImpactContract, _utc_now


class SourceTrustTier(str, Enum):
    PRIMARY_AUTHORITY = "primary_authority"
    PRIMARY_CORPORATE = "primary_corporate"
    GOVERNMENT = "government"
    APPROVED_SECONDARY = "approved_secondary"


class RetrievalStatus(str, Enum):
    RETRIEVED = "retrieved"
    NOT_FOUND = "not_found"
    TIMEOUT = "timeout"
    FAILED = "failed"
    REJECTED_BY_POLICY = "rejected_by_policy"


class PublicResearchTopic(BusinessImpactContract):
    """Sanitized topic allowed to leave the trusted internal boundary."""

    topic_id: str = Field(pattern=r"^topic-[a-z0-9][a-z0-9_-]{2,127}$")
    query_text: str = Field(min_length=3, max_length=300)
    approved_source_ids: list[str] = Field(min_length=1, max_length=20)
    purpose: str = Field(min_length=1, max_length=500)

    @field_validator("query_text")
    @classmethod
    def validate_generic_query(cls, value: str) -> str:
        lowered = value.casefold()
        forbidden = ("undq", "lumi", "transcript", "dataset", "column", "%", "percentage")
        if any(token in lowered for token in forbidden):
            raise ValueError("public query contains forbidden internal terminology")
        return value


class RetrievalProvenance(BusinessImpactContract):
    """Operational record of an external retrieval attempt without credentials."""

    retrieval_id: str = Field(pattern=r"^retrieval-[a-z0-9][a-z0-9_-]{2,127}$")
    connector_id: str = Field(min_length=1, max_length=128)
    topic_id: str = Field(min_length=1, max_length=128)
    status: RetrievalStatus
    requested_at: datetime = Field(default_factory=_utc_now)
    completed_at: datetime | None = None
    retry_count: int = Field(default=0, ge=0, le=10)
    response_size_bytes: int | None = Field(default=None, ge=0)
    error_code: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_retrieval(self) -> "RetrievalProvenance":
        if self.completed_at is not None and self.completed_at < self.requested_at:
            raise ValueError("completed_at cannot precede requested_at")
        if self.status == RetrievalStatus.FAILED and not self.error_code:
            raise ValueError("failed retrieval requires error_code")
        return self


class PublicSourceDocument(BusinessImpactContract):
    """A bounded document returned by an approved connector."""

    document_id: str = Field(pattern=r"^doc-[a-z0-9][a-z0-9_-]{2,127}$")
    connector_id: str = Field(min_length=1, max_length=128)
    publisher: str = Field(min_length=1, max_length=240)
    title: str = Field(min_length=1, max_length=500)
    canonical_url: HttpUrl
    publication_time: datetime | None = None
    retrieved_at: datetime = Field(default_factory=_utc_now)
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    trust_tier: SourceTrustTier
    document_type: str = Field(min_length=1, max_length=128)
    retrieval_provenance_id: str = Field(min_length=1, max_length=128)
    excerpt_available: bool = True


class PublicSourcePassage(BusinessImpactContract):
    """Bounded verbatim passage, linked to a verified source document."""

    passage_id: str = Field(pattern=r"^passage-[a-z0-9][a-z0-9_-]{2,127}$")
    document_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=4_000)
    locator: str | None = Field(default=None, max_length=240)
    relevance_score: float | None = Field(default=None, ge=0.0, le=1.0)


class Citation(BusinessImpactContract):
    """Validated citation ID the LLM may reference, but never fabricate."""

    citation_id: str = Field(pattern=r"^cite-[a-z0-9][a-z0-9_-]{2,127}$")
    document_id: str = Field(min_length=1, max_length=128)
    passage_ids: list[str] = Field(min_length=1, max_length=20)
    publisher: str = Field(min_length=1, max_length=240)
    title: str = Field(min_length=1, max_length=500)
    canonical_url: HttpUrl
    trust_tier: SourceTrustTier
    publication_time: datetime | None = None

    @field_validator("passage_ids")
    @classmethod
    def unique_passages(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("citation passage_ids must be unique")
        return values


class ExternalContextBundle(BusinessImpactContract):
    """Deduplicated, bounded retrieval output made available to the LLM node."""

    bundle_id: str = Field(pattern=r"^external-context-[a-z0-9][a-z0-9_-]{2,127}$")
    retrievals: list[RetrievalProvenance] = Field(default_factory=list, max_length=100)
    documents: list[PublicSourceDocument] = Field(default_factory=list, max_length=100)
    passages: list[PublicSourcePassage] = Field(default_factory=list, max_length=200)
    citations: list[Citation] = Field(default_factory=list, max_length=100)
    truncated: bool = False
    warnings: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_bundle(self) -> "ExternalContextBundle":
        document_ids = {item.document_id for item in self.documents}
        passage_ids = {item.passage_id for item in self.passages}
        if any(item.document_id not in document_ids for item in self.passages):
            raise ValueError("passages must reference returned documents")
        if any(item.document_id not in document_ids for item in self.citations):
            raise ValueError("citations must reference returned documents")
        if any(any(pid not in passage_ids for pid in item.passage_ids) for item in self.citations):
            raise ValueError("citations must reference returned passages")
        urls = [str(item.canonical_url) for item in self.documents]
        if len(urls) != len(set(urls)):
            raise ValueError("external-context documents must be deduplicated by canonical URL")
        return self
