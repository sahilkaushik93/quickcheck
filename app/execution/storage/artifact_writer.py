"""Atomic optional persistence for compact, redacted Execution output."""

from __future__ import annotations

import hashlib
import csv
import io
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.execution.models import ArtifactReference, ExecutionOutput, ExecutionStatus
from app.execution.rule_ids import canonical_rule_id


class ExecutionArtifactWriteError(RuntimeError):
    """Safe structured artifact persistence failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class ExecutionArtifactWriteResult:
    result: ExecutionOutput
    run_directory: Path
    references: tuple[ArtifactReference, ...]


class ExecutionArtifactWriter:
    """Publish aggregate-only JSON only after full validation succeeds."""

    _RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

    def __init__(
        self,
        output_root: str | Path,
        *,
        overwrite: bool = False,
        maximum_payload_bytes: int = 100 * 1024 * 1024,
    ) -> None:
        if maximum_payload_bytes < 1:
            raise ValueError("maximum_payload_bytes must be positive")
        self._root = Path(output_root).expanduser().resolve()
        self._overwrite = overwrite
        self._maximum_payload_bytes = maximum_payload_bytes

    def write(self, result: ExecutionOutput) -> ExecutionArtifactWriteResult:
        """Atomically write one compact result and manifest directory."""

        if result.status == ExecutionStatus.FAILED.value:
            raise ExecutionArtifactWriteError(
                "FAILED_RESULT_NOT_WRITTEN",
                "Failed execution runs are not persisted as final artifacts.",
            )
        if not self._RUN_ID.fullmatch(result.run_id):
            raise ExecutionArtifactWriteError(
                "INVALID_RUN_ID", "Run ID is unsafe for artifact storage."
            )
        self._assert_safe(result)
        payload = json.dumps(
            result.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(payload) > self._maximum_payload_bytes:
            raise ExecutionArtifactWriteError(
                "ARTIFACT_SIZE_LIMIT_EXCEEDED",
                "Execution result exceeds the configured persistence size limit.",
            )
        artifacts: dict[str, tuple[str, bytes]] = {
            "execution_result.json": ("application/json", payload),
            "rule_overall_summary.csv": ("text/csv", self._metrics_csv(result, grouped=False)),
            "rule_grouped_metrics.csv": ("text/csv", self._metrics_csv(result, grouped=True)),
        }
        artifacts.update(self._rule_artifacts(result))
        references = tuple(
            self._reference("execution_result" if name == "execution_result.json" else "dq_result", name, body, media)
            for name, (media, body) in sorted(artifacts.items())
        )
        persisted = result.model_copy(
            update={
                "artifact_references": [*result.artifact_references, *references]
            }
        )

        self._root.mkdir(parents=True, exist_ok=True)
        destination = self._root / result.run_id
        if destination.exists() and not self._overwrite:
            raise ExecutionArtifactWriteError(
                "RUN_ALREADY_EXISTS",
                "An artifact directory already exists for this execution run.",
            )
        temporary = self._root / f".{result.run_id}.tmp-{uuid.uuid4().hex}"
        manifest = {
            "contract_version": result.contract_version,
            "run_id": result.run_id,
            "status": result.status,
            "source_id": result.source.source_id,
            "source_fingerprint": result.source.fingerprint,
            "understanding_run_id": result.understanding_run_id,
            "execution_plan_id": result.execution_plan_id,
            "registry_fingerprint": result.registry_fingerprint,
            "artifacts": [item.model_dump(mode="json") for item in references],
        }
        try:
            temporary.mkdir(parents=False, exist_ok=False)
            for name, (_, body) in artifacts.items():
                self._atomic_file_write(temporary / name, body)
            self._atomic_file_write(
                temporary / "manifest.json",
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8"),
            )
            if destination.exists():
                shutil.rmtree(destination)
            os.replace(temporary, destination)
            return ExecutionArtifactWriteResult(
                result=persisted,
                run_directory=destination,
                references=references,
            )
        except ExecutionArtifactWriteError:
            raise
        except Exception as exc:
            raise ExecutionArtifactWriteError(
                "ARTIFACT_WRITE_FAILED",
                f"Execution artifacts could not be published ({type(exc).__name__}).",
            ) from exc
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

    @staticmethod
    def _atomic_file_write(path: Path, payload: bytes) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    @staticmethod
    def _reference(kind: str, name: str, payload: bytes, media_type: str) -> ArtifactReference:
        return ArtifactReference(
            artifact_type=kind,
            relative_name=name,
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            media_type=media_type,
        )

    @staticmethod
    def _metrics_csv(result: ExecutionOutput, *, grouped: bool, rule_id: str | None = None) -> bytes:
        stream = io.StringIO(newline="")
        fields = ["canonical_rule_id", "runtime_rule_id", "time_period", "dimensions", "row_count", "eligible_count", "passed_count", "failed_count", "skipped_count", "metric_name", "metric_value", "unit", "outcome", "threshold"]
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for rule in result.rule_results:
            if rule_id is not None and rule.rule_id != rule_id:
                continue
            for group in rule.groups:
                if grouped == group.aggregation_key.overall:
                    continue
                for metric in group.metrics:
                    writer.writerow({
                        "canonical_rule_id": canonical_rule_id(rule.rule_id),
                        "runtime_rule_id": rule.rule_id,
                        "time_period": group.aggregation_key.time_period or "",
                        "dimensions": json.dumps(group.aggregation_key.dimensions, sort_keys=True, separators=(",", ":")),
                        "row_count": group.row_count,
                        "eligible_count": group.eligible_count,
                        "passed_count": group.passed_count,
                        "failed_count": group.failed_count,
                        "skipped_count": group.skipped_count,
                        "metric_name": metric.name,
                        "metric_value": json.dumps(metric.value, sort_keys=True, separators=(",", ":")),
                        "unit": metric.unit or "",
                        "outcome": str(metric.outcome),
                        "threshold": json.dumps(metric.threshold, sort_keys=True, separators=(",", ":")),
                    })
        return stream.getvalue().encode("utf-8")

    @classmethod
    def _rule_artifacts(cls, result: ExecutionOutput) -> dict[str, tuple[str, bytes]]:
        prefixes = {
            "speaker_tag_validation": "speaker_tag_validation",
            "topic_drift": "topic_drift",
            "transcript_duration_vs_size": "transcript_duration_vs_size",
            "fill_rate": "fill_rate",
            "non_english_mistranscription": "non_english_mistranscription",
            "privacy_detection": "privacy_detection",
            "spelling_validation": "spelling_validation",
        }
        artifacts: dict[str, tuple[str, bytes]] = {}
        for rule in result.rule_results:
            canonical = canonical_rule_id(rule.rule_id)
            prefix = prefixes[canonical]
            summary = json.dumps(rule.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
            artifacts[f"{prefix}_summary.json"] = ("application/json", summary)
            artifacts[f"{prefix}_detail.csv"] = ("text/csv", cls._metrics_csv(result, grouped=False, rule_id=rule.rule_id))
            artifacts[f"{prefix}_monthly.csv"] = ("text/csv", cls._metrics_csv(result, grouped=True, rule_id=rule.rule_id))
            if canonical == "speaker_tag_validation":
                artifacts["speaker_tag_validation_direction_monthly.csv"] = ("text/csv", cls._metrics_csv(result, grouped=True, rule_id=rule.rule_id))
            if canonical == "topic_drift":
                artifacts["topic_drift_pairwise.csv"] = ("text/csv", cls._metrics_csv(result, grouped=True, rule_id=rule.rule_id))
        return artifacts

    @staticmethod
    def _assert_safe(result: ExecutionOutput) -> None:
        for collection in result.evidence:
            for item in collection.items:
                if not item.redacted:
                    raise ExecutionArtifactWriteError(
                        "UNREDACTED_EVIDENCE",
                        "Only redacted execution evidence may be persisted.",
                    )
                if not item.row_fingerprint.startswith("sha256:"):
                    raise ExecutionArtifactWriteError(
                        "INVALID_EVIDENCE_FINGERPRINT",
                        "Evidence row references must be SHA-256 fingerprints.",
                    )
                forbidden = re.compile(
                    r"(?:value|text|transcript|match|email|phone|card|account|customer|member|token|password|secret|identifier|raw)",
                    re.IGNORECASE,
                )
                if any(forbidden.search(key) for key in item.metadata):
                    raise ExecutionArtifactWriteError(
                        "UNSAFE_EVIDENCE_METADATA",
                        "Evidence metadata contains a forbidden field.",
                    )


__all__ = [
    "ExecutionArtifactWriteError",
    "ExecutionArtifactWriteResult",
    "ExecutionArtifactWriter",
]
