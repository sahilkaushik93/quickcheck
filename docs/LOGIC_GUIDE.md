# Code ownership guide

| Responsibility | File(s) |
|---|---|
| Multipart validation, bounded staging, safe ZIP extraction | `app/api/routes/_uploads.py` |
| Upload-to-service request/provenance mapping | `app/api/routes/_understanding_inputs.py` |
| Understanding/engine HTTP surfaces | `app/api/routes/understanding.py`, `app/api/routes/engine.py` |
| Public contracts | `app/models/responses.py`, `app/understanding/models.py` |
| Workflow order | `app/understanding/coordinator.py` |
| Chunked CSV adapter | `app/understanding/source_adapters/` |
| Mergeable bounded statistics | `app/understanding/profiling/` |
| Metadata, domains and relationships | `app/understanding/context/` |
| DQ signal generation | `app/understanding/signals/detector.py` |
| Rule schema, registry, applicability and plan | `app/understanding/rules/` |
| Optional one-call LLM narrative | `app/understanding/enrichment/llm_enrichment.py` |
| Provider transports | `app/llm_provider.py` |
| Atomic aggregate artifacts | `app/understanding/storage/artifact_writer.py` |
| API-facing Understanding adapter | `app/services/understanding.py` |
| Downstream composition | `app/services/engine.py`, `execution.py`, `business_impact.py` |

Configuration thresholds live under `config/understanding/`. Rule definitions remain under the uploaded registry and the bundled examples under `config/understanding/rules/`; do not embed thresholds or column mappings in routes.

To add a deterministic check, first add its rule JSON. If it needs a new runtime metric, implement the Execution handler and advertise the capability named by `implementation.required_capabilities`. No Understanding route change is necessary.
