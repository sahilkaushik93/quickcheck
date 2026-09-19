# UnDQ Transcript DQ MVP

FastAPI scaffold for a transcript metadata data-quality engine.

## API surface

- `GET /health`
- `POST /api/v1/undq/transcript/engine`
- `POST /api/v1/undq/transcript/understanding`
- `POST /api/v1/undq/transcript/understanding/profiling`
- `POST /api/v1/undq/transcript/understanding/rules`
- `POST /api/v1/undq/transcript/execution`
- `POST /api/v1/undq/transcript/business-impact`
- `GET /business-impact`
- `GET /api/v1/undq/llm/providers`

The understanding and engine endpoints accept CSV files. Execution accepts the understanding JSON envelope. Business impact accepts the combined understanding and execution JSON envelope. The engine composes the same service functions used by the layer-specific endpoints.

## LLM provider selection

Copy `.env.example` to `.env` and configure the required provider. It is loaded automatically at application startup. Supported adapters are `launchpad`, `ollama`, and `doc_intelligence`. JSON APIs accept an optional `llm` object:

```json
{
  "llm": {
    "provider": "ollama",
    "model": "llama3.1:8b",
    "request_id": "run-001"
  }
}
```

The CSV-based engine exposes `llm_provider`, `llm_model`, and `request_id` as multipart form fields. See `docs/LOGIC_GUIDE.md` for the exact files that own each layer's logic.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open Swagger at `http://127.0.0.1:8000/docs`.

## Test

```bash
pytest -q
```

## Next implementation step

Replace the stub logic in `app/services/` with profiling and DQ rule implementations. Keep the API response contracts unchanged so Streamlit and other consumers do not need to change.
