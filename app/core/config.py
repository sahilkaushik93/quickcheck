"""Application configuration loaded once from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


load_dotenv()


def _positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _csv_values(name: str, default: str) -> tuple[str, ...]:
    return tuple(value.strip() for value in os.getenv(name, default).split(",") if value.strip())


@dataclass(frozen=True)
class Settings:
    app_name: str = "UnDQ Transcript DQ Engine"
    app_version: str = "0.2.0"
    api_prefix: str = "/api/v1/undq/transcript"
    upload_chunk_bytes: int = _positive_int("UNDQ_UPLOAD_CHUNK_BYTES", 1024 * 1024)
    max_csv_upload_bytes: int = _positive_int("UNDQ_MAX_CSV_UPLOAD_BYTES", 5 * 1024**3)
    max_metadata_upload_bytes: int = _positive_int("UNDQ_MAX_METADATA_UPLOAD_BYTES", 50 * 1024**2)
    max_rules_archive_bytes: int = _positive_int("UNDQ_MAX_RULES_ARCHIVE_BYTES", 50 * 1024**2)
    max_rule_file_bytes: int = _positive_int("UNDQ_MAX_RULE_FILE_BYTES", 2 * 1024**2)
    max_rule_count: int = _positive_int("UNDQ_MAX_RULE_COUNT", 500)
    cors_origins: tuple[str, ...] = _csv_values(
        "UNDQ_CORS_ORIGINS", "http://127.0.0.1:8501,http://localhost:8501"
    )
    max_rules_uncompressed_bytes: int = _positive_int(
        "UNDQ_MAX_RULES_UNCOMPRESSED_BYTES", 100 * 1024**2
    )


settings = Settings()
