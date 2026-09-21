"""Multipart parsing and request-scoped CSV staging for Execution APIs."""

from __future__ import annotations

import hashlib
import json
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, TypeVar

from fastapi import HTTPException, UploadFile, status
from pydantic import BaseModel, ValidationError

from app.core.config import settings
from app.execution.models import AggregationRequest, ExecutionOptions
from app.models.responses import UnderstandingResult
from app.services.execution import ExecutionSourceRequest


ModelT = TypeVar("ModelT", bound=BaseModel)


@asynccontextmanager
async def stage_execution_source(
    sample_csv: UploadFile,
    *,
    source_id: str | None = None,
    source_version: str | None = None,
) -> AsyncIterator[ExecutionSourceRequest]:
    """Stream an uploaded CSV to request-scoped disk and calculate SHA-256."""

    if Path(sample_csv.filename or "").suffix.casefold() != ".csv":
        raise _error(415, "UNSUPPORTED_FILE_TYPE", "Execution source must be a CSV file.")
    with tempfile.TemporaryDirectory(prefix="undq-execution-") as temporary:
        path = Path(temporary) / "sample.csv"
        digest = hashlib.sha256()
        total = 0
        try:
            with path.open("xb") as handle:
                while chunk := await sample_csv.read(settings.upload_chunk_bytes):
                    total += len(chunk)
                    if total > settings.max_csv_upload_bytes:
                        raise _error(
                            413,
                            "CSV_UPLOAD_TOO_LARGE",
                            "Uploaded CSV exceeds the configured size limit.",
                        )
                    digest.update(chunk)
                    handle.write(chunk)
            if total == 0:
                raise _error(400, "EMPTY_UPLOAD", "Uploaded CSV is empty.")
            fingerprint = digest.hexdigest()
            yield ExecutionSourceRequest(
                csv_path=path,
                original_filename=Path(sample_csv.filename or "sample.csv").name,
                source_id=source_id or f"upload:{fingerprint[:24]}",
                size_bytes=total,
                sha256=fingerprint,
                source_version=source_version,
            )
        finally:
            await sample_csv.close()


def parse_understanding(value: str) -> UnderstandingResult:
    return _parse_model(value, UnderstandingResult, "understanding_json")


def parse_aggregation(value: str | None) -> AggregationRequest:
    return _parse_model(value or "{}", AggregationRequest, "aggregation_json")


def parse_execution_options(value: str | None) -> ExecutionOptions | None:
    if value is None or not value.strip():
        return None
    return _parse_model(value, ExecutionOptions, "execution_options_json")


def parse_column_overrides(value: str | None) -> dict[str, str]:
    if value is None or not value.strip():
        return {}
    try:
        document = json.loads(value)
    except json.JSONDecodeError as exc:
        raise _error(400, "INVALID_COLUMN_OVERRIDES", "column_overrides_json is invalid JSON.") from exc
    if not isinstance(document, dict) or not all(
        isinstance(key, str) and key.strip() and isinstance(item, str) and item.strip()
        for key, item in document.items()
    ):
        raise _error(
            400,
            "INVALID_COLUMN_OVERRIDES",
            "column_overrides_json must be an object of non-empty string mappings.",
        )
    return {key.strip(): item.strip() for key, item in sorted(document.items())}


def parse_selected_rule_ids(value: str | None) -> list[str] | None:
    """Parse an optional JSON array of canonical or legacy rule identifiers."""

    if value is None or not value.strip():
        return None
    try:
        document = json.loads(value)
    except json.JSONDecodeError as exc:
        raise _error(400, "INVALID_SELECTED_RULES", "selected_rule_ids_json is invalid JSON.") from exc
    if not isinstance(document, list) or not document or not all(
        isinstance(item, str) and item.strip() for item in document
    ):
        raise _error(
            400,
            "INVALID_SELECTED_RULES",
            "selected_rule_ids_json must be a non-empty JSON array of strings.",
        )
    return list(dict.fromkeys(item.strip() for item in document))


def _parse_model(value: str, model: type[ModelT], field: str) -> ModelT:
    try:
        document = json.loads(value)
        return model.model_validate(document)
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        raise _error(
            status.HTTP_400_BAD_REQUEST,
            "INVALID_MULTIPART_JSON",
            f"{field} does not match the required JSON contract.",
        ) from exc


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})
