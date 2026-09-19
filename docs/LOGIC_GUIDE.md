# Where to implement each layer

## LLM providers

Edit `app/llm_provider.py` to add or change provider integrations. DQ-layer code should call:

```python
from app.llm_provider import generate_text

response = generate_text(
    prompt,
    request_id="1",
    provider_name="launchpad",
)
```

Add new adapters by subclassing `BaseLLMProvider` and registering the class in `PROVIDERS`. Keep credentials in environment variables documented in `.env.example`.

## Understanding layer

Implement profiling, schema analysis, domain categorization, DQ signal discovery, and rule preparation in:

- `app/services/understanding.py`

HTTP request parsing belongs in `app/api/routes/understanding.py`, but business logic should remain in the service.

## Execution layer

Implement rule planning, rule dispatch, metric calculation, row-level checks, and aggregate checks in:

- `app/services/execution.py`

As the rule set grows, create `app/rules/` and keep one handler per rule family. The execution service should orchestrate those handlers.

## Business-impact layer

Implement score calculation, evidence selection, impact mapping, recommendations, and LLM-generated narrative in:

- `app/services/business_impact.py`

Prompt templates can later move to `app/prompts/`. Call `generate_text` here rather than importing a provider-specific class.

## End-to-end engine

The sequencing logic is in:

- `app/services/engine.py`

It should only coordinate Understanding → Execution → Business Impact. Do not put profiling, DQ rules, scoring, or prompt logic in the engine.

## API contracts

Pydantic request and response models are in:

- `app/models/responses.py`

Routes are in `app/api/routes/`. Keep them thin: validate input, call a service, return its result.
