"""Safe, request-scoped staging for Understanding Layer multipart inputs."""

from __future__ import annotations

import hashlib
import stat
import tempfile
import zipfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import AsyncIterator, Iterable

from fastapi import HTTPException, UploadFile, status

from app.core.config import settings


_METADATA_SUFFIXES = {".csv", ".xlsx", ".xls", ".xlsm"}


@dataclass(frozen=True, slots=True)
class StagedFile:
    path: Path
    original_name: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class StagedUnderstandingInputs:
    sample_csv: StagedFile
    metadata_dictionary: StagedFile
    rules_directory: Path
    rule_names: tuple[str, ...]
    raw_rules_fingerprint: str


def validate_csv(file: UploadFile) -> None:
    """Backward-compatible CSV validation used by older routes."""

    _require_suffix(file, {".csv"}, "Sample data must be a CSV file.")


@asynccontextmanager
async def stage_understanding_inputs(
    *,
    sample_csv: UploadFile,
    metadata_dictionary: UploadFile,
    rule_files: Iterable[UploadFile] | None = None,
    rules_archive: UploadFile | None = None,
) -> AsyncIterator[StagedUnderstandingInputs]:
    """Stage all request inputs and delete them when processing completes."""

    supplied_rules = tuple(rule_files or ())
    if bool(supplied_rules) == bool(rules_archive):
        raise _http_error(
            status.HTTP_400_BAD_REQUEST,
            "RULE_INPUT_MODE_INVALID",
            "Provide exactly one of rule_files or rules_archive.",
        )
    _require_suffix(sample_csv, {".csv"}, "Sample data must be a CSV file.")
    _require_suffix(
        metadata_dictionary,
        _METADATA_SUFFIXES,
        "Metadata dictionary must be CSV, XLSX, XLS or XLSM.",
    )

    uploads = [sample_csv, metadata_dictionary, *supplied_rules]
    if rules_archive is not None:
        uploads.append(rules_archive)
    with tempfile.TemporaryDirectory(prefix="undq-understanding-") as temporary_name:
        root = Path(temporary_name)
        rules_directory = root / "rules"
        rules_directory.mkdir()
        try:
            staged_csv = await _stage_upload(
                sample_csv,
                root / "sample.csv",
                settings.max_csv_upload_bytes,
                "CSV_UPLOAD_TOO_LARGE",
            )
            metadata_suffix = Path(metadata_dictionary.filename or "metadata.csv").suffix.casefold()
            staged_metadata = await _stage_upload(
                metadata_dictionary,
                root / f"metadata{metadata_suffix}",
                settings.max_metadata_upload_bytes,
                "METADATA_UPLOAD_TOO_LARGE",
            )
            if rules_archive is not None:
                _require_suffix(rules_archive, {".zip"}, "Rules archive must be a ZIP file.")
                staged_archive = await _stage_upload(
                    rules_archive,
                    root / "rules.zip",
                    settings.max_rules_archive_bytes,
                    "RULES_ARCHIVE_TOO_LARGE",
                )
                rule_names = _extract_rules_archive(staged_archive.path, rules_directory)
            else:
                rule_names = await _stage_rule_files(supplied_rules, rules_directory)

            yield StagedUnderstandingInputs(
                sample_csv=staged_csv,
                metadata_dictionary=staged_metadata,
                rules_directory=rules_directory,
                rule_names=rule_names,
                raw_rules_fingerprint=_rules_fingerprint(rules_directory, rule_names),
            )
        finally:
            for upload in uploads:
                await upload.close()


async def _stage_rule_files(files: tuple[UploadFile, ...], destination: Path) -> tuple[str, ...]:
    if not files:
        raise _http_error(400, "RULE_REGISTRY_EMPTY", "At least one JSON rule file is required.")
    if len(files) > settings.max_rule_count:
        raise _http_error(413, "TOO_MANY_RULE_FILES", "Uploaded rule count exceeds the configured limit.")
    names: list[str] = []
    seen: set[str] = set()
    for index, upload in enumerate(files):
        _require_suffix(upload, {".json"}, "Every rule file must be JSON.")
        name = _safe_filename(upload.filename, fallback=f"rule-{index + 1}.json")
        if name.casefold() in seen:
            raise _http_error(400, "DUPLICATE_RULE_FILENAME", f"Duplicate rule filename: {name!r}.")
        seen.add(name.casefold())
        await _stage_upload(
            upload,
            destination / name,
            settings.max_rule_file_bytes,
            "RULE_FILE_TOO_LARGE",
        )
        names.append(name)
    return tuple(sorted(names, key=str.casefold))


