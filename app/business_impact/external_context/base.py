"""Provider-neutral, policy-bound interface for approved public connectors."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from urllib.parse import urlparse

from app.business_impact.external_context.models import (
    PublicResearchTopic,
    PublicSourceDocument,
    PublicSourcePassage,
    RetrievalProvenance,
)


class ExternalContextPolicyError(ValueError):
    """Safe policy boundary error; never includes credentials or source content."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class ConnectorLimits:
    """Configured operational ceilings supplied to an allowlisted connector."""

    connect_timeout_seconds: float
    read_timeout_seconds: float
    maximum_retries: int
    maximum_response_bytes: int
    maximum_documents: int
    maximum_passages_per_document: int
    maximum_passage_characters: int
    requests_per_minute: int


@dataclass(frozen=True, slots=True)
class ApprovedSource:
    """Immutable source registration; only these hosts and paths are reachable."""

    source_id: str
    connector_id: str
    publisher: str
    allowed_hosts: frozenset[str]
    allowed_schemes: frozenset[str]
    allowed_path_prefixes: tuple[str, ...]
    allowed_document_types: frozenset[str]
    trust_tier: str

    def validate_url(self, url: str) -> None:
        """Reject user/LLM URLs and redirects outside this exact source definition."""

        parsed = urlparse(url)
        host = (parsed.hostname or "").casefold()
        if parsed.username or parsed.password or parsed.fragment or parsed.query:
            raise ExternalContextPolicyError("PUBLIC_URL_REJECTED", "Public source URL includes disallowed components.")
        if parsed.scheme.casefold() not in self.allowed_schemes or host not in self.allowed_hosts:
            raise ExternalContextPolicyError("PUBLIC_URL_REJECTED", "Public source URL is not allowlisted.")
        if not any(parsed.path.startswith(prefix) for prefix in self.allowed_path_prefixes):
            raise ExternalContextPolicyError("PUBLIC_URL_REJECTED", "Public source URL path is not allowlisted.")


@dataclass(frozen=True, slots=True)
class ConnectorResponse:
    """Bounded connector result; its contents are verified in later graph nodes."""

    retrieval: RetrievalProvenance
    documents: tuple[PublicSourceDocument, ...]
    passages: tuple[PublicSourcePassage, ...]


class ExternalContextConnector(ABC):
    """A connector can receive only a pre-approved generic public research topic."""

    @property
    @abstractmethod
    def connector_id(self) -> str:
        """Configured connector identifier; must match one source registration."""

    @abstractmethod
    def retrieve(self, topic: PublicResearchTopic, *, source: ApprovedSource, limits: ConnectorLimits) -> ConnectorResponse:
        """Retrieve public information for a vetted generic topic.

        Deliberately absent: URL, UnderstandingOutput, ExecutionOutput, metrics,
        scores, fact packs, credentials and any caller-supplied request object.
        """


class ConnectorClock(Protocol):
    """Optional dependency for connector implementations that need current time."""

    def now(self) -> datetime: ...
