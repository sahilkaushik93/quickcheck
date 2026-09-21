"""Write bounded, sanitized Business Impact artifacts for one completed run."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from app.business_impact.models import BusinessImpactOutput


class ArtifactWriteError(RuntimeError):
    """Safe error for controlled artifact persistence failures."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class BusinessImpactArtifactWriter:
    """Persists only output contracts; it never receives source data or secrets."""

    _JSON_FILES = (
        "business_impact_result.json", "trusted_insights.json", "external_citations.json",
        "recommended_actions.json", "business_impact_manifest.json",
    )
    _CSV_FILES = (
        "dq_score_summary.csv", "dq_score_monthly.csv", "dq_score_direction.csv",
        "evidence_summary.csv", "impact_assessments.csv",
    )

    def __init__(self, root_directory: str | Path) -> None:
        self._root = Path(root_directory).resolve()

    def write(self, output: BusinessImpactOutput) -> dict[str, str]:
        """Write fixed-name, compact artifacts beneath a request-scoped directory."""

        run_dir = (self._root / output.run_id).resolve()
        if self._root not in run_dir.parents:
            raise ArtifactWriteError("ARTIFACT_PATH_INVALID", "Artifact run path is outside the configured root.")
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
            payload = output.model_dump(mode="json")
            self._write_json(run_dir / "business_impact_result.json", payload)
            self._write_json(run_dir / "trusted_insights.json", {"claims": payload["insight_claims"]})
            self._write_json(run_dir / "external_citations.json", {"citations": payload["citations"]})
            self._write_json(run_dir / "recommended_actions.json", {"recommendations": payload["recommendations"]})
            self._write_csv(run_dir / "dq_score_summary.csv", (self._score_rows(output),))
            self._write_csv(run_dir / "dq_score_monthly.csv", self._group_rows(output, "month"))
            self._write_csv(run_dir / "dq_score_direction.csv", self._group_rows(output, "direction"))
            self._write_csv(run_dir / "evidence_summary.csv", self._evidence_rows(output))
            self._write_csv(run_dir / "impact_assessments.csv", self._impact_rows(output))
            files = {name: str((run_dir / name).relative_to(self._root)) for name in (*self._JSON_FILES, *self._CSV_FILES)}
            self._write_json(run_dir / "business_impact_manifest.json", {
                "run_id": output.run_id, "status": output.status, "contract_version": output.contract_version,
                "artifacts": files, "contains_raw_source_data": False, "contains_raw_pii": False,
            })
            return files
        except (OSError, TypeError, ValueError) as exc:
            raise ArtifactWriteError("ARTIFACT_WRITE_FAILED", "Business Impact artifacts could not be written.") from exc

    @staticmethod
    def _write_json(path: Path, value: Any) -> None:
        path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")

    @staticmethod
    def _write_csv(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
        values = list(rows)
        keys = sorted({key for row in values for key in row})
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=keys or ["status"])
            writer.writeheader()
            for row in values:
                writer.writerow({key: _compact(row.get(key)) for key in keys})

    @staticmethod
    def _score_rows(output: BusinessImpactOutput) -> Mapping[str, Any]:
        aggregate = output.aggregate_score
        return {
            "run_id": output.run_id, "status": output.status,
            "dq_score": None if aggregate is None else aggregate.score,
            "score_band": None if aggregate is None else aggregate.band,
            "coverage_ratio": None if aggregate is None else aggregate.coverage_ratio,
            "assessment_confidence": None if output.assessment_confidence is None else output.assessment_confidence.value,
        }

    @staticmethod
    def _group_rows(output: BusinessImpactOutput, dimension: str) -> Iterable[Mapping[str, Any]]:
        for item in output.normalized_metric_scores:
            key = item.aggregation_key
            if key is None or key.overall:
                continue
            dimensions = key.dimensions
            if dimension not in dimensions:
                continue
            yield {"run_id": output.run_id, "rule_id": item.rule_id, "metric_name": item.metric_name,
                   "score": item.score, "band": item.band, "availability": item.availability,
                   "dimension": dimensions[dimension], "aggregation_key": key.model_dump_json()}

    @staticmethod
    def _evidence_rows(output: BusinessImpactOutput) -> Iterable[Mapping[str, Any]]:
        for item in output.normalized_evidence:
            fact = item.fact
            yield {"run_id": output.run_id, "evidence_id": item.evidence_id, "fact_id": fact.fact_id,
                   "rule_id": fact.rule_id, "fact_kind": fact.kind, "metric_name": fact.metric_name,
                   "source_run_id": item.provenance.source_run_id,
                   "supporting_fact_count": len(item.supporting_fact_ids)}

    @staticmethod
    def _impact_rows(output: BusinessImpactOutput) -> Iterable[Mapping[str, Any]]:
        for item in output.impact_hypotheses:
            yield {"run_id": output.run_id, "hypothesis_id": item.hypothesis_id,
                   "category": item.category, "statement_type": item.statement_type,
                   "review_disposition": item.review_disposition}


def _compact(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "value"):
        return value.value
    if hasattr(value, "model_dump"):
        return json.dumps(value.model_dump(mode="json"), sort_keys=True)
    if is_dataclass(value):
        return json.dumps(asdict(value), sort_keys=True, default=str)
    return json.dumps(value, sort_keys=True, default=str)
