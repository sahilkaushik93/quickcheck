from __future__ import annotations

import ast
import json
import io
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app


ROOT = Path(__file__).resolve().parents[1]


def _engine_files() -> list[tuple[str, tuple[str, bytes, str]]]:
    rules = io.BytesIO()
    with zipfile.ZipFile(rules, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted((ROOT / "config/understanding/rules").glob("*.json")):
            archive.writestr(f"rules/{path.name}", path.read_bytes())
    return [
        ("sample_csv", ("sample.csv", (ROOT / "data/input/lumi_sample.csv").read_bytes(), "text/csv")),
        ("metadata_dictionary", ("dictionary.xlsx", (ROOT / "data/input/lumi_metadata_dictionary.xlsx").read_bytes(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
        ("rules_archive", ("rules.zip", rules.getvalue(), "application/zip")),
    ]


def test_health_and_readiness_report_seven_handlers() -> None:
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        response = client.get("/ready")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["checks"]["registered_rule_count"] == 7
    assert body["checks"]["business_impact"] == "deferred"


def test_business_impact_is_explicitly_deferred() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/undq/transcript/business-impact")
    assert response.status_code == 200
    assert response.json()["status"] == "disabled"


def test_artifact_path_traversal_is_rejected() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/undq/transcript/artifacts/not%2Fsafe")
    assert response.status_code in {400, 404}


def test_streamlit_client_does_not_import_backend_internals() -> None:
    tree = ast.parse((ROOT / "ui/api_client.py").read_text(encoding="utf-8"))
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import): imports.extend(alias.name for alias in node.names)
        if isinstance(node, ast.ImportFrom) and node.module: imports.append(node.module)
    assert not any(name.startswith("app.") for name in imports)


def test_all_seven_rules_execute_with_grouped_metrics(monkeypatch) -> None:
    monkeypatch.setenv("UNDQ_EVIDENCE_HASH_SALT", "synthetic-test-only-salt")
    aggregation = {
        "time": {"grain": "month", "column": "intrct_ts"},
        "dimensions": [{"column": "intrct_drct", "alias": "interaction_direction"}],
        "include_overall": True,
    }
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/undq/transcript/engine",
            files=_engine_files(),
            data={
                "persist_artifacts": "false",
                "llm_enabled": "false",
                "aggregation_json": json.dumps(aggregation),
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    output = body["execution"]["output"]
    assert output["summary"]["rules_executed"] == 7
    assert output["summary"]["rules_failed"] == 0
    assert len(output["rule_results"]) == 7
    assert all(item["groups"] for item in output["rule_results"])
    assert body["business_impact"] is None
    assert body["execution"]["Observed_Results"]["details"]["check_summary"]


def test_canonical_rule_selection_filters_execution(monkeypatch) -> None:
    monkeypatch.setenv("UNDQ_EVIDENCE_HASH_SALT", "synthetic-test-only-salt")
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/undq/transcript/engine",
            files=_engine_files(),
            data={
                "persist_artifacts": "false",
                "selected_rule_ids_json": json.dumps(["fill_rate", "privacy_detection"]),
            },
        )
    assert response.status_code == 200, response.text
    results = response.json()["execution"]["output"]["rule_results"]
    assert {item["rule_id"] for item in results} == {"fill_rate", "pii_detection"}


def test_public_compatibility_routes_are_in_openapi() -> None:
    with TestClient(app) as client:
        paths = client.get("/openapi.json").json()["paths"]
    assert "/UnDQ/transcript/understanding/profiling" in paths
    assert "/UnDQ/transcript/execution" in paths
    assert "/UnDQ/transcript/engine/" in paths
