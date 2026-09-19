# API design

Base path: `/api/v1/undq/transcript`

The layer endpoints and end-to-end engine reuse the same service functions. The engine does not make HTTP calls back into the application.

## Layered request flow

1. `POST /understanding` accepts CSV and returns `{"understanding": ...}`.
2. `POST /execution` accepts that JSON and returns `{"understanding": ..., "execution": ...}`.
3. `POST /business-impact` accepts that JSON, plus optional `business_context`, and returns the complete assessment.
4. `POST /engine` accepts CSV and performs all three steps.

Opening `GET /business-impact` or the versioned business-impact URL in a browser returns component health and usage guidance. Actual assessment uses `POST` with JSON.

## Engine response

```json
{
  "understanding": {
    "Profile": {},
    "Rules": {}
  },
  "execution": {
    "Observed_Results": {}
  },
  "business_impact": {
    "scores": {},
    "evidences": {},
    "impact_insights": {}
  }
}
```

The current implementation returns stub messages while preserving the target contract.
