# Relay Migration Agent

Relay is a human-supervised employee-data migration application. It ingests CSV and Excel HR exports, maps their columns to a fixed employee schema, cleans supported values, sends uncertain work to a reviewer, and pushes only records that pass deterministic policy checks.

LLMs can propose mappings and cleaning plans, but cannot bypass the schema, sensitive-data, review, or write-authorization rules.

## Features

- Upload CSV, XLSX, or XLSM exports (up to 10 MB per file), or run the bundled demo data.
- Map source columns to the employee target schema using structured Gemini output.
- Exclude bank, government-ID, passport, and health columns from migration and LLM sample prompts.
- Clean names, email addresses, phones, dates, amounts, employment status, and employee references with bounded local tools.
- Review only ambiguous mappings, invalid records, conflicts, duplicates, and other anomalies.
- Push authorized records using idempotent upserts and roll back a migration batch.
- Record an activity trail and optionally emit PII-safe LangSmith traces.

## Architecture

```text
Browser UI (vanilla HTML/CSS/JavaScript)
                 │  HTTP / multipart upload
                 ▼
             FastAPI API
                 │
                 ▼
         LangGraph migration workflow
 ingest → classify ⇄ validate → clean ⇄ validate → route
                                                 │
                                    human review │
                                                 ▼
                                authorized push / rollback
                 │
                 ▼
       SQLite run store + mock target HRMS
```

The classifier and cleaner are generator agents. A strict validator evaluates their output, provides actionable feedback, and permits a maximum of two retries. Work that remains unresolved is held for human review, while valid records can continue to the push stage.

## Tech stack

| Area | Technology |
| --- | --- |
| API | Python 3.12+, FastAPI, Uvicorn, Pydantic v2 |
| Agent workflow | LangGraph and LangChain Core |
| LLM | Google Gemini via `langchain-google-genai` with Pydantic structured output |
| Data processing | pandas, openpyxl, phonenumbers |
| Validation | JSON Schema plus deterministic data-quality and policy checks |
| Persistence | SQLite for runs, mock target records, and rollback history |
| Frontend | Static vanilla HTML, CSS, and JavaScript |
| Observability | Optional LangSmith with PII-redacted events |
| Quality | pytest, httpx, and labelled agent/e2e evaluation suites |
| Dependencies | uv, `pyproject.toml`, and `uv.lock` |

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (recommended)
- A modern browser
- A Google Gemini API key for LLM-assisted classification and cleaning (optional for the deterministic fallback)

## Quick start

1. Clone the repository.

   ```bash
   git clone https://github.com/sachink1712/fde-datamigration-agent.git
   cd fde-datamigration-agent
   ```

2. Install dependencies.

   ```bash
   uv sync --all-groups
   ```

3. Create a `.env` file and configure Gemini.

   ```dotenv
   GEMINI_API_KEY=your_key_here
   GEMINI_MODEL=gemini-2.5-flash
   ```

4. Start the backend.

   ```bash
   uv run uvicorn backend.app:app --reload --port 8000
   ```

5. In a second terminal, serve the frontend.

   ```bash
   python3 -m http.server 5173 --directory apps/web
   ```

