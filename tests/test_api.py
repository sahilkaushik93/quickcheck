from io import BytesIO

from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def csv_upload():
    return {"file": ("lumi_sample.csv", BytesIO(b"intrct_id,trnscr_tx\n1,hello"), "text/csv")}


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_llm_provider_catalog():
    response = client.get("/api/v1/undq/llm/providers")
    assert response.status_code == 200
    assert response.json()["available_providers"] == [
        "doc_intelligence",
        "launchpad",
        "ollama",
    ]


def test_engine_contract():
    response = client.post("/api/v1/undq/transcript/engine", files=csv_upload())
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"understanding", "execution", "business_impact"}
    assert set(body["understanding"]) == {"Profile", "Rules"}
    assert set(body["business_impact"]) == {
        "scores",
        "evidences",
        "impact_insights",
    }


def test_all_layer_endpoints():
    endpoints = [
        "/api/v1/undq/transcript/understanding",
        "/api/v1/undq/transcript/understanding/profiling",
        "/api/v1/undq/transcript/understanding/rules",
    ]
    for endpoint in endpoints:
        response = client.post(endpoint, files=csv_upload())
        assert response.status_code == 200


def test_layered_json_pipeline():
    understanding_response = client.post(
        "/api/v1/undq/transcript/understanding",
        files=csv_upload(),
    )
    understanding_payload = understanding_response.json()
    understanding_payload["llm"] = {
        "provider": "ollama",
        "model": "llama3.1:8b",
        "request_id": "test-run",
    }

    execution_response = client.post(
        "/api/v1/undq/transcript/execution",
        json=understanding_payload,
    )
    assert execution_response.status_code == 200
    execution_payload = execution_response.json()
    assert set(execution_payload) == {"understanding", "execution"}

    execution_payload["business_context"] = {
        "use_case": "LUMI transcript quality",
        "audience": "DQ analyst",
    }
    execution_payload["llm"] = understanding_payload["llm"]
    impact_response = client.post(
        "/api/v1/undq/transcript/business-impact",
        json=execution_payload,
    )
    assert impact_response.status_code == 200
    assert set(impact_response.json()) == {
        "understanding",
        "execution",
        "business_impact",
    }


def test_browser_friendly_component_status():
    endpoints = [
        "/business-impact",
        "/api/v1/undq/transcript/business-impact",
        "/api/v1/undq/transcript/execution",
        "/api/v1/undq/transcript/understanding",
        "/api/v1/undq/transcript/engine",
    ]
    for endpoint in endpoints:
        response = client.get(endpoint)
        assert response.status_code == 200
        assert response.json()["status"] == "healthy"


def test_rejects_non_csv():
    response = client.post(
        "/api/v1/undq/transcript/engine",
        files={"file": ("input.txt", BytesIO(b"not a csv"), "text/plain")},
    )
    assert response.status_code == 415
