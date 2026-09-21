"""Request-scoped, lazy registry for deterministic execution handlers."""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import AbstractSet

from app.execution.handlers.base import RuleHandler
from app.understanding.models import ExecutionPlan, RequiredCheck, RuleExecutionStep


class HandlerRegistryError(RuntimeError):
    """Safe structured handler registration or plan-resolution failure."""

    def __init__(self, code: str, message: str, *, check_name: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.check_name = check_name

    def as_dict(self) -> dict[str, str]:
        result = {"code": self.code, "message": self.message}
        if self.check_name is not None:
            result["check_name"] = self.check_name
        return result


@dataclass(frozen=True, slots=True)
class HandlerImportSpec:
    """Lazy import location for one built-in handler implementation."""

    check_name: str
    module: str
    class_name: str


@dataclass(frozen=True, slots=True)
class HandlerResolutionIssue:
    """Explicit non-runnable plan step diagnostic."""

    step_id: str
    rule_id: str
    check_name: str
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class ResolvedHandlerStep:
    """One dependency-ordered plan step bound to its handler."""

    step: RuleExecutionStep
    handler: RuleHandler


@dataclass(frozen=True, slots=True)
class HandlerPlanResolution:
    """Complete deterministic registry outcome for an execution plan."""

    runnable: tuple[ResolvedHandlerStep, ...]
    issues: tuple[HandlerResolutionIssue, ...]

    @property
    def complete(self) -> bool:
        return not self.issues

    @property
    def missing_handler_count(self) -> int:
        return sum(1 for issue in self.issues if issue.code == "HANDLER_NOT_AVAILABLE")


HandlerFactory = Callable[[], RuleHandler]


_BUILTIN_IMPORTS: tuple[HandlerImportSpec, ...] = (
    HandlerImportSpec(
        RequiredCheck.FILL_RATE.value,
        "app.execution.handlers.fill_rate",
        "FillRateHandler",
    ),
    HandlerImportSpec(
        RequiredCheck.TOPIC_DISTRIBUTION.value,
        "app.execution.handlers.topic_distribution",
        "TopicDistributionHandler",
    ),
    HandlerImportSpec(
        RequiredCheck.AGENT_SPEAKER_TAG_VALIDATION.value,
        "app.execution.handlers.agent_speaker_tag_validation",
        "AgentSpeakerTagValidationHandler",
    ),
    HandlerImportSpec(
        RequiredCheck.NON_ENGLISH_SPELLING.value,
        "app.execution.handlers.non_english_spelling",
        "NonEnglishSpellingHandler",
    ),
    HandlerImportSpec(
        RequiredCheck.MISTRANSLATED_RATE.value,
        "app.execution.handlers.mistranslated_rate",
        "MistranslatedRateHandler",
    ),
    HandlerImportSpec(
        RequiredCheck.PII_DETECTION.value,
        "app.execution.handlers.pii_detection",
        "PIIDetectionHandler",
    ),
    HandlerImportSpec(
        RequiredCheck.SPEECH_PER_DURATION_RATE.value,
        "app.execution.handlers.speech_per_duration_rate",
        "SpeechPerDurationRateHandler",
    ),
)


class HandlerRegistry:
    """Map plan check names to stateless handlers without global mutable state.

    Built-ins are imported only when selected by an execution plan, allowing the
    API to start while a handler is explicitly reported as implementation
    pending. Custom handler factories can be supplied per application or test.
    """

    def __init__(
        self,
        *,
        import_specs: Iterable[HandlerImportSpec] = _BUILTIN_IMPORTS,
        factories: Mapping[str, HandlerFactory] | None = None,
    ) -> None:
        specs: dict[str, HandlerImportSpec] = {}
        for spec in import_specs:
            key = self._normalize(spec.check_name)
            if key in specs:
                raise HandlerRegistryError(
                    "DUPLICATE_HANDLER",
                    "More than one import specification exists for a check.",
                    check_name=spec.check_name,
                )
            specs[key] = spec
        custom: dict[str, HandlerFactory] = {}
        for name, factory in (factories or {}).items():
            key = self._normalize(name)
            if key in custom:
                raise HandlerRegistryError(
                    "DUPLICATE_HANDLER",
                    "More than one custom handler exists for a check.",
                    check_name=name,
                )
            custom[key] = factory
        self._import_specs = MappingProxyType(specs)
        self._factories = MappingProxyType(custom)

    @classmethod
    def from_handlers(cls, handlers: Iterable[RuleHandler]) -> "HandlerRegistry":
        """Build an isolated registry from concrete stateless handler objects."""

        factories: dict[str, HandlerFactory] = {}
        for handler in handlers:
            name = cls._normalize(str(handler.check_name))
            if name in factories:
                raise HandlerRegistryError(
                    "DUPLICATE_HANDLER",
                    "More than one handler was supplied for a check.",
                    check_name=name,
                )
            factories[name] = lambda selected=handler: selected
        return cls(import_specs=(), factories=factories)

    def with_handler(self, handler: RuleHandler) -> "HandlerRegistry":
        """Return a new registry with one explicit handler override."""

        name = self._normalize(str(handler.check_name))
        factories = dict(self._factories)
        factories[name] = lambda selected=handler: selected
        return HandlerRegistry(
            import_specs=self._import_specs.values(),
            factories=factories,
        )

    def registered_check_names(self) -> tuple[str, ...]:
        """Return configured names, including lazily imported built-ins."""

        return tuple(sorted(set(self._import_specs) | set(self._factories)))

    def resolve(self, check_name: str) -> RuleHandler:
        """Resolve and validate one handler without caching mutable run state."""

        key = self._normalize(check_name)
        factory = self._factories.get(key)
        if factory is not None:
            handler = self._construct(factory, key)
        else:
            spec = self._import_specs.get(key)
            if spec is None:
                raise HandlerRegistryError(
                    "HANDLER_NOT_REGISTERED",
                    "No execution handler is registered for the requested check.",
                    check_name=check_name,
                )
            handler = self._import_handler(spec)
        if self._normalize(str(handler.check_name)) != key:
            raise HandlerRegistryError(
                "HANDLER_CHECK_MISMATCH",
                "Loaded handler declares a different check name.",
                check_name=check_name,
            )
        return handler

    def resolve_plan(
        self,
        plan: ExecutionPlan,
        *,
        approved_rule_ids: AbstractSet[str] | None = None,
    ) -> HandlerPlanResolution:
        """Resolve plan steps in stable dependency/priority order.

        When ``approved_rule_ids`` is provided, a plan step not present in that
        allowlist is explicitly rejected. When omitted, the versioned execution
        plan is treated as the approval boundary created by Understanding.
        """

        ordered = self._dependency_order(plan.steps)
        runnable: list[ResolvedHandlerStep] = []
        issues: list[HandlerResolutionIssue] = []
        blocked_step_ids: set[str] = set()
        for step in ordered:
            name = str(step.check_name)
            if approved_rule_ids is not None and step.rule_id not in approved_rule_ids:
                issues.append(
                    self._issue(
                        step,
                        "RULE_NOT_APPROVED",
                        "The rule is not present in the approved applicability allowlist.",
                    )
                )
                blocked_step_ids.add(step.step_id)
                continue
            if any(dependency in blocked_step_ids for dependency in step.dependencies):
                issues.append(
                    self._issue(
                        step,
                        "DEPENDENCY_NOT_RUNNABLE",
                        "At least one required execution dependency is not runnable.",
                    )
                )
                blocked_step_ids.add(step.step_id)
                continue
            try:
                handler = self.resolve(name)
                handler.validate_step(step)
            except HandlerRegistryError as exc:
                code = (
                    "HANDLER_NOT_AVAILABLE"
                    if exc.code in {"HANDLER_NOT_REGISTERED", "HANDLER_IMPORT_FAILED"}
                    else exc.code
                )
                issues.append(self._issue(step, code, exc.message))
                blocked_step_ids.add(step.step_id)
                continue
            except Exception as exc:
                issues.append(
                    self._issue(
                        step,
                        getattr(exc, "code", "HANDLER_VALIDATION_FAILED"),
                        getattr(
                            exc,
                            "message",
                            f"Handler validation failed ({type(exc).__name__}).",
                        ),
                    )
                )
                blocked_step_ids.add(step.step_id)
                continue
            runnable.append(ResolvedHandlerStep(step=step, handler=handler))
        return HandlerPlanResolution(tuple(runnable), tuple(issues))

    @staticmethod
    def _dependency_order(
        steps: Iterable[RuleExecutionStep],
    ) -> tuple[RuleExecutionStep, ...]:
        materialized = tuple(steps)
        by_id = {step.step_id: step for step in materialized}
        if len(by_id) != len(materialized):
            raise HandlerRegistryError(
                "DUPLICATE_EXECUTION_STEP", "Execution plan step identifiers must be unique."
            )
        remaining = set(by_id)
        completed: set[str] = set()
        ordered: list[RuleExecutionStep] = []
        while remaining:
            available = [
                by_id[step_id]
                for step_id in remaining
                if set(by_id[step_id].dependencies).issubset(completed)
            ]
            if not available:
                raise HandlerRegistryError(
                    "CYCLIC_EXECUTION_DEPENDENCY",
                    "Execution plan dependencies contain a cycle or unknown step.",
                )
            available.sort(key=lambda step: (step.priority, step.step_id, step.rule_id))
            for step in available:
                ordered.append(step)
                completed.add(step.step_id)
                remaining.remove(step.step_id)
        return tuple(ordered)

    @staticmethod
    def _construct(factory: HandlerFactory, check_name: str) -> RuleHandler:
        try:
            handler = factory()
        except Exception as exc:
            raise HandlerRegistryError(
                "HANDLER_CONSTRUCTION_FAILED",
                f"Execution handler construction failed ({type(exc).__name__}).",
                check_name=check_name,
            ) from exc
        if not isinstance(handler, RuleHandler):
            raise HandlerRegistryError(
                "INVALID_HANDLER_TYPE",
                "Registered factory did not return a RuleHandler.",
                check_name=check_name,
            )
        return handler

    def _import_handler(self, spec: HandlerImportSpec) -> RuleHandler:
        try:
            module = importlib.import_module(spec.module)
            handler_type = getattr(module, spec.class_name)
        except (ImportError, AttributeError) as exc:
            raise HandlerRegistryError(
                "HANDLER_IMPORT_FAILED",
                "The selected execution handler implementation is not available.",
                check_name=spec.check_name,
            ) from exc
        if not isinstance(handler_type, type) or not issubclass(handler_type, RuleHandler):
            raise HandlerRegistryError(
                "INVALID_HANDLER_TYPE",
                "Imported execution handler does not implement RuleHandler.",
                check_name=spec.check_name,
            )
        return self._construct(handler_type, spec.check_name)

    @staticmethod
    def _issue(
        step: RuleExecutionStep, code: str, message: str
    ) -> HandlerResolutionIssue:
        return HandlerResolutionIssue(
            step_id=step.step_id,
            rule_id=step.rule_id,
            check_name=str(step.check_name),
            code=code,
            message=message,
        )

    @staticmethod
    def _normalize(value: str) -> str:
        normalized = value.strip().casefold()
        if not normalized:
            raise HandlerRegistryError(
                "INVALID_CHECK_NAME", "Handler check name cannot be empty."
            )
        return normalized


__all__ = [
    "HandlerImportSpec",
    "HandlerPlanResolution",
    "HandlerRegistry",
    "HandlerRegistryError",
    "HandlerResolutionIssue",
    "ResolvedHandlerStep",
]
