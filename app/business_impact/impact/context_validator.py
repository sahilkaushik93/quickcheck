"""Validate bounded, non-sensitive business context before impact mapping."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from app.business_impact.impact.models import BusinessContext


@dataclass(frozen=True, slots=True)
class BusinessContextValidationResult:
    """Validation outcome; callers map `sufficient=False` to insufficient_context."""

    context: BusinessContext | None
    sufficient: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...]


class BusinessContextValidator:
    """Schema-driven safety and adequacy validator with no data-source access."""

    def __init__(self, schema: Mapping[str, Any]) -> None:
        self._schema = schema
        extensions = schema.get("x-undq-validation", {})
        self._required_contextual = tuple(extensions.get("required_for_contextual_impact_mapping", ()))
        self._forbidden = tuple(re.compile(item) for item in extensions.get("forbidden_key_patterns", ()))
        self._max_bytes = int(extensions.get("maximum_serialized_bytes", 16_384))

    @classmethod
    def from_file(cls, path: str | Path) -> "BusinessContextValidator":
        try:
            schema = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("BUSINESS_CONTEXT_SCHEMA_LOAD_FAILED") from exc
        if not isinstance(schema, Mapping):
            raise ValueError("BUSINESS_CONTEXT_SCHEMA_INVALID")
        return cls(schema)

    def validate(self, context: BusinessContext | None) -> BusinessContextValidationResult:
        """Return an explicit insufficient-context result instead of guessing context."""

        if context is None:
            return BusinessContextValidationResult(None, False, ("Business context was not supplied.",), ())
        payload = context.model_dump(mode="json")
        if len(json.dumps(payload, sort_keys=True).encode("utf-8")) > self._max_bytes:
            return BusinessContextValidationResult(None, False, ("Business context exceeds the governed size limit.",), ())
        forbidden = [key for key in _walk_keys(payload) if any(pattern.search(key) for pattern in self._forbidden)]
        if forbidden:
            return BusinessContextValidationResult(None, False, ("Business context contains prohibited sensitive or technical fields.",), ())
        required = [name for name in self._required_contextual if not payload.get(name)]
        if required:
            return BusinessContextValidationResult(context, False, ("Business context is missing fields required for contextual impact mapping.",), tuple(sorted(required)))
        return BusinessContextValidationResult(context, True, (), ())


def _walk_keys(value: Any) -> list[str]:
    if isinstance(value, Mapping):
        return [str(key) for key in value] + [key for item in value.values() for key in _walk_keys(item)]
    if isinstance(value, list):
        return [key for item in value for key in _walk_keys(item)]
    return []
