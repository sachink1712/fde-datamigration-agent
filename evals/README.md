# Migration-agent evals

Scores every agent on a labelled dataset instead of "it works": per-case score, per-suite score, one final average.

## Run (from `backend/`)

```bash
pip install -r requirements.txt            # nothing extra needed
echo "GEMINI_API_KEY=..." >> .env          # or export it

python -m evals.run_evals --mode live --min-interval 6   # real Gemini, all 4 suites (~90 calls; 6s spacing is safe on the free tier)
python -m evals.run_evals --mode deterministic           # no key: cleaner (fallback plan) + validator only
python -m evals.run_evals --mode mock                    # harness self-test (scripted oracle) - NOT a Gemini score
python -m evals.run_evals --mode mock --mock-noise 0.25  # proves the metric drops when the agents get worse

python -m evals.run_evals --only classifier,cleaner      # subset
python -m evals.run_evals --case CL07                    # one case
python -m evals.run_evals --min-score 0.85               # exit 1 below threshold (CI gate)
```
Results: `evals/eval_results.json` (per-case scores, metrics, and details of every miss). `--mode auto` (default) = live if a key exists, else deterministic.

## What is scored (all scores 0-1)

| Suite | Cases | Measures | Case score |
|---|---|---|---|
| **Classifier** (`ColumnClassifierAgent`, one shot, no validator loop) | 12 files / 121 columns | header+values -> target field + transform; ignoring sensitive/unrelated columns; escalating genuinely ambiguous ones; prompt-injection resistance | mean column score: correct & confident **1.0**, correct but needlessly low-confidence **0.6**, wrong but low-confidence (safe failure) **0.3**, wrong & confident **0.0**; ambiguous column: **1.0** only if confidence < 0.85 |
| **Cleaner** (`CleanerAgent` + deterministic tools, gold mapping injected) | 8 files / 111 cells | tool selection and final cleaned values (dates, phones, salary, names, email, status, manager name->id); un-cleanable cells must be *flagged*, not guessed | `0.5 * tool-selection F1 + 0.5 * cell accuracy` (right value but needlessly flagged = 0.5) |
| **Validator** (`StrictValidatorAgent`) | 10 mapping-review + 15 record cases | catches wrong mappings and bad records without false alarms: placeholders, collisions, cycles, unknown managers, implausible dates/salaries, conflicts, missing required, waivers | F1 of flagged items vs gold (a clean batch flagging anything = 0) |
| **End-to-end** (whole LangGraph + push + rollback) | 3 multi-file scenarios (CSV, 2-sheet XLSX) | mapping accuracy, record field accuracy, **escalation boundary** precision/recall, push integrity (right ids pushed, no sensitive values leaked, rollback restores) | mean of those four |

`final_average_score` = unweighted mean of the four suite scores. Cases with `requires_llm` (semantic critic, VC09/VC10) run only in `--mode live`; skipped suites/cases are reported, and `complete=false` is set, so a partial run never masquerades as a full one.

## Dataset (`evals/datasets/`)
```
classifier/cases.json + files/*.csv       gold: per column {kind: map|ignore|escalate, target_field, transform}
cleaner/cases.json    + files/*.csv       gold: per employee/field expected value | null | {"flag": true}, required tools per field
validator/classification_review.json     proposed mapping + expected flagged columns
validator/record_validation.json          records + expected [type, subject, field] findings
e2e/scenarios.json    + e2e{1,2,3}_*/     source files + gold mapping, gold approved records, gold escalations, ids that must be pushed
```
Add a case by appending to the relevant JSON (and dropping a CSV in `files/`). Data is synthetic. Dates in the validator cases are far from the plausibility cut-offs so gold stays stable over time.

## Known findings the suite surfaces (real defects, not gold errors)
1. `normalize_status("Inactive")` fuzzy-matches to `active` at confidence 0.80, which is not `< CLEAN_MIN` (0.80), so an inactive employee is silently pushed as active (CLN08/E6, E2E3/G307). Fix: cap the fuzzy-match confidence at 0.75 or reject matches that flip meaning.
2. No upper bound on `annual_salary`; a 9,000,000,000 salary is accepted (V10/E72).
