"""Policy-driven conversion of profile/context observations into DQ signals.

This component evaluates only metrics already present in bounded profile state.
It never scans source rows, calls an LLM, or invents unavailable rates.  Missing
metric coverage is returned explicitly so later rule planning can schedule the
corresponding check in the Execution Layer.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from string import Formatter
from typing import Any, Protocol

from app.understanding.context.context_builder import ContextBundle
from app.understanding.models import (
    DQSignal,
    DatasetProfile,
    EvidenceReference,
    ProvenanceType,
    RecommendedAction,
    RequiredCheck,
    Severity,
)


class SignalDetectionError(RuntimeError):
    """Configuration/evaluation failure safe for logs and API responses."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class MetricObservation:
    """One aggregate, non-sensitive metric available for policy evaluation."""

    check_name: str
    metric_name: str
    value: float
    affected_columns: tuple[str, ...]
    domain: str
    sample_size: int
    confidence: float
    exact: bool
    details: tuple[tuple[str, str | int | float | bool | None], ...] = ()


@dataclass(frozen=True, slots=True)
class MetricAvailability:
    """Coverage status for one required check."""

    check_name: str
    available: bool
    reason: str
    observation_count: int = 0


@dataclass(frozen=True, slots=True)
class SignalDetectionResult:
    """Ranked signals plus transparent metric-coverage information."""

    signals: tuple[DQSignal, ...]
    metric_availability: tuple[MetricAvailability, ...]
    warnings: tuple[str, ...] = ()


class MetricExtractor(Protocol):
    """Extension point for future aggregate metrics without changing policies."""

    def extract(
        self,
        profile: DatasetProfile,
        context: ContextBundle,
    ) -> tuple[list[MetricObservation], dict[str, str]]:
        """Return observations and unavailable-check reasons."""

        ...


class ProfileMetricExtractor:
    """Extract metrics currently supported by the MVP profile contracts."""

    def extract(
        self,
        profile: DatasetProfile,
        context: ContextBundle,
    ) -> tuple[list[MetricObservation], dict[str, str]]:
        domain_by_column = {
            assignment.column_name.casefold(): assignment.domain
            for assignment in context.domains
        }
        observations: list[MetricObservation] = []

        for column in profile.columns:
            domain = domain_by_column.get(column.column_name.casefold(), "unknown")
            observations.append(
                MetricObservation(
                    check_name=RequiredCheck.FILL_RATE.value,
                    metric_name="fill_rate",
                    value=column.fill_rate,
                    affected_columns=(column.column_name,),
                    domain=domain,
                    sample_size=column.row_count,
                    confidence=1.0,
                    exact=True,
                )
            )

            if domain == "conversation.topic" and column.non_null_count and column.top_values:
                dominant_count = max(item.count for item in column.top_values)
                observations.append(
                    MetricObservation(
                        check_name=RequiredCheck.TOPIC_DISTRIBUTION.value,
                        metric_name="dominant_topic_share",
                        value=min(1.0, dominant_count / column.non_null_count),
                        affected_columns=(column.column_name,),
                        domain=domain,
                        sample_size=column.non_null_count,
                        confidence=0.75,
                        exact=False,
                        details=(
                            ("frequency_method", "bounded_heavy_hitter"),
                            ("distinct_topics", column.distinct_count),
                        ),
                    )
                )

        available_checks = {observation.check_name for observation in observations}
        unavailable = {
            RequiredCheck.AGENT_SPEAKER_TAG_VALIDATION.value: (
                "The profile records speaker-pattern presence but not invalid-tag counts; "
                "the Execution Layer must evaluate transcript structure."
            ),
            RequiredCheck.NON_ENGLISH_SPELLING.value: (
                "Language/token counters are not present in the current profile; schedule the execution rule."
            ),
            RequiredCheck.MISTRANSLATED_RATE.value: (
                "No approved mistranslation lexicon counters are present in the profile; schedule the execution rule."
            ),
            RequiredCheck.PII_DETECTION.value: (
                "Only redacted pattern names are profiled; matched-record counts must be computed by the execution rule."
            ),
            RequiredCheck.SPEECH_PER_DURATION_RATE.value: (
                "Speech-rate evaluation requires paired transcript-word and duration aggregates not present in independent column profiles."
            ),
        }
        for check_name in list(unavailable):
            if check_name in available_checks:
                del unavailable[check_name]
        if RequiredCheck.TOPIC_DISTRIBUTION.value not in available_checks:
            unavailable[RequiredCheck.TOPIC_DISTRIBUTION.value] = (
                "No topic-domain column with bounded frequency observations was available."
            )
        return observations, unavailable


@dataclass(frozen=True, slots=True)
class _Policy:
    policy_id: str
    check_name: str
    metric: str
    scope: str
    applicable_domains: tuple[str, ...]
    bands: tuple[dict[str, Any], ...]
    rationale_template: str
    recommended_actions: tuple[dict[str, Any], ...]
    minimum_distinct_topics: int | None = None


