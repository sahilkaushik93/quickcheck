"""Build the deterministic Understanding-to-Execution rule plan."""

from __future__ import annotations

import hashlib

from app.understanding.models import (
    ExecutionReadiness,
    ExecutionPlan,
    PlanStatus,
    RuleApplicability,
    RuleApplicabilityStatus,
    RuleExecutionStep,
)
from app.understanding.rules.models import RuleDefinition, RuleRegistrySnapshot


class PlanBuildError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ExecutionPlanBuilder:
    """Translate applicable rule decisions into a serializable execution plan."""

    def build(
        self,
        snapshot: RuleRegistrySnapshot,
        decisions: list[RuleApplicability],
        *,
        run_id: str,
    ) -> ExecutionPlan:
        rules = {rule.rule_id: rule for rule in snapshot.rules}
        decision_by_rule = {decision.rule_id: decision for decision in decisions}
        selected = {
            rule_id: decision
            for rule_id, decision in decision_by_rule.items()
            if decision.status == RuleApplicabilityStatus.APPLICABLE.value
        }
        warnings: list[str] = list(snapshot.warnings)
        pending = {
            rule_id
            for rule_id, decision in selected.items()
            if decision.execution_readiness
            == ExecutionReadiness.IMPLEMENTATION_PENDING.value
        }
        runnable = set(selected) - pending

        # A rule is not runnable when any dependency is not runnable. Propagate
        # that state until stable so downstream steps never reference a missing
        # execution step.
        changed = True
        while changed:
            changed = False
            for rule_id in sorted(runnable):
                rule = rules.get(rule_id)
                if rule is None:
                    raise PlanBuildError(
                        "RULE_NOT_FOUND",
                        f"Selected rule {rule_id!r} is missing from the registry.",
                    )
                if any(dependency not in runnable for dependency in rule.depends_on):
                    runnable.remove(rule_id)
                    pending.add(rule_id)
                    changed = True
                    break

        step_ids = {rule_id: self._step_id(run_id, rule_id) for rule_id in runnable}
        steps: list[RuleExecutionStep] = []
        for rule_id in sorted(runnable):
            rule = rules.get(rule_id)
            if rule is None:
                raise PlanBuildError("RULE_NOT_FOUND", f"Selected rule {rule_id!r} is missing from the registry.")
            decision = selected[rule_id]
            steps.append(
                RuleExecutionStep(
                    step_id=step_ids[rule_id],
                    rule_id=rule.rule_id,
                    rule_version=rule.version,
                    check_name=rule.check_name,
                    target_columns=decision.target_columns,
                    parameters={binding.name: binding.value for binding in decision.parameters},
                    dependencies=[step_ids[dependency] for dependency in rule.depends_on],
                    priority=self._priority(rule.severity),
                    parallelizable=True,
                )
            )

        for rule_id in sorted(pending):
            decision = selected[rule_id]
            missing = ", ".join(decision.missing_capabilities) or "a runnable dependency"
            warnings.append(
                f"Rule {rule_id} is selected for this dataset but execution is pending: {missing}."
            )

        if steps and not pending:
            status = PlanStatus.READY
        elif steps:
            status = PlanStatus.PARTIAL
        else:
            status = PlanStatus.BLOCKED
        plan_identity = (
            f"{run_id}|{snapshot.fingerprint}|"
            f"{'|'.join(sorted(selected))}|{'|'.join(step.step_id for step in steps)}"
        )
        plan_id = "plan-" + hashlib.sha256(plan_identity.encode("utf-8")).hexdigest()[:16]
        return ExecutionPlan(
            plan_id=plan_id,
            status=status,
            steps=steps,
            selected_rules=sorted(selected),
            implementation_pending_rules=sorted(pending),
            # Retained for compatibility with existing API consumers. New code
            # should read implementation_pending_rules.
            blocked_rules=sorted(pending),
            warnings=list(dict.fromkeys(warnings)),
            registry_fingerprint=snapshot.fingerprint,
        )

    @staticmethod
    def _step_id(run_id: str, rule_id: str) -> str:
        return "step-" + hashlib.sha256(f"{run_id}|{rule_id}".encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _priority(severity: str) -> int:
        return {"critical": 10, "high": 20, "medium": 30, "low": 40, "info": 50}.get(str(severity), 100)
