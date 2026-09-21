# Technical audit and remediation report

## Executive verdict

**Partially operational.** The active runtime is operational from uploaded structured CSV through Understanding, all seven Execution handlers, computed grouped results, FastAPI, protected artifact download, and an HTTP-only Streamlit UI. The verified sample run processed 8 rows, invoked all seven rules, produced zero rule failures and 26 artifact references.

Full workbook parity is not yet complete. In particular, the current artifact `*_detail.csv` files are aggregate metric exports rather than workbook-specified row/turn/segment detail; topic semantic centroid/cosine drift is explicitly `not_evaluated`; and the three semantic rules use one bounded sampled LLM assessment rather than row-complete LLM evaluation. Those limitations are visible rather than fabricated.

Business Impact is correctly deferred: the engine returns `business_impact: null`, its status endpoint says `disabled`, and POST returns HTTP 501.

## Sources inspected

- Every populated row in `Consolidated_Validation_Table(2).xlsx`, sheet `Consolidated view`.
- Every populated row in `Supplemental_Validation_Tables(2).xlsx`, sheets `Output-file summary`, `Common processing`, and `Script classification`.
- Active imports, routers, services, coordinators, models, adapters, registry, seven handlers, configuration, tests, Postman assets and startup files.
- Git status/diff. The repository was already heavily patched and dirty; unrelated changes were retained.

## Active runtime

```text
multipart uploads
  -> request-scoped staging + SHA-256 provenance
  -> CSVSourceAdapter chunk stream
  -> UnderstandingCoordinator
  -> evidence-based applicability + ExecutionPlan
  -> canonical/legacy rule-ID normalization
  -> ExecutionCoordinator (one feature extraction per row)
  -> seven bounded handlers + grouped accumulators
  -> redacted evidence + artifacts
  -> FastAPI response
  -> Streamlit HTTP client
```

## Remediation applied

| Path | Reason |
|---|---|
| `app/execution/rule_ids.py` | One canonical workbook-ID map with legacy registry aliases |
| `app/execution/presentation.py` | Consumer-readable computed results with canonical IDs |
| `app/execution/aggregation/models.py` | Configuration-driven estimates for unknown profiled date ranges |
| `app/execution/aggregation/resolver.py` | Removed false 5,000-bucket rejection while retaining runtime hard caps |
| `app/understanding/models.py` | Allowed nested JSON metric values required by per-column results |
| `app/execution/handlers/topic_distribution.py` | Fixed group ordering and configuration-priority topic selection |
| `config/understanding/rules/*.json` | Workbook thresholds and bounded column selectors |
| `data/input/rules.zip` | Refreshed from the active governed registry |
| `app/services/execution.py` | Canonical rule selection, dynamic plan filtering, provenance validation |
| `app/services/engine.py` | Active Understanding-to-Execution composition; Business Impact removed |
| `app/api/routes/*.py` | Multipart execution, readiness, artifacts and deferred Business Impact |
| `app/main.py` | CORS and documented compatibility endpoints |
| `app/execution/storage/artifact_writer.py` | Atomic summary/grouped/per-rule outputs with safe references |
| `streamlit_app.py`, `ui/*` | FastAPI-only POC UI with profiling, selection, aggregation and results |
| `tests/*` | Runtime reachability, all-rule execution, selection and API checks |

## Understanding Layer

- Schema, row/column counts, type inference, null statistics, bounded frequencies, numeric/string/date statistics and capped samples are computed in a streaming pass.
- The uploaded metadata dictionary is normalized and its descriptions/types join the source profile.
- Domain classification uses weighted ontology evidence from column name, metadata, type and value hints.
- Relationship candidates and DQ signals are bounded and deterministic.
- Rule recommendations include target columns, rationale, applicability, parameters, readiness and provenance.
- The uploaded registry is fingerprinted. Adding/removing a valid rule changes the plan without changing API code; an unknown handler remains implementation-pending.
- LLM enrichment is optional, sequential and post-deterministic. Suggestions require review.