class SignalDetector:
    """Evaluate deterministic signal policies and rank triggered signals."""

    _SEVERITY_RANK = {
        Severity.CRITICAL.value: 5,
        Severity.HIGH.value: 4,
        Severity.MEDIUM.value: 3,
        Severity.LOW.value: 2,
        Severity.INFO.value: 1,
    }

    def __init__(
        self,
        policy_config: dict[str, Any],
        metric_extractor: MetricExtractor | None = None,
    ) -> None:
        self._evaluation = dict(policy_config.get("evaluation", {}))
        self._policies = self._parse_policies(policy_config.get("policies", []))
        self._extractor = metric_extractor or ProfileMetricExtractor()
        self._policies_by_metric = {
            (policy.check_name, policy.metric): policy for policy in self._policies
        }

    def detect(
        self,
        profile: DatasetProfile,
        context: ContextBundle,
    ) -> list[DQSignal]:
        """Return ranked signals for compatibility with ``UnderstandingOutput``."""

        return list(self.evaluate(profile, context).signals)

    def evaluate(
        self,
        profile: DatasetProfile,
        context: ContextBundle,
    ) -> SignalDetectionResult:
        """Return signals plus transparent coverage and suppression information."""

        try:
            observations, unavailable = self._extractor.extract(profile, context)
            signals: list[DQSignal] = []
            warnings: list[str] = []
            observation_counts: dict[str, int] = {}
            for observation in observations:
                observation_counts[observation.check_name] = observation_counts.get(observation.check_name, 0) + 1
                policy = self._policies_by_metric.get((observation.check_name, observation.metric_name))
                if policy is None:
                    warnings.append(
                        f"No signal policy matched {observation.check_name}/{observation.metric_name}."
                    )
                    continue
                if not self._domain_applies(policy, observation.domain):
                    continue
                if policy.minimum_distinct_topics is not None:
                    distinct_topics = dict(observation.details).get("distinct_topics")
                    if not isinstance(distinct_topics, int) or distinct_topics < policy.minimum_distinct_topics:
                        warnings.append(
                            f"Suppressed {observation.check_name} for {','.join(observation.affected_columns)} "
                            "because distinct-topic coverage was insufficient."
                        )
                        continue
                minimum_rows = int(self._evaluation.get("minimum_rows_for_rate_policy", 0))
                if not observation.exact and observation.sample_size < minimum_rows:
                    warnings.append(
                        f"Suppressed {observation.check_name} for {','.join(observation.affected_columns)} "
                        f"because sample size {observation.sample_size} is below {minimum_rows}."
                    )
                    continue
                band = next(
                    (candidate for candidate in policy.bands if self._matches(observation.value, candidate)),
                    None,
                )
                if band is not None:
                    signals.append(self._build_signal(policy, band, observation))

            signals.sort(
                key=lambda signal: (
                    -self._SEVERITY_RANK.get(str(signal.severity), 0),
                    -signal.confidence,
                    signal.signal_id,
                )
            )
            required = [check.value for check in RequiredCheck]
            availability = tuple(
                MetricAvailability(
                    check_name=check,
                    available=check not in unavailable,
                    reason=(
                        "One or more aggregate observations were available for policy evaluation."
                        if check not in unavailable
                        else unavailable[check]
                    ),
                    observation_count=observation_counts.get(check, 0),
                )
                for check in required
            )
            return SignalDetectionResult(
                signals=tuple(signals),
                metric_availability=availability,
                warnings=tuple(dict.fromkeys(warnings)),
            )
        except SignalDetectionError:
            raise
        except Exception as exc:
            raise SignalDetectionError(
                "SIGNAL_DETECTION_FAILED",
                f"DQ signal evaluation failed ({type(exc).__name__}).",
            ) from exc

    @staticmethod
    def _parse_policies(raw_policies: list[dict[str, Any]]) -> tuple[_Policy, ...]:
        policies: list[_Policy] = []
        seen: set[str] = set()
        for raw in raw_policies:
            policy_id = str(raw.get("policy_id", "")).strip()
            if not policy_id or policy_id in seen:
                raise SignalDetectionError(
                    "INVALID_SIGNAL_POLICY",
                    "Signal policy IDs must be non-empty and unique.",
                )
            seen.add(policy_id)
            bands = tuple(raw.get("bands", []))
            if not bands:
                raise SignalDetectionError(
                    "INVALID_SIGNAL_POLICY",
                    f"Signal policy {policy_id!r} does not define severity bands.",
                )
            policies.append(
                _Policy(
                    policy_id=policy_id,
                    check_name=str(raw.get("check_name", "")),
                    metric=str(raw.get("metric", "")),
                    scope=str(raw.get("scope", "")),
                    applicable_domains=tuple(map(str, raw.get("applicable_domains", ["*"]))),
                    bands=bands,
                    rationale_template=str(raw.get("rationale_template", "Data-quality threshold exceeded.")),
                    recommended_actions=tuple(raw.get("recommended_actions", [])),
                    minimum_distinct_topics=(
                        int(raw["minimum_distinct_topics"])
                        if "minimum_distinct_topics" in raw
                        else None
                    ),
                )
            )
        if not policies:
            raise SignalDetectionError("NO_SIGNAL_POLICIES", "No signal policies were configured.")
        return tuple(policies)

    @staticmethod
    def _domain_applies(policy: _Policy, domain: str) -> bool:
        return "*" in policy.applicable_domains or domain in policy.applicable_domains

    @staticmethod
    def _matches(value: float, band: dict[str, Any]) -> bool:
        operator = str(band.get("operator", ""))
        if operator == "lt":
            return value < float(band["value"])
        if operator == "lte":
            return value <= float(band["value"])
        if operator == "gt":
            return value > float(band["value"])
        if operator == "gte":
            return value >= float(band["value"])
        if operator == "eq":
            return value == float(band["value"])
        if operator == "outside_inclusive":
            return value < float(band["lower"]) or value > float(band["upper"])
        raise SignalDetectionError(
            "UNSUPPORTED_POLICY_OPERATOR",
            f"Unsupported signal-policy operator {operator!r}.",
        )

    def _build_signal(
        self,
        policy: _Policy,
        band: dict[str, Any],
        observation: MetricObservation,
    ) -> DQSignal:
        severity = Severity(str(band["severity"]))
        threshold = self._threshold_value(band)
        variables: dict[str, Any] = {
            "column_name": ", ".join(observation.affected_columns),
            "metric_value": round(observation.value, 6),
            "metric_value_pct": f"{observation.value * 100:.2f}%",
            "threshold": threshold,
            "threshold_pct": (
                f"{float(threshold) * 100:.2f}%" if isinstance(threshold, (int, float)) else threshold
            ),
            "lower_threshold": band.get("lower"),
            "upper_threshold": band.get("upper"),
            **dict(observation.details),
        }
        signal_id = self._signal_id(policy, observation)
        evidence = EvidenceReference(
            evidence_type="aggregate_metric",
            summary=f"Policy {policy.policy_id} evaluated aggregate metric {observation.metric_name}.",
            metric_name=observation.metric_name,
            metric_value=round(observation.value, 8),
            column_names=list(observation.affected_columns),
            sample_count=observation.sample_size,
            redacted=True,
        )
        return DQSignal(
            signal_id=signal_id,
            signal_type=str(band["signal_type"]),
            dimension=self._dimension(policy.check_name),
            severity=severity,
            confidence=observation.confidence,
            affected_columns=list(observation.affected_columns),
            observed_value=round(observation.value, 8),
            threshold=threshold,
            message=self._format(policy.rationale_template, variables),
            evidence=[evidence],
            recommended_actions=self._actions(policy, severity, variables),
            policy_id=policy.policy_id,
            provenance=[ProvenanceType.DETERMINISTIC, ProvenanceType.CONFIGURATION],
        )

    @staticmethod
    def _threshold_value(band: dict[str, Any]) -> float | dict[str, float] | None:
        if "value" in band:
            return float(band["value"])
        if "lower" in band and "upper" in band:
            return {"lower": float(band["lower"]), "upper": float(band["upper"])}
        return None

    def _actions(
        self,
        policy: _Policy,
        signal_severity: Severity,
        variables: dict[str, Any],
    ) -> list[RecommendedAction]:
        actions: list[RecommendedAction] = []
        for raw in policy.recommended_actions:
            priority = (
                signal_severity
                if raw.get("priority_from_signal", False)
                else Severity(str(raw.get("priority", Severity.MEDIUM.value)))
            )
            actions.append(
                RecommendedAction(
                    action_code=str(raw["action_code"]),
                    title=str(raw["title"]),
                    description=self._format(str(raw["description_template"]), variables),
                    owner_role=str(raw["owner_role"]) if raw.get("owner_role") else None,
                    priority=priority,
                    source=ProvenanceType.CONFIGURATION,
                    requires_approval=True,
                )
            )
        return actions

    @staticmethod
    def _format(template: str, variables: dict[str, Any]) -> str:
        missing = {
            field_name
            for _, field_name, _, _ in Formatter().parse(template)
            if field_name and field_name not in variables
        }
        if missing:
            raise SignalDetectionError(
                "INVALID_POLICY_TEMPLATE",
                f"Signal policy template references unsupported variables: {sorted(missing)}.",
            )
        return template.format_map(variables)

    @staticmethod
    def _signal_id(policy: _Policy, observation: MetricObservation) -> str:
        identity = "|".join(
            [
                policy.policy_id,
                observation.metric_name,
                *sorted(observation.affected_columns, key=str.casefold),
            ]
        )
        return "sig-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _dimension(check_name: str) -> str:
        return {
            RequiredCheck.FILL_RATE.value: "completeness",
            RequiredCheck.TOPIC_DISTRIBUTION.value: "representativeness",
            RequiredCheck.AGENT_SPEAKER_TAG_VALIDATION.value: "validity",
            RequiredCheck.NON_ENGLISH_SPELLING.value: "accuracy",
            RequiredCheck.MISTRANSLATED_RATE.value: "accuracy",
            RequiredCheck.PII_DETECTION.value: "privacy",
            RequiredCheck.SPEECH_PER_DURATION_RATE.value: "consistency",
        }.get(check_name, "quality")
