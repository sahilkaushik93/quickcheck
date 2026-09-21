"""Validate the Understanding-to-Execution handoff without reprocessing data."""

from __future__ import annotations

import re

from app.business_impact.models import BusinessImpactRunRequest


class HandoffValidationError(ValueError):
    """Safe handoff failure containing no source values or sensitive metadata."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


_SHA256 = re.compile(r"^[a-f0-9]{64}$")


def validate_handoff(request: BusinessImpactRunRequest) -> None:
    """Ensure a Business Impact run consumes one coherent completed DQ run.

    The Pydantic request model validates the run linkage.  This node adds
    explicit source and plan provenance checks and deliberately has no source
    adapter, file path, rule registry mutation or connector dependency.
    """

    execution = request.execution
    understanding = request.understanding
    if understanding.run_id != execution.understanding_run_id:
        raise HandoffValidationError("UNDERSTANDING_EXECUTION_RUN_MISMATCH", "Execution does not belong to the supplied Understanding run.")
    if not execution.execution_plan_id:
        raise HandoffValidationError("EXECUTION_PLAN_MISSING", "Execution output has no plan provenance.")
    if not _SHA256.fullmatch(execution.source.fingerprint):
        raise HandoffValidationError("EXECUTION_SOURCE_FINGERPRINT_INVALID", "Execution source provenance is invalid.")
    if execution.registry_fingerprint and not _SHA256.fullmatch(execution.registry_fingerprint):
        raise HandoffValidationError("RULE_REGISTRY_FINGERPRINT_INVALID", "Execution rule registry provenance is invalid.")
    if not execution.rule_results and execution.summary.rules_executed:
        raise HandoffValidationError("EXECUTION_RESULTS_MISSING", "Execution summary and result provenance are inconsistent.")
    if execution.summary.evidence_retained > execution.summary.evidence_observed:
        raise HandoffValidationError("EXECUTION_EVIDENCE_COUNTS_INVALID", "Execution evidence provenance is inconsistent.")


__all__ = ["HandoffValidationError", "validate_handoff"]
