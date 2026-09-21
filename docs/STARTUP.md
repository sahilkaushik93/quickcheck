# Startup and verification

## Supported runtime

- Python 3.11 or newer
- CSV is the POC source adapter; the adapter contract permits future database/BigQuery implementations.
- The metadata dictionary may be CSV, XLSX, XLS or XLSM.
- Rules are supplied per request as repeated JSON files or a ZIP archive.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set a private `UNDQ_EVIDENCE_HASH_SALT` in `.env`. Never reuse the example value in a real environment.

## Backend

```bash
uvicorn app.main:app --reload
```

Useful URLs:

- `http://127.0.0.1:8000/health`
- `http://127.0.0.1:8000/ready`
- `http://127.0.0.1:8000/docs`
- `http://127.0.0.1:8000/openapi.json`

The versioned base path is `/api/v1/undq/transcript`. Compatibility paths are also exposed at `/UnDQ/transcript/...`.

## Frontend

```bash
streamlit run streamlit_app.py
```

The UI uses `UNDQ_API_BASE_URL` (default `http://127.0.0.1:8000`) and HTTP only. It does not import backend services.

## Tests

```bash
python -m pytest -q
```

## Example input

Use:

- `data/input/lumi_sample.csv`
- `data/input/lumi_metadata_dictionary.xlsx`
- `data/input/rules.zip`

For monthly aggregation use `intrct_ts` with the bundled sample because `intrct_dt` is deliberately sparse.

## Important environment variables

| Variable | Purpose |
|---|---|
| `UNDQ_EVIDENCE_HASH_SALT` | Required secret salt for stable evidence fingerprints |
| `UNDQ_EXECUTION_OUTPUT_ROOT` | Execution artifact root |
| `UNDQ_API_BASE_URL` | Streamlit backend URL |
| `UNDQ_UI_HTTP_TIMEOUT_SECONDS` | UI HTTP timeout |
| `UNDQ_CORS_ORIGINS` | Comma-separated allowed UI origins |
| `LLM_PROVIDER` | `ollama`, `launchpad`, or `doc_intelligence` |
| `OLLAMA_API_URL`, `OLLAMA_MODEL` | Ollama endpoint and model |

## POC limits

- Source processing is chunked and memory-bounded, but CSV parsing is serial.
- LLM assessment is optional, sampled, sequential, and does not replace deterministic metrics.
- Generated `*_detail.csv` files currently contain aggregate metric rows, not source-row extracts. This protects transcript/PII data but is not full workbook detail parity.
- Business Impact is unavailable by design in this phase.
