"""Perform the single mandatory trusted-insight LLM invocation for a run."""

from __future__ import annotations

from dataclasses import dataclass

from app.business_impact.evidence.models import EvidenceFactPack
from app.business_impact.external_context.models import ExternalContextBundle
from app.business_impact.insights.prompt_builder import TrustedInsightPromptBuilder
from app.business_impact.insights.response_parser import TrustedInsightResponseError, TrustedInsightResponseParser
from app.business_impact.models import BusinessImpactError, LLMSelection, TrustedInsightClaim
from app.llm_provider import LLMProviderError, generate_text


@dataclass(frozen=True, slots=True)
class InsightGenerationResult:
    """Successful claims or a visible, safe partial-result error."""

    claims: tuple[TrustedInsightClaim, ...]
    error: BusinessImpactError | None = None


class TrustedInsightGenerator:
    """Single-call boundary: no per-rule, per-passage or retry LLM invocation."""

    def __init__(self, prompt_builder: TrustedInsightPromptBuilder, response_parser: TrustedInsightResponseParser) -> None:
        self._prompt_builder = prompt_builder
        self._response_parser = response_parser

    def generate(self, *, llm: LLMSelection, fact_pack: EvidenceFactPack, context: ExternalContextBundle) -> InsightGenerationResult:
        """Generate once, then deterministically validate model JSON and references."""

        try:
            prompt = self._prompt_builder.build(fact_pack, context)
            raw_response = generate_text(prompt.text, request_id=llm.request_id, provider_name=llm.provider, model=llm.model)
            response = self._response_parser.parse(raw_response, prompt)
            if not response.claims:
                return InsightGenerationResult((), BusinessImpactError(code="LLM_INSUFFICIENT_SUPPORTED_CLAIMS", message="The LLM returned no supported trusted-insight claims.", retryable=False, component="generate_insight"))
            return InsightGenerationResult(response.claims)
        except (LLMProviderError, TrustedInsightResponseError, ValueError) as exc:
            code = getattr(exc, "code", "LLM_GENERATION_FAILED")
            return InsightGenerationResult((), BusinessImpactError(code=code, message="Trusted insight generation failed; deterministic evidence and scores remain available.", retryable=isinstance(exc, LLMProviderError), component="generate_insight"))


__all__ = ["InsightGenerationResult", "TrustedInsightGenerator"]
