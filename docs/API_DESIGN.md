# API and Postman guide

Base path: `/api/v1/undq/transcript`.

## Uploaded Understanding inputs

`POST /understanding`, `/understanding/profiling`, `/understanding/rules`, and `/engine` use `multipart/form-data`. In Postman select **Body → form-data** and configure:

| Key | Type | Value |
|---|---|---|
| `sample_csv` | File | transcript/sample CSV |
| `metadata_dictionary` | File | metadata CSV/XLSX/XLS/XLSM |
| `rules_archive` | File | ZIP containing rule JSON files |
| `persist_artifacts` | Text | `false` while testing |
| `llm_enabled` | Text | `false`, or `true` for one enrichment call |
| `llm_provider` | Text | `ollama` when enabled |
| `llm_model` | Text | optional model override |

For loose rules, omit `rules_archive` and add the same `rule_files` key once per JSON file. Exactly one rule upload mode is required. Nested JSON files inside a ZIP are supported; unsafe paths, links, duplicate IDs and invalid schemas are rejected.

The response records safe provenance: original filenames, byte sizes, SHA-256 input fingerprints, rule filenames and a canonical validated registry fingerprint. Uploaded temporary files are removed after the request.

## Layer flow

1. Understanding streams the CSV, joins metadata, detects context/signals, loads the supplied registry and builds an execution plan.
2. `POST /execution` accepts `{"understanding": ...}`.
3. `POST /business-impact` accepts Understanding, Execution and optional business context.
4. `/engine` accepts the same uploads and composes all three services without internal HTTP calls.

`selected_rules` means the rule applies to the dataset. `execution_ready_rules` means the current Execution layer advertises the required handler. `implementation_pending_rules` are valid selected rules awaiting that handler; they are not incorrectly treated as inapplicable.

## LLM diagnosis

LLM enrichment is disabled unless `llm_enabled=true`. It runs once after deterministic processing and is serialized across API requests. If `llm_insights` is empty, inspect `understanding.warnings`; transport, timeout and invalid-JSON failures are reported there without exposing prompts or data. Configure Ollama with `.env`, then verify `POST http://127.0.0.1:11434/api/generate` independently.

Health endpoints: `GET /health`, `GET /api/v1/undq/transcript/understanding`, `GET /api/v1/undq/transcript/engine`, and `GET /business-impact`.
