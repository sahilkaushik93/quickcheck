"""Atomic writer for compact, redacted Understanding Layer artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.understanding.models import UnderstandingOutput


class ArtifactWriteError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ArtifactWriteResult:
    result: UnderstandingOutput
    run_directory: Path
    references: dict[str, str]


class UnderstandingArtifactWriter:
    """Publish a complete run directory only after every artifact is valid."""

    _RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

    def __init__(self, output_root: str | Path, *, overwrite: bool = False) -> None:
        self._root = Path(output_root).expanduser().resolve()
        self._overwrite = overwrite

    def write(self, result: UnderstandingOutput) -> ArtifactWriteResult:
        if result.status == "failed":
            raise ArtifactWriteError("FAILED_RESULT_NOT_WRITTEN", "Failed runs are not persisted as final artifacts.")
        if not self._RUN_ID.fullmatch(result.run_id):
            raise ArtifactWriteError("INVALID_RUN_ID", "Run ID is unsafe for artifact storage.")
        self._assert_safe(result)
        self._root.mkdir(parents=True, exist_ok=True)
        destination = self._root / result.run_id
        if destination.exists() and not self._overwrite:
            raise ArtifactWriteError("RUN_ALREADY_EXISTS", "An artifact directory already exists for this run ID.")
        temporary = self._root / f".{result.run_id}.tmp-{uuid.uuid4().hex}"
        references = {
            "profile": "profile.json",
            "metadata_context": "metadata_context.json",
            "domains": "domains.json",
            "relationships": "relationships.json",
            "dq_signals": "dq_signals.json",
            "applicable_rules": "applicable_rules.json",
            "execution_plan": "execution_plan.json",
            "llm_insights": "llm_insights.json",
            "understanding_result": "understanding_result.json",
        }
        persisted = result.model_copy(update={"artifact_references": references})
        documents: dict[str, Any] = {
            "profile.json": persisted.profile.model_dump(mode="json"),
            "metadata_context.json": persisted.metadata_knowledge_base.model_dump(mode="json"),
            "domains.json": [item.model_dump(mode="json") for item in persisted.domains],
            "relationships.json": [item.model_dump(mode="json") for item in persisted.relationships],
            "dq_signals.json": [item.model_dump(mode="json") for item in persisted.dq_signals],
            "applicable_rules.json": [item.model_dump(mode="json") for item in persisted.rule_applicability],
            "execution_plan.json": persisted.execution_plan.model_dump(mode="json"),
            "llm_insights.json": [item.model_dump(mode="json") for item in persisted.llm_insights],
            "understanding_result.json": persisted.model_dump(mode="json"),
        }
        try:
            temporary.mkdir(parents=False, exist_ok=False)
            hashes: dict[str, str] = {}
            for filename, document in documents.items():
                payload = json.dumps(document, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                self._atomic_file_write(temporary / filename, payload)
                hashes[filename] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            manifest = {
                "contract_version": persisted.contract_version,
                "run_id": persisted.run_id,
                "status": persisted.status,
                "source_id": persisted.source.source_id,
                "input_provenance": (
                    persisted.input_provenance.model_dump(mode="json")
                    if persisted.input_provenance is not None
                    else None
                ),
                "artifacts": references,
                "sha256": hashes,
            }
            self._atomic_file_write(
                temporary / "manifest.json",
                json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
            )
            if destination.exists():
                shutil.rmtree(destination)
            os.replace(temporary, destination)
            return ArtifactWriteResult(result=persisted, run_directory=destination, references=references)
        except ArtifactWriteError:
            raise
        except Exception as exc:
            raise ArtifactWriteError(
                "ARTIFACT_WRITE_FAILED",
                f"Understanding artifacts could not be published ({type(exc).__name__}).",
            ) from exc
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

    @staticmethod
    def _atomic_file_write(path: Path, payload: str) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    @staticmethod
    def _assert_safe(result: UnderstandingOutput) -> None:
        for column in result.profile.columns:
            if any(not item.redacted for item in column.top_values):
                raise ArtifactWriteError(
                    "UNREDACTED_PROFILE_VALUE",
                    "Profile contains an unredacted top value and cannot be persisted.",
                )
            if column.safe_samples.values and column.safe_samples.strategy not in {"hashed", "synthetic"}:
                raise ArtifactWriteError(
                    "UNSAFE_PROFILE_SAMPLE",
                    "Only hashed or synthetic profile samples may be persisted.",
                )
        if any(not evidence.redacted for signal in result.dq_signals for evidence in signal.evidence):
            raise ArtifactWriteError(
                "UNREDACTED_SIGNAL_EVIDENCE",
                "Signal evidence must be redacted before persistence.",
            )
