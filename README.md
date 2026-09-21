# UnDQ QuickCheck — Transcript DQ MVP

Production-aware FastAPI + Streamlit MVP for profiling structured transcript exports, loading governed rules dynamically, executing seven DQ checks, and returning bounded aggregate results and downloadable artifacts. Business Impact is deliberately deferred.

## What is working

- CSV input is streamed with `pandas.read_csv(..., chunksize=...)`; chunks are never concatenated.
- Accumulators, samples, value frequencies, relationship candidates and evidence are bounded.
- Metadata dictionaries can be CSV, XLSX, XLS or XLSM and are matched to source columns.
- Every request supplies its own rule registry as multiple JSON files or one ZIP archive.
- Adding/removing valid rule JSON files changes the registry dynamically; no route/code edit is required.
- All seven supplied transcript rules are registered, recommended from profile evidence, and executable through the active runtime path.
- Optional LLM enrichment happens once after deterministic processing and is serialized across requests.
- User aggregation supports day/week/month/quarter/year, direction and bounded low-cardinality dimensions.
- Artifacts contain aggregate/redacted values only, are written atomically, and can be listed/downloaded through guarded API routes.
- Streamlit calls FastAPI over HTTP and never imports rule-engine internals.

## Start

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env              # Windows: copy .env.example .env
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/docs` or check `GET /health`.

In a second terminal:

```bash
streamlit run streamlit_app.py
```

Open `http://127.0.0.1:8501`. Set `UNDQ_API_BASE_URL` when the backend is not local.

## Understanding request

`POST /api/v1/undq/transcript/understanding` uses `multipart/form-data`:

| Field | Required | Meaning |
|---|---:|---|
| `sample_csv` | yes | Tabular transcript export (`file` remains a deprecated alias) |
| `metadata_dictionary` | yes | CSV/XLSX/XLS/XLSM column dictionary |
| `rule_files` | one mode | Repeat this field for each JSON rule |
| `rules_archive` | one mode | ZIP alternative to `rule_files` |
| `llm_enabled` | no | Defaults to `false` |
| `llm_provider` | no | `ollama`, `launchpad`, or `doc_intelligence` |
| `llm_model` | no | Provider model override |
| `persist_artifacts` | no | Defaults to `true` |

Provide exactly one rule mode. HTTP cannot upload a filesystem folder as a folder, so Postman users should multi-select the JSON files for repeated `rule_files` fields or upload the folder as `rules_archive`.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/undq/transcript/understanding \
  -F 'sample_csv=@data/input/lumi_sample.csv' \
  -F 'metadata_dictionary=@data/input/lumi_metadata_dictionary.xlsx' \
  -F 'rules_archive=@rules.zip' \
  -F 'persist_artifacts=false' \
  -F 'llm_enabled=false'
```

The end-to-end route `POST /api/v1/undq/transcript/engine` also accepts `aggregation_json`, `selected_rule_ids_json`, `column_overrides_json`, and `execution_options_json`. Compatibility routes under `/UnDQ/transcript/...` invoke the same functions. Profiling and rule views are available at `/understanding/profiling` and `/understanding/rules`.

## Dynamic rules

Rule files are data, not Python code. To add or remove a rule, change the uploaded JSON set/ZIP. The registry recursively discovers `*.json`, validates each file, rejects duplicate rule IDs, and returns upload and canonical registry fingerprints. A new `check_name` is accepted, but it remains `implementation_pending` until the Execution layer advertises the matching capability/handler.

## Tests

```bash
python -m pytest -q
```

See `docs/STARTUP.md`, `docs/AUDIT_REPORT.md`, `docs/TRACEABILITY_MATRIX.md`, and `docs/SEVEN_RULE_SCORECARD.md` for verified behavior and remaining gaps.
