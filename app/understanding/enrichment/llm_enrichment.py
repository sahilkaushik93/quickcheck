"""Optional single-call LLM enrichment for deterministic Understanding results."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import Lock
from typing import Any, Literal

from app.understanding.context.context_builder import ContextBundle
from app.understanding.models import DatasetProfile, DQSignal, ExecutionPlan, LLMInsight


_SERIAL_LLM_LOCK = Lock()


class LLMEnrichmentError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class LLMEnrichmentOptions:
    enabled: bool = False
    provider: str = "launchpad"
    model: str | None = None
    request_id: str = "1"
    max_prompt_characters: int = 12000
    max_insights: int = 5
    failure_mode: Literal["skip", "raise"] = "skip"

    def __post_init__(self) -> None:
        if self.max_prompt_characters < 1000:
            raise ValueError("max_prompt_characters must be at least 1000")
        if self.max_insights < 0:
            raise ValueError("max_insights cannot be negative")


@dataclass(frozen=True, slots=True)
class LLMEnrichmentResult:
    insights: tuple[LLMInsight, ...]
    warnings: tuple[str, ...]
    calls_made: int


class LLMEnricher:
    """Perform at most one serialized provider request for an entire run."""

    _ALLOWED_TYPES = {
        "business_interpretation",
        "signal_explanation",
        "domain_suggestion",
        "rule_suggestion",
        "recommended_action",
    }

    def __init__(
        self,
        options: LLMEnrichmentOptions,
        *,
        generator: Callable[..., Any] | None = None,
        request_lock: Lock | None = None,
    ) -> None:
        self._options = options
        self._generator = generator
        # Local/weak providers are intentionally serialized across API requests.
        self._request_lock = request_lock or _SERIAL_LLM_LOCK

    def enrich(
        self,
        profile: DatasetProfile,
        context: ContextBundle,
        signals: list[DQSignal],
        execution_plan: ExecutionPlan,
    ) -> LLMEnrichmentResult:
        if not self._options.enabled or self._options.max_insights == 0:
            return LLMEnrichmentResult(insights=(), warnings=(), calls_made=0)

        prompt = self._build_prompt(profile, context, signals, execution_plan)
        try:
            generator = self._generator
            if generator is None:
                # Keep deterministic processing importable when optional LLM
                # transport dependencies are not installed or configured.
                from app.llm_provider import generate_text

                generator = generate_text
            # One lock-protected call only. The coordinator should reuse one
            # enricher instance across API requests to serialize weak/local LLMs.
            with self._request_lock:
                response = generator(
                    prompt,
                    request_id=self._options.request_id,
                    provider_name=self._options.provider,
                    model=self._options.model,
                )
            insights = self._parse_response(response, profile, signals)
            return LLMEnrichmentResult(insights=tuple(insights), warnings=(), calls_made=1)
        except Exception as exc:
            if self._options.failure_mode == "raise":
                raise LLMEnrichmentError(
                    "LLM_ENRICHMENT_FAILED",
                    f"Optional LLM enrichment failed ({type(exc).__name__}).",
                ) from exc
            return LLMEnrichmentResult(
                insights=(),
                warnings=(self._safe_failure_warning(exc),),
                calls_made=1,
            )

    @staticmethod
    def _safe_failure_warning(exc: Exception) -> str:
        """Return an actionable diagnostic without prompts, rows or secrets."""

        detail = str(exc).strip().replace("\n", " ")[:500]
        suffix = f": {detail}" if detail else ""
        return f"Optional LLM enrichment skipped ({type(exc).__name__}){suffix}"

    def _build_prompt(
        self,
        profile: DatasetProfile,
        context: ContextBundle,
        signals: list[DQSignal],
        execution_plan: ExecutionPlan,
    ) -> str:
        """Build a compact prompt containing no samples, top values or source rows."""

        domain_by_column = {item.column_name: item for item in context.domains}
        signal_columns = {
            name for signal in signals for name in signal.affected_columns
        }
        ordered_columns = sorted(
            profile.columns,
            key=lambda item: (
                item.column_name not in signal_columns,
                item.fill_rate,
                item.column_name.casefold(),
            ),
        )
        columns = [
            {
                "name": column.column_name,
                "type": column.inferred_data_type,
                "fill_rate": round(column.fill_rate, 6),
                "domain": domain_by_column[column.column_name].domain
                if column.column_name in domain_by_column
                else "unknown",
                "domain_confidence": round(domain_by_column[column.column_name].confidence, 4)
                if column.column_name in domain_by_column
                else 0.0,
                "patterns": column.detected_patterns[:5],
            }
            for column in ordered_columns[:40]
        ]
        signal_payload = [
            {
                "signal_id": signal.signal_id,
                "type": signal.signal_type,
                "severity": signal.severity,
                "dimension": signal.dimension,
                "columns": signal.affected_columns,
                "observed_value": signal.observed_value,
                "threshold": signal.threshold,
            }
            for signal in sorted(
                signals,
                key=lambda item: (
                    {"critical": 0, "high": 1, "warning": 2, "medium": 3, "info": 4}.get(item.severity, 5),
                    item.signal_id,
                ),
            )[:40]
        ]
        plan_payload = [
            {
                "check": step.check_name,
                "target_count": len(step.target_columns),
                "priority": step.priority,
            }
            for step in execution_plan.steps
        ]
        payload = {
            "dataset": {
                "row_count": profile.row_count,
                "column_count": profile.column_count,
                "chunks_processed": profile.chunks_processed,
            },
            "columns": columns,
            "signals": signal_payload,
            "execution_plan": plan_payload,
            "selected_rules": execution_plan.selected_rules,
            "implementation_pending_rules": execution_plan.implementation_pending_rules,
        }
        instructions = (
            "You are a data-quality analyst. Interpret only the aggregate JSON below. "
            "Do not invent measurements, thresholds, rules, columns, PII, customer facts, or approvals. "
            "Return strict JSON only with shape {\"insights\":[{\"insight_type\": one of "
            "[\"business_interpretation\",\"signal_explanation\",\"domain_suggestion\","
            "\"rule_suggestion\",\"recommended_action\"],\"title\":string,\"narrative\":string,"
            "\"confidence\":number from 0 to 1,\"supporting_signal_ids\":[string],"
            "\"supporting_columns\":[string]}]}. All output is advisory and requires review.\n"
        )
        serialized = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
        available = self._options.max_prompt_characters - len(instructions)
        if available <= 0:
            raise LLMEnrichmentError("PROMPT_LIMIT_INVALID", "LLM prompt limit is too small.")
        if len(serialized) > available:
            # Preserve valid JSON and deterministic ordering while reducing detail.
            payload["columns"] = columns[: max(1, min(len(columns), 25))]
            payload["signals"] = signal_payload[: max(1, min(len(signal_payload), 25))]
            serialized = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
        if len(serialized) > available:
            raise LLMEnrichmentError(
                "PROMPT_LIMIT_EXCEEDED",
                "Aggregate enrichment payload exceeds the configured safe prompt limit.",
            )
        return instructions + serialized

    def _parse_response(
        self,
        response: Any,
        profile: DatasetProfile,
        signals: list[DQSignal],
    ) -> list[LLMInsight]:
        cleaned = self._extract_response_text(response).strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", cleaned, flags=re.I | re.S)
        if fenced:
            cleaned = fenced.group(1)
        try:
            document = json.loads(cleaned)
        except json.JSONDecodeError:
            # Weak/local models sometimes wrap JSON with a short explanation.
            # Extract only the outermost object; never attempt an LLM repair call.
            start, end = cleaned.find("{"), cleaned.rfind("}")
            if start < 0 or end <= start:
                raise LLMEnrichmentError(
                    "INVALID_LLM_JSON", "LLM response did not contain a JSON object."
                )
            document = json.loads(cleaned[start : end + 1])
        if not isinstance(document, dict) or not isinstance(document.get("insights"), list):
            raise LLMEnrichmentError("INVALID_LLM_RESPONSE", "LLM response must contain an insights array.")
        valid_columns = {column.column_name for column in profile.columns}
        valid_signals = {signal.signal_id for signal in signals}
        insights: list[LLMInsight] = []
        for index, raw in enumerate(document["insights"][: self._options.max_insights]):
            if not isinstance(raw, dict):
                continue
            insight_type = str(raw.get("insight_type", ""))
            if insight_type not in self._ALLOWED_TYPES:
                continue
            title = str(raw.get("title", "")).strip()
            narrative = str(raw.get("narrative", "")).strip()
            if not title or not narrative:
                continue
            confidence = max(0.0, min(1.0, float(raw.get("confidence", 0.0))))
            supporting_signal_ids = [
                str(value) for value in raw.get("supporting_signal_ids", []) if str(value) in valid_signals
            ]
            supporting_columns = [
                str(value) for value in raw.get("supporting_columns", []) if str(value) in valid_columns
            ]
            identity = f"{self._options.request_id}|{index}|{insight_type}|{title}"
            insight_id = "llm-" + __import__("hashlib").sha256(identity.encode("utf-8")).hexdigest()[:16]
            insights.append(
                LLMInsight(
                    insight_id=insight_id,
                    insight_type=insight_type,
                    title=title[:200],
                    narrative=narrative[:4000],
                    confidence=confidence,
                    supporting_signal_ids=supporting_signal_ids,
                    supporting_columns=supporting_columns,
                    provider=self._options.provider,
                    model=self._options.model,
                    request_id=self._options.request_id,
                )
            )
        if not insights:
            raise LLMEnrichmentError("NO_VALID_LLM_INSIGHTS", "LLM response contained no valid insights.")
        return insights

    @staticmethod
    def _extract_response_text(response: Any) -> str:
        """Normalize provider abstractions and direct Ollama-style responses."""

        if isinstance(response, str):
            return response
        if isinstance(response, Mapping):
            for key in ("response", "text", "generated_text", "content", "output", "result"):
                value = response.get(key)
                if isinstance(value, str):
                    return value
                if isinstance(value, Mapping):
                    try:
                        return LLMEnricher._extract_response_text(value)
                    except LLMEnrichmentError:
                        continue
        raise LLMEnrichmentError(
            "UNSUPPORTED_LLM_RESPONSE",
            f"Unsupported LLM response type: {type(response).__name__}.",
        )
