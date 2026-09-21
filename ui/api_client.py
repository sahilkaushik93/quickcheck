"""HTTP client used by Streamlit; no backend service imports are permitted."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import httpx


class APIClientError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class APIClient:
    base_url: str = os.getenv("UNDQ_API_BASE_URL", "http://127.0.0.1:8000")
    timeout_seconds: float = float(os.getenv("UNDQ_UI_HTTP_TIMEOUT_SECONDS", "300"))

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            with httpx.Client(base_url=self.base_url, timeout=self.timeout_seconds) as client:
                response = client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise APIClientError(f"Backend connection failed: {exc}") from exc
        if response.is_error:
            try:
                detail = response.json().get("detail", response.json())
            except ValueError:
                detail = response.text[:500]
            raise APIClientError(f"API {response.status_code}: {detail}")
        return response.json()

    def ready(self) -> dict[str, Any]:
        return self._request("GET", "/ready")

    def understanding(self, *, csv_name: str, csv_bytes: bytes, metadata_name: str, metadata_bytes: bytes, rules_name: str, rules_bytes: bytes, llm: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST", "/api/v1/undq/transcript/understanding",
            files={
                "sample_csv": (csv_name, csv_bytes, "text/csv"),
                "metadata_dictionary": (metadata_name, metadata_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                "rules_archive": (rules_name, rules_bytes, "application/zip"),
            },
            data={"persist_artifacts": "false", "llm_enabled": str(bool(llm.get("enabled"))).lower(), "llm_provider": llm.get("provider", "ollama"), "llm_model": llm.get("model") or "", "request_id": llm.get("request_id", "ui")},
        )

    def execution(self, *, csv_name: str, csv_bytes: bytes, understanding: dict[str, Any], aggregation: dict[str, Any], llm: dict[str, Any], selected_rule_ids: list[str] | None = None) -> dict[str, Any]:
        return self._request(
            "POST", "/api/v1/undq/transcript/execution",
            files={"sample_csv": (csv_name, csv_bytes, "text/csv")},
            data={
                "understanding_json": json.dumps(understanding),
                "aggregation_json": json.dumps(aggregation),
                "column_overrides_json": "{}",
                "selected_rule_ids_json": json.dumps(selected_rule_ids) if selected_rule_ids else "",
                "llm_enabled": str(bool(llm.get("enabled"))).lower(),
                "llm_provider": llm.get("provider", "ollama"),
                "llm_model": llm.get("model") or "",
                "request_id": llm.get("request_id", "ui"),
            },
        )

    def list_artifacts(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/undq/transcript/artifacts/{run_id}")

    def business_impact(
        self, *, understanding: dict[str, Any], execution: dict[str, Any],
        business_context: dict[str, Any] | None, options: dict[str, Any], llm: dict[str, Any],
    ) -> dict[str, Any]:
        """Request only the API's governed downstream assessment endpoint."""

        return self._request(
            "POST", "/api/v1/undq/transcript/business-impact",
            json={
                "understanding": understanding,
                "execution": execution,
                "business_context": business_context,
                "options": options,
                "llm": {"provider": llm["provider"], "model": llm.get("model"), "request_id": llm["request_id"], "failure_mode": "partial"},
            },
        )

    def list_business_impact_artifacts(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/undq/transcript/artifacts/business-impact/{run_id}")

    def download_business_impact_artifact(self, run_id: str, filename: str) -> bytes:
        """Download only a server-manifested artifact; never construct local paths."""

        try:
            with httpx.Client(base_url=self.base_url, timeout=self.timeout_seconds) as client:
                response = client.get(f"/api/v1/undq/transcript/artifacts/business-impact/{run_id}/{filename}")
        except httpx.HTTPError as exc:
            raise APIClientError(f"Backend connection failed: {exc}") from exc
        if response.is_error:
            raise APIClientError(f"API {response.status_code}: Business Impact artifact is unavailable.")
        return response.content


__all__ = ["APIClient", "APIClientError"]