Remaining Understanding gap: the public recommendation model retains the repository's `rule_applicability` contract rather than exactly renaming every field to the workbook example. The information is present, but a dedicated consumer DTO could improve naming.

## Execution Layer

- Seven handlers are registered and reachable.
- Shared transcript tokenization, PII patterns, speaker parsing and duration features are extracted once per chunk.
- Overall plus time/dimension groups are independent of handlers.
- State is capped by maximum groups/categories/evidence rather than source row count.
- Rule exceptions are isolated and successful results survive.
- Evidence contains salted SHA-256 row fingerprints and categories, never transcripts or raw PII.
- `Observed_Results.details.check_summary`, grouped results, canonical `output.rule_results`, warnings, errors and evidence are populated.

Remaining Execution gaps are listed in the traceability matrix and scorecard. The largest are row-complete workbook detail exports, exact semantic LLM workflows and topic TF-IDF centroid/cosine calculation.

## FastAPI

Implemented and verified:

- `GET /health`
- `GET /ready`
- `POST /api/v1/undq/transcript/understanding`
- `POST /api/v1/undq/transcript/execution`
- `POST /api/v1/undq/transcript/engine`
- safe artifact list/download routes
- compatibility POST routes under `/UnDQ/transcript/...`
- OpenAPI, Pydantic validation, request-scoped uploads, size limits, CORS settings and structured errors

## Streamlit

The UI uploads CSV/dictionary/rules, previews at most 100 rows, calls Understanding, shows profiles/domains/signals/recommendations, permits rule selection and aggregation controls, calls Execution over HTTP, and renders actual rule/grouped results. It labels Business Impact as future work.

## Patch assessment

- Correctly integrated prior work: streaming adapters/profiler, typed contracts, dynamic rule registry, applicability/plan builder, execution handlers, shared feature extraction, evidence collector and LLM provider abstraction.
- Previously partial/unreachable: execution response was difficult to interpret, public selection did not accept canonical IDs, artifact download was absent, Streamlit was absent, and Business Impact fabricated outputs.
- Defects corrected: false aggregation-cardinality failure, nested metric serialization failure, topic group sort failure, stale rule ZIP and broad selectors.
- Duplicate configuration risk remains: Understanding domain ontology and Execution transcript ontology are separate governed files. Both are active for different responsibilities and should be consolidated in a later governance migration, not silently replaced.

## Test evidence

Commands:

```bash
python -m pytest -q
python -m compileall -q app ui streamlit_app.py
```

Runtime TestClient verification exercised a monthly + direction engine request, all seven rules, artifact listing and artifact download. The resulting execution summary was:

```json
{
  "rows_read": 8,
  "rows_after_filters": 8,
  "chunks_processed": 1,
  "groups_created": 8,
  "rules_requested": 7,
  "rules_executed": 7,
  "rules_completed": 7,
  "rules_failed": 0
}
```

## Risks and deferred work

### Blocking for full workbook parity

- True row/turn/segment detail outputs with pagination or protected artifacts.
- Speaker LLM confidence>=80 and row fallback applied across evaluated records.
- Non-English 20-token segments/2,000-character LLM workflow and spelling 3,000-character paragraph chunks across evaluated records.
- Topic demand-specific outputs, Hellinger metric and TF-IDF semantic centroid/cosine drift.

### High priority

- Expand boundary/invalid-input tests to the entire workbook checklist.
- Replace the small spelling glossary with an approved enterprise vocabulary/model.
- Add database/BigQuery source adapters and asynchronous job APIs for 10M+ interactive workloads.

### Medium priority

- Consolidate ontologies with a migration/version policy.
- Add retention/expiry policy to local artifacts and authentication/authorization before deployment.

### Deferred

- Business Impact scoring, risk, recommendations and impact insights.
