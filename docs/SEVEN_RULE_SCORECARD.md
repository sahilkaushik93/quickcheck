# Seven-rule readiness scorecard

| Canonical rule | Registered / reachable | Inputs and processing | Formula / thresholds | Detail output | Aggregates / summary | LLM alignment | API / UI | Tests | Overall |
|---|---|---|---|---|---|---|---|---|---|
| Speaker Tag Validation | Yes / Yes | Preferred transcript alias, deterministic role/tag parse | Invalid-tag ratio; configured max | Aggregate-shaped `detail.csv` | Monthly + direction + summary | Sampled only; not row-complete confidence fallback | Serialized/rendered | Combined runtime | Partially aligned |
| Topic Drift Validation | Yes / Yes (`topic_distribution`) | Preferred topic alias, split, priority ontology | JSD/PSI/Cohen h thresholds aligned | Aggregate-shaped `detail.csv` | Monthly/pairwise/summary | Deterministic as required | Serialized/rendered | Combined runtime | Partially aligned |
| Transcript Duration vs Size | Yes / Yes (`speech_per_duration_rate`) | Preferred transcript + duration aliases, WPM | 40/250 pass, 30/300 warn boundaries configured | Aggregate-shaped `detail.csv` | Monthly + summary | Deterministic | Serialized/rendered | Combined runtime | Mostly aligned |
| Fill Rate Validation | Yes / Yes | Config-selected columns, null-like handling | >=90 pass, >70 warn, <=70 fail | Per-column metrics in aggregate export | Overall/monthly/summary | Deterministic | Serialized/rendered | Combined runtime | Mostly aligned |
| Non-English and Mistranscription | Yes / Yes (`mistranslated_rate`) | Lexicon matching plus sampled semantic assessment | Token/record rates available | Aggregate-shaped `detail.csv` | Monthly + summary | Sampling/retry path, not full row workflow | Serialized/rendered | Combined runtime | Partially aligned |
| PII / Privacy Detection | Yes / Yes (`pii_detection`) | Configured regex, Luhn/SSN validators, redaction | Record rate/type counts | Aggregate-shaped `detail.csv` | Monthly + summary | Deterministic | Serialized/rendered | Combined runtime | Mostly aligned |
| Spelling Error Rate | Yes / Yes (`non_english_spelling`) | Shared tokenizer + governed vocabulary gate | Unknown-token rate; no fabricated result if vocabulary inadequate | Aggregate-shaped `detail.csv` | Monthly + summary | Sampled semantic assessment | Serialized/rendered | Combined runtime | Partially aligned |

## Canonical mapping

| Workbook ID | Runtime registry ID |
|---|---|
| `speaker_tag_validation` | `agent_speaker_tag_validation` |
| `topic_drift` | `topic_distribution` |
| `transcript_duration_vs_size` | `speech_per_duration_rate` |
| `fill_rate` | `fill_rate` |
| `non_english_mistranscription` | `mistranslated_rate` |
| `privacy_detection` | `pii_detection` |
| `spelling_validation` | `non_english_spelling` |

Both forms are accepted by API rule selection; registry execution always uses the runtime ID and responses include the canonical ID.
