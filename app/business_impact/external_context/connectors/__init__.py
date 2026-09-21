"""Bounded, allowlisted public-source connector implementations.

All concrete connectors inherit :class:`HttpPublicConnector`.  They use a
request-scoped transport, require a previously approved generic topic, and
validate both the requested and final redirect URL against the source registry.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from html.parser import HTMLParser
import re
from time import monotonic, sleep
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.business_impact.external_context.base import (
    ApprovedSource, ConnectorLimits, ConnectorResponse, ExternalContextConnector,
    ExternalContextPolicyError,
)
from app.business_impact.external_context.models import (
    PublicResearchTopic, PublicSourceDocument, PublicSourcePassage,
    RetrievalProvenance, RetrievalStatus, SourceTrustTier,
)
from app.business_impact.external_context.query_guard import PublicQueryGuard


@dataclass(frozen=True, slots=True)
class HttpResponse:
    url: str
    status_code: int
    headers: dict[str, str]
    body: bytes


class HttpTransport(Protocol):
    def get(self, url: str, *, connect_timeout: float, read_timeout: float, maximum_bytes: int) -> HttpResponse: ...


class UrllibTransport:
    """Small standard-library transport with no authentication or cookies."""

    def get(self, url: str, *, connect_timeout: float, read_timeout: float, maximum_bytes: int) -> HttpResponse:
        del read_timeout  # urllib exposes one combined timeout; policy uses the conservative minimum.
        request = Request(url, headers={"User-Agent": "UnDQ-Public-Context/1.0", "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8"})
        with urlopen(request, timeout=connect_timeout) as response:  # nosec B310: source policy validates both URLs.
            body = response.read(maximum_bytes + 1)
            if len(body) > maximum_bytes:
                raise ExternalContextPolicyError("PUBLIC_RESPONSE_TOO_LARGE", "Public source response exceeds configured size limit.")
            return HttpResponse(response.geturl(), int(response.status), dict(response.headers.items()), body)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(); self._title = ""; self._in_title = False; self._chunks: list[str] = []
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._in_title = tag.casefold() == "title"
    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "title": self._in_title = False
    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if text:
            self._chunks.append(text)
            if self._in_title: self._title = (self._title + " " + text).strip()
    @property
    def title(self) -> str: return self._title
    @property
    def text(self) -> str: return " ".join(self._chunks)


class HttpPublicConnector(ExternalContextConnector, ABC):
    """Safe base for a configured source landing page or document feed."""

    def __init__(self, query_guard: PublicQueryGuard, transport: HttpTransport | None = None) -> None:
        self._query_guard = query_guard; self._transport = transport or UrllibTransport(); self._last_request_at = 0.0

    @abstractmethod
    def endpoint(self, source: ApprovedSource) -> str:
        """Return a connector-owned endpoint, never caller-provided URL text."""

    def retrieve(self, topic: PublicResearchTopic, *, source: ApprovedSource, limits: ConnectorLimits) -> ConnectorResponse:
        self._query_guard.validate_topic(topic)
        if source.connector_id != self.connector_id:
            raise ExternalContextPolicyError("CONNECTOR_SOURCE_MISMATCH", "Connector does not match its approved source registration.")
        endpoint = self.endpoint(source); source.validate_url(endpoint)
        retrieval_id = "retrieval-" + _digest(f"{self.connector_id}|{topic.topic_id}|{endpoint}")
        response, error_code = self._fetch(endpoint, source, limits)
        now = datetime.now(timezone.utc)
        if response is None:
            status = RetrievalStatus.TIMEOUT if error_code == "PUBLIC_SOURCE_TIMEOUT" else RetrievalStatus.FAILED
            return ConnectorResponse(RetrievalProvenance(retrieval_id=retrieval_id, connector_id=self.connector_id, topic_id=topic.topic_id, status=status, requested_at=now, completed_at=now, retry_count=limits.maximum_retries, error_code=error_code), (), ())
        text, title = _extract_text(response.body)
        document_id = "doc-" + _digest(response.url + "|" + sha256(response.body).hexdigest())
        document = PublicSourceDocument(document_id=document_id, connector_id=self.connector_id, publisher=source.publisher, title=title or source.publisher, canonical_url=response.url, publication_time=None, retrieved_at=now, content_sha256=sha256(response.body).hexdigest(), trust_tier=SourceTrustTier(source.trust_tier), document_type=self.document_type(response.url, source), retrieval_provenance_id=retrieval_id, excerpt_available=bool(text))
        passages = _passages(text, document_id, limits.maximum_passages_per_document, limits.maximum_passage_characters)
        retrieval = RetrievalProvenance(retrieval_id=retrieval_id, connector_id=self.connector_id, topic_id=topic.topic_id, status=RetrievalStatus.RETRIEVED, requested_at=now, completed_at=now, retry_count=0, response_size_bytes=len(response.body))
        return ConnectorResponse(retrieval, (document,), passages)

    def document_type(self, url: str, source: ApprovedSource) -> str:
        """Use source-configured type only; a later verifier may refine it."""
        del url
        return sorted(source.allowed_document_types)[0]

    def _fetch(self, endpoint: str, source: ApprovedSource, limits: ConnectorLimits) -> tuple[HttpResponse | None, str | None]:
        interval = 60.0 / limits.requests_per_minute
        delay = max(0.0, interval - (monotonic() - self._last_request_at))
        if delay: sleep(delay)
        for attempt in range(limits.maximum_retries + 1):
            try:
                response = self._transport.get(endpoint, connect_timeout=min(limits.connect_timeout_seconds, limits.read_timeout_seconds), read_timeout=limits.read_timeout_seconds, maximum_bytes=limits.maximum_response_bytes)
                source.validate_url(response.url)
                self._last_request_at = monotonic()
                return response, None
            except ExternalContextPolicyError as exc:
                return None, exc.code
            except (HTTPError, URLError, TimeoutError, OSError):
                if attempt == limits.maximum_retries: return None, "PUBLIC_SOURCE_TIMEOUT"
                sleep(min(2.0, 0.5 * (2**attempt)))
        return None, "PUBLIC_SOURCE_FAILED"


def _extract_text(body: bytes) -> tuple[str, str]:
    decoded = body.decode("utf-8", errors="replace")
    parser = _TextExtractor(); parser.feed(decoded)
    return " ".join(parser.text.split()), " ".join(parser.title.split())[:500]


def _passages(text: str, document_id: str, maximum_passages: int, maximum_characters: int) -> tuple[PublicSourcePassage, ...]:
    sentences = [item.strip() for item in re.split(r"(?<=[.!?])\s+", text) if len(item.strip()) >= 40]
    output: list[PublicSourcePassage] = []
    for index, sentence in enumerate(sentences[:maximum_passages]):
        clipped = sentence[:maximum_characters]
        output.append(PublicSourcePassage(passage_id=f"passage-{_digest(document_id + '|' + str(index) + '|' + clipped)}", document_id=document_id, text=clipped, locator=f"extracted_text:{index + 1}", relevance_score=None))
    return tuple(output)


def _digest(value: str) -> str: return sha256(value.encode("utf-8")).hexdigest()[:24]


from app.business_impact.external_context.connectors.sec import SecConnector  # noqa: E402
from app.business_impact.external_context.connectors.amex_investor_relations import AmexInvestorRelationsConnector  # noqa: E402
from app.business_impact.external_context.connectors.cfpb import CfpbConnector  # noqa: E402
from app.business_impact.external_context.connectors.federal_reserve import FederalReserveConnector  # noqa: E402
from app.business_impact.external_context.connectors.occ import OccConnector  # noqa: E402
from app.business_impact.external_context.connectors.ftc import FtcConnector  # noqa: E402
from app.business_impact.external_context.connectors.approved_web_search import ApprovedWebSearchConnector  # noqa: E402

__all__ = ["ApprovedWebSearchConnector", "AmexInvestorRelationsConnector", "CfpbConnector", "FederalReserveConnector", "FtcConnector", "HttpPublicConnector", "HttpResponse", "HttpTransport", "OccConnector", "SecConnector", "UrllibTransport"]
