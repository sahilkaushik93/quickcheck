"""Resolve registered rules against profiled domains and relationships."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.understanding.context.context_builder import ContextBundle
from app.understanding.models import (
    DatasetProfile,
    ExecutionReadiness,
    ProvenanceType,
    RuleApplicability,
    RuleApplicabilityStatus,
    RuleParameterBinding,
)
from app.understanding.rules.models import RuleDefinition, RuleLevel, RuleRegistrySnapshot


@dataclass(frozen=True, slots=True)
class CapabilityCatalog:
    """Execution/profile features currently available to a run."""

    capabilities: frozenset[str]

    def missing(self, required: list[str]) -> list[str]:
        return sorted(set(required) - self.capabilities)


class RuleApplicabilityResolver:
    """Create run-specific applicability decisions without mutating rules."""

    def resolve(
        self,
        snapshot: RuleRegistrySnapshot,
        profile: DatasetProfile,
        context: ContextBundle,
        capability_catalog: CapabilityCatalog,
    ) -> list[RuleApplicability]:
        return [
            self._resolve_rule(rule, profile, context, capability_catalog)
            for rule in snapshot.rules
        ]

    def _resolve_rule(
        self,
        rule: RuleDefinition,
        profile: DatasetProfile,
        context: ContextBundle,
        catalog: CapabilityCatalog,
    ) -> RuleApplicability:
        targets, matched_domains = self._targets(rule, profile, context)
        if not rule.enabled:
            return self._decision(
                rule,
                RuleApplicabilityStatus.DISABLED,
                ExecutionReadiness.NOT_REQUIRED,
                targets,
                1.0,
                "Rule is disabled in the registry.",
                matched_domains,
                [],
            )
        if str(rule.lifecycle) != "approved" or rule.suggested_by_llm:
            return self._decision(
                rule,
                RuleApplicabilityStatus.NEEDS_REVIEW,
                ExecutionReadiness.NOT_REQUIRED,
                targets,
                self._confidence(targets, context) if targets else 1.0,
                "Rule is not approved; it is retained as a governed suggestion only.",
                matched_domains,
                [],
            )
        if not targets:
            return self._decision(
                rule,
                RuleApplicabilityStatus.NOT_APPLICABLE,
                ExecutionReadiness.NOT_REQUIRED,
                [],
                1.0,
                "No source columns or relationships matched the configured selector.",
                matched_domains,
                [],
            )
        missing = catalog.missing(
            [*rule.required_capabilities, f"handler.{rule.execution_handler}"]
        )
        readiness = (
            ExecutionReadiness.IMPLEMENTATION_PENDING
            if missing
            else ExecutionReadiness.READY
        )
        rationale = (
            "Approved rule selector matched this dataset; execution implementation is pending."
            if missing
            else "Approved rule selector matched this dataset and can be executed."
        )
        return self._decision(
            rule,
            RuleApplicabilityStatus.APPLICABLE,
            readiness,
            targets,
            self._confidence(targets, context),
            rationale,
            matched_domains,
            missing,
        )

    def _targets(
        self,
        rule: RuleDefinition,
        profile: DatasetProfile,
        context: ContextBundle,
    ) -> tuple[list[str], list[str]]:
        selector = rule.selector
        domain_assignments = {item.column_name: item for item in context.domains}
        relation_columns: set[str] = set()
        if selector.required_relationship_types:
            required = set(selector.required_relationship_types)
            for relationship in context.relationships:
                if str(relationship.relationship_type) in required:
                    relation_columns.update(relationship.source_columns)
                    relation_columns.update(relationship.target_columns)

        targets: list[str] = []
        matched_domains: set[str] = set()
        for column in profile.columns:
            assignment = domain_assignments.get(column.column_name)
            domain_match = (
                not selector.required_domains
                or "*" in selector.required_domains
                or bool(assignment and assignment.domain in selector.required_domains)
            )
            semantic_match = (
                not selector.semantic_types
                or bool(assignment and assignment.semantic_type in selector.semantic_types)
            )
            name_match = (
                not selector.column_name_patterns
                or any(re.search(pattern, column.column_name, re.I) for pattern in selector.column_name_patterns)
            )
            relationship_match = column.column_name in relation_columns
            confidence_match = (
                assignment is None or assignment.confidence >= selector.minimum_domain_confidence
            )
            excluded = any(
                re.search(pattern, column.column_name, re.I)
                for pattern in selector.excluded_column_patterns
            )
            configured_checks: list[bool] = []
            if selector.required_domains:
                configured_checks.append(domain_match)
            if selector.semantic_types:
                configured_checks.append(semantic_match)
            if selector.column_name_patterns:
                configured_checks.append(name_match)
            if selector.required_relationship_types:
                configured_checks.append(relationship_match)
            criteria_match = (
                all(configured_checks)
                if selector.match_mode == "all"
                else any(configured_checks)
            ) if configured_checks else True
            selected = confidence_match and criteria_match and not excluded
            if selected and not excluded:
                targets.append(column.column_name)
                if assignment:
                    matched_domains.add(assignment.domain)

        if rule.level == RuleLevel.DATASET.value and targets:
            targets = sorted(set(targets), key=str.casefold)
        return sorted(set(targets), key=str.casefold), sorted(matched_domains)

    @staticmethod
    def _confidence(targets: list[str], context: ContextBundle) -> float:
        values = [item.confidence for item in context.domains if item.column_name in targets]
        return round(sum(values) / len(values), 6) if values else 1.0

    @staticmethod
    def _decision(
        rule: RuleDefinition,
        status: RuleApplicabilityStatus,
        execution_readiness: ExecutionReadiness,
        targets: list[str],
        confidence: float,
        rationale: str,
        domains: list[str],
        missing: list[str],
    ) -> RuleApplicability:
        bindings = [
            RuleParameterBinding(name=name, value=definition.default, source="rule_default")
            for name, definition in sorted(rule.parameters.items())
        ]
        return RuleApplicability(
            rule_id=rule.rule_id,
            rule_version=rule.version,
            check_name=rule.check_name,
            status=status,
            execution_readiness=execution_readiness,
            target_columns=targets,
            confidence=confidence,
            rationale=rationale,
            matched_domains=domains,
            required_capabilities=sorted(
                set([*rule.required_capabilities, f"handler.{rule.execution_handler}"])
            ),
            missing_capabilities=missing,
            parameters=bindings,
            provenance=[ProvenanceType.CONFIGURATION, ProvenanceType.DETERMINISTIC],
            suggested_by_llm=rule.suggested_by_llm,
            approved=str(rule.lifecycle) == "approved" and not rule.suggested_by_llm,
        )
