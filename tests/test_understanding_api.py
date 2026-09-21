from __future__ import annotations

import io
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "data" / "input" / "lumi_sample.csv"
DICTIONARY = ROOT / "data" / "input" / "lumi_metadata_dictionary.xlsx"
RULES = ROOT / "config" / "understanding" / "rules"


def _base_files() -> list[tuple[str, tuple[str, bytes, str]]]:
    return [
        ("sample_csv", (SAMPLE.name, SAMPLE.read_bytes(), "text/csv")),
        (
            "metadata_dictionary",
            (DICTIONARY.name, DICTIONARY.read_bytes(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ),
    ]


def _loose_rule_files() -> list[tuple[str, tuple[str, bytes, str]]]:
    return [
        ("rule_files", (path.name, path.read_bytes(), "application/json"))
        for path in sorted(RULES.glob("*.json"))
    ]


def _rules_zip() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(RULES.glob("*.json")):
            archive.writestr(f"registry/{path.name}", path.read_bytes())
    return output.getvalue()


def test_understanding_accepts_csv_dictionary_and_loose_rules() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/undq/transcript/understanding",
            files=[*_base_files(), *_loose_rule_files()],
            data={"persist_artifacts": "false", "llm_enabled": "false"},
        )
    assert response.status_code == 200, response.text
    result = response.json()["understanding"]
    assert result["Profile"]["details"]["row_count"] == 8
    assert set(result["Rules"]["details"]["selected_rules"]) == {
        "fill_rate", "topic_distribution", "agent_speaker_tag_validation",
        "non_english_spelling", "mistranslated_rate", "pii_detection",
        "speech_per_duration_rate",
    }
    assert result["input_provenance"]["rule_registry"]["file_count"] == 7
    assert result["input_provenance"]["rule_registry"]["canonical_fingerprint"]


def test_understanding_accepts_nested_rules_zip() -> None:
    files = [*_base_files(), ("rules_archive", ("rules.zip", _rules_zip(), "application/zip"))]
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/undq/transcript/understanding/rules",
            files=files,
            data={"dictionary_version": "test-v1"},
        )
    assert response.status_code == 200, response.text
    assert len(response.json()["details"]["selected_rules"]) == 7


def test_engine_consumes_uploaded_bytes() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/undq/transcript/engine",
            files=[*_base_files(), *_loose_rule_files()],
            data={"persist_artifacts": "false"},
        )
    assert response.status_code == 200, response.text
    assert response.json()["understanding"]["Profile"]["details"]["row_count"] == 8


def test_exactly_one_rule_upload_mode_is_required() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/undq/transcript/understanding",
            files=_base_files(),
            data={"persist_artifacts": "false"},
        )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "RULE_INPUT_MODE_INVALID"
