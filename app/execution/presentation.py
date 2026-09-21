"""Consumer-oriented projection of canonical Execution results."""

from __future__ import annotations

from typing import Any

from app.execution.models import ExecutionOutput, GroupedRuleMetrics, RuleMetric
from app.execution.rule_ids import canonical_rule_id


_PRIMARY_METRICS = {
    "fill_rate": "fill_rate",
    "topic_distribution": "dominant_topic_share",
    "agent_speaker_tag_validation": "invalid_speaker_tag_rate",
    "non_english_spelling": "unknown_token_rate",
    "mistranslated_rate": "mistranslated_rate",
    "pii_detection": "pii_record_rate",
    "speech_per_duration_rate": "speech_words_per_minute",
}


def build_observed_results(output: ExecutionOutput) -> dict[str, Any]:
    """Build an explicit summary while preserving the canonical output separately."""

    checks: list[dict[str, Any]] = []
    for result in output.rule_results:
        overall = next(
            (group for group in result.groups if group.aggregation_key.overall),
            result.groups[0] if result.groups else None,
        )
        primary_name = _PRIMARY_METRICS.get(str(result.check_name))
        primary = _metric(overall, primary_name) if overall and primary_name else None
        checks.append(
            {
                "check_name": str(result.check_name),
                "rule_id": result.rule_id,
                "canonical_rule_id": canonical_rule_id(result.rule_id),
                "display_name": canonical_rule_id(result.rule_id).replace("_", " ").title(),
                "status": str(result.status),
                "target_columns": result.target_columns,
                "population": _counts(overall),
                "primary_metric": _metric_dict(primary),
                "overall_metrics": {
                    metric.name: metric.model_dump(mode="json")
                    for metric in (overall.metrics if overall else [])
                },
                "grouped_result_count": max(0, len(result.groups) - (1 if overall else 0)),
                "warnings": result.warnings,
                "errors": result.errors,
            }
        )
    return {
        "run_scope": {
            "rows_read": output.summary.rows_read,
            "rows_after_filters": output.summary.rows_after_filters,
            "chunks_processed": output.summary.chunks_processed,
            "aggregation": output.aggregation.model_dump(mode="json"),
        },
        "check_summary": checks,
        "grouped_results": [
            {
                "check_name": str(result.check_name),
                "canonical_rule_id": canonical_rule_id(result.rule_id),
                "groups": [group.model_dump(mode="json") for group in result.groups if not group.aggregation_key.overall],
            }
            for result in output.rule_results
        ],
        "evidence_summary": {
            "observed": output.summary.evidence_observed,
            "retained": output.summary.evidence_retained,
            "privacy": "fingerprints and failure categories only; no source rows or raw matches",
        },
    }


def _metric(group: GroupedRuleMetrics, name: str) -> RuleMetric | None:
    return next((metric for metric in group.metrics if metric.name == name), None)


def _metric_dict(metric: RuleMetric | None) -> dict[str, Any] | None:
    return metric.model_dump(mode="json") if metric else None


def _counts(group: GroupedRuleMetrics | None) -> dict[str, int]:
    if group is None:
        return {"rows": 0, "eligible": 0, "passed": 0, "failed": 0, "skipped": 0}
    return {
        "rows": group.row_count,
        "eligible": group.eligible_count,
        "passed": group.passed_count,
        "failed": group.failed_count,
        "skipped": group.skipped_count,
    }


__all__ = ["build_observed_results"]
