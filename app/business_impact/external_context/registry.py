"""Immutable registration of configured allowlisted public-source connectors."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from app.business_impact.external_context.base import ApprovedSource, ConnectorLimits, ExternalContextConnector, ExternalContextPolicyError


@dataclass(frozen=True, slots=True)
class ConnectorRegistryConfig:
    """Validated registry policy, independent of runtime connector instances."""

    registry_version: str
    limits: ConnectorLimits
    sources_by_connector: Mapping[str, ApprovedSource]

    @classmethod
    def from_file(cls, path: str | Path) -> "ConnectorRegistryConfig":
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ExternalContextPolicyError("SOURCE_REGISTRY_LOAD_FAILED", "Configured source registry could not be loaded.") from exc
        if not isinstance(document, Mapping):
            raise ExternalContextPolicyError("SOURCE_REGISTRY_INVALID", "Configured source registry must be an object.")
        try:
            limits_raw = document["global_limits"]
            limits = ConnectorLimits(float(limits_raw["connect_timeout_seconds"]), float(limits_raw["read_timeout_seconds"]), int(limits_raw["maximum_retries"]), int(limits_raw["maximum_response_bytes"]), int(limits_raw["maximum_documents_per_connector"]), int(limits_raw["maximum_passages_per_document"]), int(limits_raw["maximum_passage_characters"]), int(limits_raw["requests_per_minute_per_connector"]))
            sources: dict[str, ApprovedSource] = {}
            for item in document["sources"]:
                source = ApprovedSource(str(item["source_id"]), str(item["connector_id"]), str(item["publisher"]), frozenset(str(x).casefold() for x in item["allowed_hosts"]), frozenset(str(x).casefold() for x in item["allowed_schemes"]), tuple(str(x) for x in item["allowed_path_prefixes"]), frozenset(str(x) for x in item["allowed_document_types"]), str(item["trust_tier"]))
                if source.connector_id in sources: raise ValueError("duplicate connector id")
                sources[source.connector_id] = source
        except (KeyError, TypeError, ValueError) as exc:
            raise ExternalContextPolicyError("SOURCE_REGISTRY_INVALID", "Configured source registry has invalid source definitions.") from exc
        if not sources or min(limits.connect_timeout_seconds, limits.read_timeout_seconds, limits.maximum_response_bytes, limits.maximum_documents, limits.maximum_passages_per_document, limits.maximum_passage_characters, limits.requests_per_minute) <= 0 or limits.maximum_retries < 0:
            raise ExternalContextPolicyError("SOURCE_REGISTRY_INVALID", "Configured connector limits are invalid.")
        return cls(str(document.get("registry_version", "unknown")), limits, MappingProxyType(sources))


class ConnectorRegistry:
    """Request-scoped registry; an unconfigured or duplicate connector is rejected."""

    def __init__(self, config: ConnectorRegistryConfig, connectors: tuple[ExternalContextConnector, ...] = ()) -> None:
        self._config = config
        indexed: dict[str, ExternalContextConnector] = {}
        for connector in connectors:
            self.register(connector, indexed)
        self._connectors = MappingProxyType(indexed)

    @property
    def registered_connector_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._connectors))

    def source_for(self, connector_id: str) -> ApprovedSource:
        try: return self._config.sources_by_connector[connector_id]
        except KeyError as exc: raise ExternalContextPolicyError("CONNECTOR_NOT_CONFIGURED", "Connector is not configured in the approved source registry.") from exc

    def connector_for(self, connector_id: str) -> ExternalContextConnector:
        self.source_for(connector_id)
        try: return self._connectors[connector_id]
        except KeyError as exc: raise ExternalContextPolicyError("CONNECTOR_NOT_REGISTERED", "Configured connector implementation is unavailable.") from exc

    @property
    def limits(self) -> ConnectorLimits: return self._config.limits

    def register(self, connector: ExternalContextConnector, target: dict[str, ExternalContextConnector] | None = None) -> None:
        """Validate one implementation against policy; no runtime replacement occurs."""
        destination = self._connectors if target is None else target
        connector_id = connector.connector_id
        if connector_id not in self._config.sources_by_connector: raise ExternalContextPolicyError("CONNECTOR_NOT_CONFIGURED", "Connector implementation is not allowlisted.")
        if connector_id in destination: raise ExternalContextPolicyError("CONNECTOR_DUPLICATE", "Duplicate connector registration is not allowed.")
        if isinstance(destination, MappingProxyType): raise ExternalContextPolicyError("REGISTRY_IMMUTABLE", "Connector registry cannot change after construction.")
        destination[connector_id] = connector
