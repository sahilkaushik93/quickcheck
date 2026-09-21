"""Safe listing and download of final aggregate DQ artifacts."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse

from app.core.config import settings


router = APIRouter(prefix=f"{settings.api_prefix}/artifacts", tags=["artifacts"])
_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_IMPACT_FILES: dict[str, str] = {
    "business_impact_result.json": "application/json",
    "dq_score_summary.csv": "text/csv; charset=utf-8",
    "dq_score_monthly.csv": "text/csv; charset=utf-8",
    "dq_score_direction.csv": "text/csv; charset=utf-8",
    "evidence_summary.csv": "text/csv; charset=utf-8",
    "impact_assessments.csv": "text/csv; charset=utf-8",
    "trusted_insights.json": "application/json",
    "external_citations.json": "application/json",
    "recommended_actions.json": "application/json",
    "business_impact_manifest.json": "application/json",
}


def _root() -> Path:
    repository = Path(__file__).resolve().parents[3]
    return Path(os.getenv("UNDQ_EXECUTION_OUTPUT_ROOT", repository / "data/execution_layer")).resolve()


def _impact_root() -> Path:
    repository = Path(__file__).resolve().parents[3]
    return Path(os.getenv("UNDQ_BUSINESS_IMPACT_OUTPUT_ROOT", repository / "data/business_impact")).resolve()


def _manifest(run_id: str) -> tuple[Path, dict[str, object]]:
    if not _SAFE.fullmatch(run_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail={"code": "INVALID_RUN_ID", "message": "Invalid artifact run identifier."})
    directory = (_root() / run_id).resolve()
    if directory.parent != _root() or not directory.is_dir():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail={"code": "ARTIFACT_RUN_NOT_FOUND", "message": "Artifact run does not exist or has expired."})
    path = directory / "manifest.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail={"code": "ARTIFACT_MANIFEST_NOT_FOUND", "message": "Artifact manifest is unavailable."}) from exc
    return directory, document


@router.get("/{run_id}")
def list_artifacts(run_id: str) -> dict[str, object]:
    _, manifest = _manifest(run_id)
    return {"run_id": run_id, "artifacts": manifest.get("artifacts", [])}


@router.get("/{run_id}/{filename}")
def download_artifact(run_id: str, filename: str) -> FileResponse:
    if not _SAFE.fullmatch(filename):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail={"code": "INVALID_ARTIFACT_NAME", "message": "Invalid artifact filename."})
    directory, manifest = _manifest(run_id)
    allowed = {
        str(item.get("relative_name")): str(item.get("media_type") or "application/octet-stream")
        for item in manifest.get("artifacts", [])
        if isinstance(item, dict)
    }
    if filename not in allowed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail={"code": "ARTIFACT_NOT_FOUND", "message": "Artifact is not listed for this run."})
    path = (directory / filename).resolve()
    if path.parent != directory or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail={"code": "ARTIFACT_NOT_FOUND", "message": "Artifact is unavailable."})
    return FileResponse(path, media_type=allowed[filename], filename=filename)


def _impact_manifest(run_id: str) -> tuple[Path, dict[str, object]]:
    """Resolve only a request-scoped Business Impact manifest beneath its own root."""

    if not _SAFE.fullmatch(run_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail={"code": "INVALID_RUN_ID", "message": "Invalid artifact run identifier."})
    root = _impact_root()
    directory = (root / run_id).resolve()
    if directory.parent != root or not directory.is_dir():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail={"code": "BUSINESS_IMPACT_ARTIFACT_RUN_NOT_FOUND", "message": "Business Impact artifacts do not exist or have expired."})
    try:
        max_age = int(os.getenv("UNDQ_BUSINESS_IMPACT_ARTIFACT_TTL_SECONDS", "604800"))
    except ValueError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail={"code": "BUSINESS_IMPACT_ARTIFACT_POLICY_INVALID", "message": "Business Impact artifact expiry policy is invalid."}) from exc
    if max_age > 0 and time.time() - directory.stat().st_mtime > max_age:
        raise HTTPException(status.HTTP_410_GONE, detail={"code": "BUSINESS_IMPACT_ARTIFACT_EXPIRED", "message": "Business Impact artifacts have expired."})
    path = directory / "business_impact_manifest.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail={"code": "BUSINESS_IMPACT_ARTIFACT_MANIFEST_NOT_FOUND", "message": "Business Impact artifact manifest is unavailable."}) from exc
    artifacts = document.get("artifacts")
    if not isinstance(artifacts, dict) or any(name not in _IMPACT_FILES or not isinstance(value, str) for name, value in artifacts.items()):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail={"code": "BUSINESS_IMPACT_ARTIFACT_MANIFEST_INVALID", "message": "Business Impact artifact manifest is invalid."})
    return directory, document


@router.get("/business-impact/{run_id}")
def list_business_impact_artifacts(run_id: str) -> dict[str, object]:
    """List only manifest-approved compact Business Impact artifacts."""

    _, manifest = _impact_manifest(run_id)
    return {"run_id": run_id, "artifacts": sorted(manifest["artifacts"]), "status": manifest.get("status")}


@router.get("/business-impact/{run_id}/{filename}")
def download_business_impact_artifact(run_id: str, filename: str) -> FileResponse:
    """Download one allowlisted Business Impact output without arbitrary file access."""

    if filename not in _IMPACT_FILES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail={"code": "BUSINESS_IMPACT_ARTIFACT_NOT_FOUND", "message": "Artifact is not permitted for download."})
    directory, manifest = _impact_manifest(run_id)
    relative_name = manifest["artifacts"].get(filename)
    if not isinstance(relative_name, str) or Path(relative_name).name != filename:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail={"code": "BUSINESS_IMPACT_ARTIFACT_NOT_FOUND", "message": "Artifact is not listed for this run."})
    path = (directory / filename).resolve()
    if path.parent != directory or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail={"code": "BUSINESS_IMPACT_ARTIFACT_NOT_FOUND", "message": "Artifact is unavailable."})
    return FileResponse(path, media_type=_IMPACT_FILES[filename], filename=filename)


__all__ = ["router"]
