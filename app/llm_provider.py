"""Pluggable text-generation providers used by the DQ layers.

Layer logic should import ``generate_text`` from this module instead of calling
a vendor API directly. Provider credentials and default endpoints come from
environment variables; callers may select a provider per request.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import httpx
from dotenv import load_dotenv


load_dotenv()


class LLMProviderError(RuntimeError):
    """Raised when a configured provider cannot generate a response."""


@dataclass(frozen=True)
class ProviderConfig:
    timeout_seconds: float = 60.0
    launchpad_url: str = "http://127.0.0.1:8080/generate/single"
    ollama_url: str = "http://127.0.0.1:11434/api/generate"
    ollama_model: str = "llama3.1:8b"
    doc_intelligence_url: str | None = None
    doc_intelligence_username: str | None = None
    doc_intelligence_password: str | None = None

    @classmethod
    def from_env(cls) -> "ProviderConfig":
        return cls(
            timeout_seconds=float(os.getenv("LLM_TIMEOUT_SECONDS", "60")),
            launchpad_url=os.getenv(
                "LAUNCHPAD_API_URL",
                "http://127.0.0.1:8080/generate/single",
            ),
            ollama_url=os.getenv(
                "OLLAMA_API_URL",
                "http://127.0.0.1:11434/api/generate",
            ),
            ollama_model=os.getenv("OLLAMA_MODEL", "llama3.1:8b"),
            doc_intelligence_url=os.getenv("DOC_INTELLIGENCE_API_URL"),
            doc_intelligence_username=os.getenv("DOC_INTELLIGENCE_USERNAME"),
            doc_intelligence_password=os.getenv("DOC_INTELLIGENCE_PASSWORD"),
        )


def _extract_text(data: Any) -> str:
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        for key in ("text", "response", "generated_text", "output", "result"):
            value = data.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, dict):
                nested = _extract_text(value)
                if nested:
                    return nested
    raise LLMProviderError("Provider response did not contain generated text.")


class BaseLLMProvider(ABC):
    name: str

    def __init__(self, config: ProviderConfig, model: str | None = None):
        self.config = config
        self.model = model

    @abstractmethod
    def generate_text(self, prompt: str, request_id: str = "1") -> str:
        raise NotImplementedError

    def _post(self, url: str, **kwargs: Any) -> dict[str, Any] | str:
        try:
            with httpx.Client(timeout=self.config.timeout_seconds) as client:
                response = client.post(url, **kwargs)
                response.raise_for_status()
                return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LLMProviderError(f"{self.name} request failed: {exc}") from exc


class LaunchpadProvider(BaseLLMProvider):
    name = "launchpad"

    def generate_text(self, prompt: str, request_id: str = "1") -> str:
        data = self._post(
            self.config.launchpad_url,
            json={"prompt": prompt, "request_id": request_id},
        )
        return _extract_text(data)


class OllamaProvider(BaseLLMProvider):
    name = "ollama"

    def generate_text(self, prompt: str, request_id: str = "1") -> str:
        data = self._post(
            self.config.ollama_url,
            json={
                "model": self.model or self.config.ollama_model,
                "prompt": prompt,
                "stream": False,
                "options": {"seed": _safe_seed(request_id)},
            },
        )
        return _extract_text(data)


class DocumentIntelligenceProvider(BaseLLMProvider):
    """Generic credential-based adapter; adjust payload to the actual API contract."""

    name = "doc_intelligence"

    def generate_text(self, prompt: str, request_id: str = "1") -> str:
        if not self.config.doc_intelligence_url:
            raise LLMProviderError("DOC_INTELLIGENCE_API_URL is not configured.")
        if not self.config.doc_intelligence_username or not self.config.doc_intelligence_password:
            raise LLMProviderError("Document-intelligence credentials are not configured.")
        data = self._post(
            self.config.doc_intelligence_url,
            auth=(
                self.config.doc_intelligence_username,
                self.config.doc_intelligence_password,
            ),
            json={
                "prompt": prompt,
                "request_id": request_id,
                **({"model": self.model} if self.model else {}),
            },
        )
        return _extract_text(data)


def _safe_seed(request_id: str) -> int:
    try:
        return int(request_id)
    except ValueError:
        return abs(hash(request_id)) % (2**31)


PROVIDERS: dict[str, type[BaseLLMProvider]] = {
    "launchpad": LaunchpadProvider,
    "ollama": OllamaProvider,
    "doc_intelligence": DocumentIntelligenceProvider,
}


def get_provider(
    provider_name: str | None = None,
    *,
    model: str | None = None,
    config: ProviderConfig | None = None,
) -> BaseLLMProvider:
    name = (provider_name or os.getenv("LLM_PROVIDER", "launchpad")).strip().lower()
    provider_class = PROVIDERS.get(name)
    if provider_class is None:
        supported = ", ".join(sorted(PROVIDERS))
        raise LLMProviderError(f"Unsupported LLM provider '{name}'. Supported: {supported}.")
    return provider_class(config or ProviderConfig.from_env(), model=model)


def generate_text(
    prompt: str,
    request_id: str = "1",
    *,
    provider_name: str | None = None,
    model: str | None = None,
) -> str:
    """Generate text with the selected provider.

    Example: ``generate_text(prompt, request_id="1", provider_name="ollama")``.
    When ``provider_name`` is omitted, ``LLM_PROVIDER`` is used.
    """
    return get_provider(provider_name, model=model).generate_text(prompt, request_id)
