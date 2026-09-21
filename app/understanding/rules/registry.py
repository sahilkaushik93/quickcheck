"""Filesystem-backed loader for versioned JSON DQ rules."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.understanding.rules.models import RuleDefinition, RuleLifecycle, RuleRegistrySnapshot


class RuleRegistryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


class RuleRegistry:
    """Load, validate, fingerprint and query rule definitions."""

    def __init__(self, rules_directory: str | Path) -> None:
        self._directory = Path(rules_directory).expanduser().resolve()

    def load(self) -> RuleRegistrySnapshot:
        if not self._directory.is_dir():
            raise RuleRegistryError("RULE_DIRECTORY_NOT_FOUND", "Rule registry directory was not found.")
        paths = sorted(
            self._directory.rglob("*.json"),
            key=lambda path: path.relative_to(self._directory).as_posix().casefold(),
        )
        if not paths:
            raise RuleRegistryError("RULE_REGISTRY_EMPTY", "No JSON rule definitions were found.")

        rules: list[RuleDefinition] = []
        keys: set[str] = set()
        active_checks: set[str] = set()
        canonical_documents: list[dict[str, Any]] = []
        warnings: list[str] = []
        for path in paths:
            relative_name = path.relative_to(self._directory).as_posix()
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
                rule = RuleDefinition.model_validate(document)
            except (OSError, json.JSONDecodeError, ValidationError) as exc:
                raise RuleRegistryError(
                    "INVALID_RULE_FILE",
                    f"Rule file {relative_name!r} is invalid ({type(exc).__name__}).",
                ) from exc
            if rule.registry_key in keys:
                raise RuleRegistryError("DUPLICATE_RULE_VERSION", f"Duplicate rule version {rule.registry_key!r}.")
            keys.add(rule.registry_key)
            if rule.enabled and rule.lifecycle == RuleLifecycle.APPROVED.value:
                check = str(rule.check_name)
                if check in active_checks:
                    raise RuleRegistryError(
                        "MULTIPLE_ACTIVE_RULES",
                        f"More than one enabled approved rule exists for check {check!r}.",
                    )
                active_checks.add(check)
            elif rule.enabled and rule.lifecycle != RuleLifecycle.APPROVED.value:
                warnings.append(f"Enabled rule {rule.registry_key} is not approved and will not execute.")
            rules.append(rule)
            canonical_documents.append(rule.model_dump(mode="json", exclude_none=True))

        fingerprint_source = json.dumps(
            canonical_documents,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()
        return RuleRegistrySnapshot(
            rules=tuple(rules),
            fingerprint=fingerprint,
            source_directory=str(self._directory),
            warnings=tuple(warnings),
        )

    @staticmethod
    def executable_rules(snapshot: RuleRegistrySnapshot) -> tuple[RuleDefinition, ...]:
        return tuple(
            rule
            for rule in snapshot.rules
            if rule.enabled and rule.lifecycle == RuleLifecycle.APPROVED.value and not rule.suggested_by_llm
        )