6. Visit [http://localhost:5173](http://localhost:5173), upload source files, or choose **Use bundled demo data**.

The health check is available at [http://localhost:8000/health](http://localhost:8000/health).

### Live demo

Open the deployed web application at [https://fde-datamigration-agent.vercel.app/](https://fde-datamigration-agent.vercel.app/).

> The API is deployed on Render's free tier. If it is initially unavailable, its resources may be unallocated; wait briefly and try again while the service starts.

### Running without a Gemini key

Relay starts safely without `GEMINI_API_KEY`. The cleaner uses a schema-driven deterministic plan, but automatic source-column classification cannot be completed and is routed safely instead of guessed. A Gemini key is needed for regular automatic mapping.

## Configuration

Configuration comes from environment variables; `.env` is loaded by the workflow. Never expose keys to the frontend.

| Variable | Default | Purpose |
| --- | --- | --- |
| `GEMINI_API_KEY` | — | Gemini API key; `GOOGLE_API_KEY` is also accepted. |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Model for structured agent output. |
| `CORS_ALLOWED_ORIGINS` | local port 5173 origins | Comma-separated permitted frontend origins. |
| `ENVIRONMENT` | `development` | Set to `production` to disable local-host CORS matching. |
| `MAX_UPLOAD_BYTES` | `10485760` | Per-file upload limit. |
| `UPLOAD_DIR` | `data/uploads` | Validated upload storage directory. |
| `LANGSMITH_API_KEY` | — | Enables optional LangSmith tracing with a project. |
| `LANGSMITH_PROJECT` | — | LangSmith project name. |

## Migration lifecycle

1. **Ingest and profile** — reads CSV files and Excel sheets, removes empty rows/columns, and captures safe samples.
2. **Classify** — Gemini proposes a target field, transform, and confidence for every source column. Sensitive source columns are forcibly ignored.
3. **Validate mappings** — deterministic checks verify schema field, confidence, value shape, transforms, duplicate mappings, and sensitivity policy; rejected mappings receive retry feedback.
4. **Plan and clean** — Gemini chooses only predefined local tools. When unavailable or unsuccessful, Relay uses a deterministic schema-driven plan.
5. **Validate records** — checks schema conformance, required fields, cleaning confidence, conflicts, duplicates, manager references/cycles, and cross-record anomalies.
6. **Review exceptions** — reviewers can approve, correct, reject, merge, or keep both records as appropriate.
7. **Push or roll back** — only schema-valid, non-blocked records are allowed through the final idempotent write gate. The included SQLite target stores batch history for rollback.

## Target schema and transformations

The source of truth is [`schemas/employee.schema.json`](schemas/employee.schema.json). It defines the employee contract, required fields, allowed formats, and enumerations for fields including employee ID, names, email, phone, start date, department, job title, manager ID, annual salary, and employment status.

Supported transformations:

- `direct`: map a source value directly.
- `split_full_name`: derive first and last names from a full-name field.
- `monthly_to_annual`: convert monthly pay to annual salary.
- `ignore`: exclude unrelated or sensitive source data.

Cleaning is constrained to named tools rather than free-form value generation: whitespace trimming, email/name/phone/date/status normalization, amount parsing, and employee-reference resolution.

## API

| Method | Endpoint | Description |
| --- | --- | --- |
| `GET` | `/health` | Service status. |
| `GET` | `/api/schema` | Target employee JSON Schema. |
| `POST` | `/api/runs` | Creates and processes a run. Submit repeated multipart `files` fields; no files uses demo fixtures. |
| `GET` | `/api/runs/{run_id}` | Run state, activity, review queue, and outcomes. |
| `POST` | `/api/runs/{run_id}/escalations/{escalation_id}` | Review decision: `approved`, `corrected`, `rejected`, `merged`, or `keep_both`. |
| `POST` | `/api/runs/{run_id}/push` | Push authorized records; add `?retry=true` for a failed push. |
| `POST` | `/api/runs/{run_id}/rollback` | Restore the mock target to the state before that batch. |

## Repository layout

```text
apps/web/                 Migration-control-room UI
backend/app.py            FastAPI routes and CORS configuration
backend/graph.py          LangGraph workflow and retry loops
backend/agents.py         Gemini generators and strict evaluator
backend/engine.py         Migration, review, push, and rollback lifecycle
backend/tools.py          Bounded deterministic cleaning tools
backend/checks.py         Record and cross-record validations
backend/policies.py       Schema, PII, threshold, and write policies
backend/store.py          SQLite run store and mock target adapter
backend/uploads.py        Upload validation and storage
backend/observability.py  PII-safe optional LangSmith events
schemas/                  Employee target JSON Schema
data/fixtures/            Bundled demo source data
evals/                    Labelled datasets, harness, and results
```

## Tests and evaluations

Run tests:

```bash
uv run pytest -q
```

The evaluation harness measures classifier quality, tool selection and cleaned values, validator precision/recall, plus end-to-end mapping, escalation, push, and rollback integrity.

```bash
# Cleaner and validator coverage without an API key
uv run python -m evals.run_evals --mode deterministic

# Full Gemini-backed evaluation
uv run python -m evals.run_evals --mode live --min-interval 6

# CI-style quality gate
uv run python -m evals.run_evals --mode live --min-score 0.85
```

See [`evals/README.md`](evals/README.md) for scoring methodology, supported modes, datasets, and documented findings.

## Safety and privacy

- Sensitive bank, government-ID, passport, and health columns are never mapped or sent to the LLM as samples.
- LLM responses must satisfy Pydantic contracts; generated text is not executable instruction.
- Deterministic policy checks are the final authority for target writes.
- Records awaiting or failing review cannot be pushed.
- PII is hashed/redacted before optional observability events are emitted.
- Uploads are restricted by extension and size, stored outside the static web root, and assigned generated filenames.

## Production readiness

The repository uses SQLite and a mock target HRMS so that the complete flow runs locally. Before production deployment, add or replace:

- Durable state, graph checkpoints, and audit history (for example PostgreSQL).
- Encrypted object storage and retention/deletion controls for uploaded files.
- A real target-system adapter with service authentication, idempotency, and robust failure handling.
- Tenant authentication, RBAC, HTTPS, audit logging, rate limits, and exact production CORS origins.
- Secrets management, monitoring, and a release gate that runs the evaluation suite.

## License

No license file is currently included. Add an explicit license before distributing or reusing the source outside its intended project context.
