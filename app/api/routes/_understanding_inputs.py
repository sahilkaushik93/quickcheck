"""Shared API helpers for constructing a request-scoped Understanding source."""

from __future__ import annotations

from fastapi import HTTPException, UploadFile, status

from app.api.routes._uploads import StagedUnderstandingInputs
from app.services.understanding import UnderstandingSourceRequest
from app.understanding.models import (
    InputAssetProvenance,
    RuleRegistryProvenance,
    UnderstandingInputProvenance,
)


def select_sample_csv(
    sample_csv: UploadFile | None,
    legacy_file: UploadFile | None,
) -> UploadFile:
    """Select exactly one CSV field while retaining the legacy ``file`` alias."""

    if (sample_csv is None) == (legacy_file is None):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "CSV_INPUT_INVALID",
                "message": "Provide exactly one of sample_csv or the deprecated file field.",
            },
        )
    selected = sample_csv if sample_csv is not None else legacy_file
    assert selected is not None
    return selected


def build_source_request(
    staged: StagedUnderstandingInputs,
    *,
    run_id: str | None = None,
    dictionary_version: str | None = None,
    persist_artifacts: bool,
) -> UnderstandingSourceRequest:
    """Translate safe staged inputs into the service-layer request contract."""

    provenance = UnderstandingInputProvenance(
        sample_csv=InputAssetProvenance(
            filename=staged.sample_csv.original_name,
            size_bytes=staged.sample_csv.size_bytes,
            sha256=staged.sample_csv.sha256,
        ),
        metadata_dictionary=InputAssetProvenance(
            filename=staged.metadata_dictionary.original_name,
            size_bytes=staged.metadata_dictionary.size_bytes,
            sha256=staged.metadata_dictionary.sha256,
        ),
        rule_registry=RuleRegistryProvenance(
            file_count=len(staged.rule_names),
            filenames=list(staged.rule_names),
            upload_fingerprint=staged.raw_rules_fingerprint,
        ),
    )
    return UnderstandingSourceRequest(
        csv_path=staged.sample_csv.path,
        original_filename=staged.sample_csv.original_name,
        source_id=f"upload:{staged.sample_csv.sha256[:24]}",
        metadata_dictionary_path=staged.metadata_dictionary.path,
        rules_directory=staged.rules_directory,
        input_provenance=provenance,
        run_id=run_id,
        dictionary_version=dictionary_version,
        persist_artifacts=persist_artifacts,
    )
