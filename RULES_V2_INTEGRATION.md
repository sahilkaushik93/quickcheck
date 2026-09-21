# Transcript DQ Rules v2 integration

This bundle is a drop-in upgrade for the existing `quickcheck_QC` repository.
Copy the bundle contents over the repository root, preserving paths. Existing
files at the same paths should be replaced; the two new execution modules and
two new configuration files should be added.

## What changed

- Rule selectors now target governed transcript concepts rather than every
  column. The seven approved checks remain registry-driven.
- Fill rate follows the validation table: pass at 90% or above, warn above 70%
  and below 90%, fail at 70% or below.
- Topic values are exploded, mapped through a small priority ontology, and
  compared across consecutive time buckets using JSD, PSI, and Cohen's h.
- Speaker parsing supports both line-oriented and inline Nexidia tags.
- Speech-per-duration uses pass (40–250 WPM), warning (30–<40 or >250–300),
  and fail (<30 or >300) bands.
- PII remains deterministic and returns category counts only; SSN, contextual
  DOB, phone, email, and Luhn-validated payment-card patterns are supported.
- A small business glossary is no longer treated as a complete dictionary.
  Spelling stays unavailable until a sufficiently broad governed vocabulary is
  configured, avoiding misleading false-positive rates.
- Optional LLM analysis uses a second streaming pass, deterministic
  hash/stratified sampling, PII redaction, truncation, and exactly one request.
  Its output is advisory and never changes deterministic rule outcomes.
- `Observed_Results.details` now contains `run_scope`, `check_summary`,
  `grouped_results`, `evidence_summary`, and `llm_sample_assessment`.
- Optional artifact persistence also writes `rule_overall_summary.csv` and
  `rule_grouped_metrics.csv` beside the canonical JSON.

## API use

For `POST /api/v1/undq/transcript/engine` or the standalone execution endpoint,
set multipart fields:

- `llm_enabled=true`
- `llm_provider=ollama` (or `launchpad` / `doc_intelligence`)
- `llm_model=<installed model>` when needed
- `request_id=<stable request identifier>`

Use `column_overrides_json` only when aliases or Understanding semantics cannot
resolve a physical field, for example:

```json
{
  "transcript": "trnscr_tx",
  "duration": "media_fl_dur_mscnd",
  "topic": "topic_nm",
  "interaction_direction": "intrct_drct",
  "interaction_date": "intrct_dt"
}
```

Request monthly and direction-level output independently of rule logic:

```json
{
  "time": {"column": "intrct_dt", "grain": "month"},
  "dimensions": [{"column": "intrct_drct", "alias": "interaction_direction"}],
  "include_overall": true,
  "maximum_groups": 5000
}
```

## Governance notes

Edit `config/execution/transcript_dq_ontology.json` to extend aliases, direction
normalization, speaker roles, and topic patterns. Edit the seven versioned rule
JSON files to change governed thresholds or selectors. Edit
`llm_sampling_policy.json` to change only bounded sampling limits. LLM output
must remain a suggestion with confidence and limitations; never write its
suggestions directly into approved rule JSON.

For production, replace the MVP word allowlist with a versioned dictionary of
at least 500 reviewed tokens or a deterministic enterprise spell-check service.
Keep raw transcripts and raw PII out of logs and final artifacts.
