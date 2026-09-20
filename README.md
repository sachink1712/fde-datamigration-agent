# Relay Migration Agent

Relay is a LangGraph generator-evaluator workflow for employee data migration.

## Workflow

`ingest → profile → classifier generator → strict classifier validator → cleaner generator → strict record validator → human review for only unresolved work → target push`

- **Classifier generator:** Gemini structured/Pydantic output, an XML-tagged few-shot prompt, and a confidence score for every received column. It has no hard-coded source-field aliases.
- **Cleaner generator:** Gemini structured output selects only bounded local tools for whitespace, names, email, phone, dates, and status; it returns a confidence score with each plan.
- **Validator evaluator:** strict schema/policy validation. It sends actionable feedback to the originating generator and permits at most two retries.
- **Human review:** only exhausted/rejected columns or employee records are withheld. Valid records can still be pushed.

## Run locally

```bash
uv sync --all-groups
uv run uvicorn backend.app:app --reload --port 8000
python3 -m http.server 5173 --directory apps/web
```

Open `http://localhost:5173`.

## Configuration

Configure `GEMINI_API_KEY`, `GEMINI_MODEL`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT`, and CORS in `.env`. Never expose keys to the frontend.

## Tests and guardrails

```bash
uv run pytest -q
```

Guardrails cover PII redaction, write authorization, evaluator retry logs, and pushing clean records while unresolved records are withheld.

## Production requirements

Use Postgres for state/checkpoints, encrypted object storage for uploads, tenant auth/RBAC, a real target API adapter, HTTPS, rate limits, and exact production CORS origins. SQLite/in-memory checkpoints are local-demo defaults.
