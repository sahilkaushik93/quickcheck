# Workbook traceability matrix

Status reflects the active runtime, not file presence.

| Excel requirement | Workbook / sheet | Repository implementation | Runtime path | Test evidence | Status | Gap / required patch |
|---|---|---|---|---|---|---|
| Common ingestion lifecycle | Supplemental / Common processing | upload staging, `CSVSourceAdapter` | API -> adapter | API tests | Aligned | Add non-CSV adapters later |
| Chunked/bounded processing | Consolidated / all scripts | profiler and execution accumulators | coordinators | all-rule engine test | Aligned | Load test still needed |
| Metadata dictionary join | Consolidated / inputs | `metadata_loader.py`, context builder | Understanding | Understanding API tests | Aligned | — |
| Evidence-based rule recommendation | Consolidated / all | applicability + plan builder | Understanding -> plan | seven selected assertions | Aligned | Consumer DTO naming differs |
| Speaker alias/tag parsing | Consolidated / Speaker | feature extractor + speaker handler | execution plan -> handler | all-rule engine test | Partially aligned | No row-complete LLM role inference |
| Speaker confidence >=80 and fallback | Consolidated / Speaker | sampled LLM assessment, deterministic parser | post-execution sampling | response-visible assessment | Partially aligned | Apply validated LLM result per evaluated row |
| Speaker monthly/direction aggregates | Consolidated + Supplemental output summary | generic time/dimension groups | aggregation resolver -> handler | monthly+direction engine test | Aligned | Direction artifact is aggregate metric format |
| Topic/demand multi-value split | Consolidated / Topic Drift | bounded delimiter split in topic handler | topic handler | all-rule engine test | Partially aligned | Demand needs independent series/artifacts |
| Ontology priority mapping | Consolidated / Topic Drift | versioned execution ontology, priority order | topic handler | all-rule engine test | Aligned | Governance approval/version migration needed |
| JSD thresholds | Consolidated / Topic Drift | pairwise drift in topic handler | finalize | grouped result inspection | Aligned | — |
| PSI and Cohen's h | Consolidated / Topic Drift | pairwise formulas with smoothing | finalize | grouped result inspection | Aligned | Hellinger absent |
| TF-IDF centroid/cosine drift | Consolidated / Topic Drift | metric explicitly not evaluated | topic result | serialization test | Missing | Add bounded vocabulary/vector accumulator |
| Duration WPM formula and units | Consolidated / Duration | shared word count + duration handler | execution | all-rule engine test | Aligned | Add complete boundary matrix tests |
| WPM pass/warn/fail thresholds | Consolidated / Duration | governed rule defaults | handler | rule executes | Aligned | — |
| Invalid/null/zero/negative duration | Consolidated / Duration | typed parse + skipped/failed categories | handler | runtime code path | Partially aligned | Add explicit tests for all cases |
| Fill null-like normalization | Consolidated / Fill | fill handler | execution | all-rule engine test | Aligned | — |
| Fill 90/70 thresholds | Consolidated / Fill | governed rule default parameters | execution plan -> handler | registry + engine test | Aligned | — |
| Non-English segment max 20 tokens / 2,000 chars | Consolidated / Language | sampled LLM policy exists | sampled enrichment | LLM-disabled path test | Partially aligned | Exact row-complete segmentation not implemented |
| Language categories and fail-closed | Consolidated / Language | deterministic/LLM sampled assessment | execution service | response serialization | Partially aligned | Retry/fail-closed matrix incomplete |
| PII email/phone/SSN/card + Luhn | Consolidated / Privacy | configured compiled patterns and validators | shared feature extractor -> handler | all-rule engine test | Aligned | DOB context coverage requires expansion |
| PII canonical dedup across columns | Consolidated / Privacy | transcript role currently preferred single text source | feature extractor | runtime inspection | Partially aligned | Cross-column entity-key dedup not complete |
| PII monthly/type counts | Consolidated / Privacy | grouped rate and category counts | handler | monthly engine test | Aligned | — |
| Spelling paragraph chunks <=3,000 | Consolidated / Spelling | sampled LLM policy + deterministic bounded tokenizer | LLM sampling / handler | warning visibility test | Partially aligned | Exact paragraph chunking not complete |
| Spelling formula | Consolidated / Spelling | unknown-token rate when governed vocabulary is adequate | handler | sample warning visible | Partially aligned | Small bundled vocabulary disables metric safely |
| LLM errors remain visible | Consolidated / semantic rules | sampled assessment status/errors | observed results | API serialization | Aligned | More timeout/invalid-JSON tests needed |
| Expected output filenames | Supplemental / Output-file summary | execution artifact writer | optional persistence | artifact list/download test | Partially aligned | Detail content is aggregate, and some topic-specific names absent |
| Deterministic vs LLM classification | Supplemental / Script classification | deterministic core; LLM only sampled semantic assessment | execution service | code-path test | Aligned | Semantic parity limitations above |
| Common normalization/aggregation/serialization | Supplemental / Common processing | config loaders, shared extractor, accumulators, writer | active path | all-rule engine test | Aligned | — |
| FastAPI responses preserve results | Requested API contract | response models + presentation | routers | TestClient tests | Aligned | — |
| Streamlit consumes API only | Requested UI contract | `ui/api_client.py` | Streamlit -> HTTP | AST import test | Aligned | Browser E2E test not added |
| Business Impact deferred | Requested phase boundary | null engine field + disabled route | engine/business route | API tests | Aligned | Future phase |
