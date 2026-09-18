# Migration Control Room

A small, supervised employee-data migration agent. It autonomously maps and cleans low-risk data, but policy gates target writes and routes ambiguous or sensitive cases to a human reviewer.

## Architecture

The backend is a LangGraph workflow: `ingest → profile → Gemini mapping agent → mapping evaluator → human interrupt when needed → transform → policy gate → push/retry/rollback`. Each executor stage produces evidence; the evaluator/policy layer selects `continue`, `retry`, `review`, or `rollback`. Gemini proposes mappings through structured output, but cannot directly call target tools.

- `apps/web`: separately deployable review UI.
- `backend`: FastAPI API, orchestration, SQLite demo persistence, PII-safe observability.
- `data/fixtures`: deliberately inconsistent source exports.
- `schemas/employee.schema.json`: versioned target contract consumed by the mapper, evaluator, and target-write policy.
- `evals`: regression and guardrail tests that CI uses as a quality gate.

## Run locally

```bash
uv sync --all-groups
uv run uvicorn backend.app:app --reload
python3 -m http.server 5173 --directory apps/web
```

Open `http://localhost:5173`. Start the demo, approve/reject the review items, push the batch, retry the intentional mock failure, then roll it back.

Before running it as an AI agent, paste your Gemini key into the ignored `.env` file:

```dotenv
GEMINI_API_KEY=your_key_here
GEMINI_MODEL=gemini-2.5-flash
REQUIRE_GEMINI=true
```

`GEMINI_MODEL` is the only model-selection variable. The same backend configuration is used locally and in production; production receives these values from its secret manager/GitHub environment secrets. Without a key, the local demo labels its conservative header-alias behavior as `deterministic_fallback`; it never claims that fallback was an AI decision.

The demo UI can be served from either `http://localhost:5173` or `http://127.0.0.1:5173`. For a deployed frontend, set `ENVIRONMENT=production` and replace `CORS_ALLOWED_ORIGINS` with its exact HTTPS origin.

## Evals and monitoring

Run `uv run pytest -q`. The initial suite validates the escalation boundary, target-write policy, retry behavior, and PII redaction. Expand it with labeled mapping/dedupe fixtures and enforce agreed quality thresholds in CI.

Set `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT`, and `LANGSMITH_TRACING=true` in the backend deployment environment to enable PII-safe, graph-level LangSmith traces. Raw source values are never sent to the custom observability boundary or Gemini mapping prompt. Use GitHub Actions secrets for these values and for the separate deployment commands (`DEPLOY_BACKEND_COMMAND`, `DEPLOY_FRONTEND_COMMAND`). Set the corresponding repository variable (`DEPLOY_BACKEND_ENABLED` or `DEPLOY_FRONTEND_ENABLED`) to `true` only after its deployment secret has been configured.

## Security boundary

Unexpected sensitive columns are quarantined, uploaded values are treated only as data, writes require deterministic validation with zero unresolved reviews, and the target client is restricted to idempotent upserts/retries. Deletion is limited to an auditable batch rollback endpoint.
