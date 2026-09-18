# One-page approach: Relay Migration Control Room

## What I built

Relay is a supervised AI agent for migrating employee data from inconsistent CSV/XLSX exports to a canonical Employee schema. It uses a LangGraph workflow and Gemini structured output for semantic schema mapping. The product is intentionally not an unconstrained autonomous agent: it is an executor/evaluator workflow with a deterministic policy router and resumable human decisions.

`ingest → profile → Gemini mapping agent → evaluator → human interrupt if needed → normalize/dedupe → policy gate → mock target push → retry or rollback`

The web UI accepts multiple source files, displays graph activity, shows a focused review queue, reports per-record target outcomes, and retains an audit event trail. The target schema lives in `schemas/employee.schema.json`, so target fields, required fields, aliases, and validation policy share one versioned contract.

## Autonomy boundary

The agent autonomously maps only high-confidence Gemini proposals (>=0.85), normalizes whitespace/case/email, parses recognized dates, and merges obvious duplicate rows using email. The evaluator checks that mapped fields exist in the schema and that sensitive source fields are quarantined. The agent escalates a low-confidence/unknown field, an ambiguous header, an unexpected sensitive column, or a record that cannot pass deterministic validation. Target writes are only idempotent upserts and require zero unresolved reviews plus required-field and email checks. Retry is bounded; rollback is explicit and batch-scoped.

This boundary is deliberate: it minimizes implementation-consultant work while ensuring that the risky judgments remain visible and reversible.

## PII and operational safety

The Gemini mapping prompt receives headers/schema only, never raw employee rows. LangSmith receives sanitized stage metadata rather than raw PII. Uploaded files are type- and size-validated, stored outside the static frontend, and excluded from Git. External writes are mediated by deterministic policy rather than model output. Production configuration requires exact CORS origins, secret-managed Gemini/LangSmith keys, and persistent database/object-store/checkpointer replacements for local SQLite/in-memory services.

## Evaluation

The repository contains a 25-case mapping dataset covering recognized aliases, unknown fields, abbreviations, and sensitive fields. `python -m evals.run_evals` invokes Gemini for the agent proposal and a second Gemini structured model grader. Its JSON report exposes each input, expected mapping/route, actual mapping/route, grader rationale, individual score, and final average. Deterministic score is retained alongside the model score as a safety cross-check. A no-network offline run scored 1.000 across 25 cases; this is a smoke baseline, not a claim about model quality. The model-graded score must be generated from the configured project and reported from the resulting artifact; a quota error is treated as a failed evaluation, never a passing score.

## What I would build next

Replace local SQLite/in-memory graph checkpoints with Postgres, place uploads in encrypted object storage with retention controls, add tenant RBAC/authentication, stream node events over SSE, support richer record-level duplicate review, and make evaluation thresholds a protected CI deployment gate. I would also record reviewer corrections as labeled dataset additions to continuously improve mapping quality.