def _extract_rules_archive(archive_path: Path, destination: Path) -> tuple[str, ...]:
    try:
        archive = zipfile.ZipFile(archive_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise _http_error(400, "INVALID_RULES_ARCHIVE", "Rules archive is not a valid ZIP file.") from exc

    names: list[str] = []
    seen: set[str] = set()
    total_uncompressed = 0
    try:
        json_items = [
            item
            for item in archive.infolist()
            if not item.is_dir() and PurePosixPath(item.filename).suffix.casefold() == ".json"
        ]
        if not json_items:
            raise _http_error(400, "RULE_REGISTRY_EMPTY", "Rules archive contains no JSON rule files.")
        if len(json_items) > settings.max_rule_count:
            raise _http_error(413, "TOO_MANY_RULE_FILES", "Rules archive exceeds the configured rule count.")

        for item in json_items:
            member = PurePosixPath(item.filename)
            if member.is_absolute() or ".." in member.parts:
                raise _http_error(400, "UNSAFE_RULE_ARCHIVE_PATH", "Rules archive contains an unsafe path.")
            mode = item.external_attr >> 16
            if mode and stat.S_ISLNK(mode):
                raise _http_error(400, "UNSAFE_RULE_ARCHIVE_LINK", "Rules archive cannot contain links.")
            if item.file_size > settings.max_rule_file_bytes:
                raise _http_error(413, "RULE_FILE_TOO_LARGE", f"Rule file {member.name!r} is too large.")
            total_uncompressed += item.file_size
            if total_uncompressed > settings.max_rules_uncompressed_bytes:
                raise _http_error(413, "RULES_ARCHIVE_EXPANDED_TOO_LARGE", "Expanded rules exceed the configured limit.")

            relative = Path(*member.parts)
            key = relative.as_posix().casefold()
            if key in seen:
                raise _http_error(400, "DUPLICATE_RULE_FILENAME", f"Duplicate rule path: {relative.as_posix()!r}.")
            seen.add(key)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as source, target.open("xb") as output:
                copied = 0
                while chunk := source.read(settings.upload_chunk_bytes):
                    copied += len(chunk)
                    if copied > settings.max_rule_file_bytes:
                        raise _http_error(413, "RULE_FILE_TOO_LARGE", f"Rule file {member.name!r} is too large.")
                    output.write(chunk)
            names.append(relative.as_posix())
    finally:
        archive.close()
    return tuple(sorted(names, key=str.casefold))


async def _stage_upload(
    upload: UploadFile,
    destination: Path,
    maximum_bytes: int,
    error_code: str,
) -> StagedFile:
    digest = hashlib.sha256()
    total = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as output:
        while chunk := await upload.read(settings.upload_chunk_bytes):
            total += len(chunk)
            if total > maximum_bytes:
                raise _http_error(413, error_code, "Uploaded file exceeds the configured size limit.")
            digest.update(chunk)
            output.write(chunk)
    if total == 0:
        raise _http_error(400, "EMPTY_UPLOAD", f"Uploaded file {upload.filename or '<unnamed>'!r} is empty.")
    return StagedFile(
        path=destination,
        original_name=_safe_filename(upload.filename, fallback=destination.name),
        size_bytes=total,
        sha256=digest.hexdigest(),
    )


def _rules_fingerprint(directory: Path, names: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        with (directory / Path(name)).open("rb") as handle:
            while chunk := handle.read(settings.upload_chunk_bytes):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _require_suffix(upload: UploadFile, allowed: set[str], message: str) -> None:
    if Path(upload.filename or "").suffix.casefold() not in allowed:
        raise _http_error(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "UNSUPPORTED_FILE_TYPE", message)


def _safe_filename(value: str | None, *, fallback: str) -> str:
    name = Path(value or fallback).name.strip()
    return fallback if not name or name in {".", ".."} else name


def _http_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})
