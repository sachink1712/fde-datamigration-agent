"""The four eval suites. Each returns a list of case dicts: {id, name, description, score, metrics, details, [error|skipped]}."""
from __future__ import annotations

import json
import os
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from backend.agents import CleanerAgent, ColumnClassifierAgent, LLMProvider, StrictValidatorAgent
from backend.engine import MigrationEngine
from backend.graph import MigrationGraph
from backend.pipeline import column_key
from backend.store import Store
from backend.tools import safe_plan

from . import metrics as m

DS = Path(__file__).parent / "datasets"


@dataclass
class Ctx:
    llm: LLMProvider
    tmp: Path
    mode: str                       # live | mock | deterministic
    only_case: str | None = None

    @property
    def llm_on(self) -> bool:
        return self.llm.available

    def engine(self) -> MigrationEngine:
        return MigrationEngine(Store(str(self.tmp / f"{uuid.uuid4().hex}.db")))


def _load(*parts: str) -> Any:
    return json.loads((DS.joinpath(*parts)).read_text(encoding="utf-8"))


def _select(cases: list[dict], ctx: Ctx) -> list[dict]:
    return [c for c in cases if not ctx.only_case or c["id"] == ctx.only_case]


def _guard(case: dict, fn: Callable[[], dict]) -> dict:
    """A crashing case scores 0 and records why: it must never abort the whole run."""
    try:
        return {"id": case["id"], "name": case["name"], "description": case.get("description", ""), **fn()}
    except Exception as error:  # noqa: BLE001
        return {"id": case["id"], "name": case["name"], "description": case.get("description", ""), "score": 0.0, "metrics": {}, "details": [],
                "error": f"{type(error).__name__}: {error}"}


def _skipped(case: dict, why: str) -> dict:
    return {"id": case["id"], "name": case["name"], "description": case.get("description", ""), "skipped": True, "reason": why, "score": None, "metrics": {}, "details": []}


# ======================================================================================================== 1. CLASSIFIER
def classifier_gold_lookup() -> dict[tuple[str, str], dict]:
    gold: dict[tuple[str, str], dict] = {}
    for c in _load("classifier", "cases.json"):
        for header, g in c["columns"].items():
            gold[(c["file"], header)] = g
    for s in _load("e2e", "scenarios.json"):
        for col in s["mapping"]:
            gold[(col["file"], col["header"])] = {k: v for k, v in col.items() if k not in {"file", "header"}}
    return gold


def classifier_suite(ctx: Ctx) -> list[dict]:
    agent, out = ColumnClassifierAgent(ctx.llm), []
    for case in _select(_load("classifier", "cases.json"), ctx):
        def run_case(case=case) -> dict:
            engine = ctx.engine()
            run = engine.create_run([{"name": case["file"], "path": str(DS / "classifier" / "files" / case["file"])}])
            engine.ingest(run)
            items, error = agent.generate(run["profiles"], {}, {})          # ONE shot, no validator loop: this scores the generator itself
            preds = {column_key(i["source_file"], i["source_header"]): i for i in items}
            rows = []
            for header, gold in case["columns"].items():
                pred = preds.get(column_key(case["file"], header))
                score, outcome = m.score_column(gold, pred)
                rows.append({"header": header, "gold": gold.get("target_field") if gold["kind"] != "ignore" else "<ignore>", "gold_kind": gold["kind"],
                             "gold_transform": gold.get("transform"), "pred": (pred or {}).get("target_field") if pred and pred.get("transform") != "ignore" else "<ignore>",
                             "pred_transform": (pred or {}).get("transform"), "confidence": (pred or {}).get("confidence"), "outcome": outcome, "score": score})
            n = len(rows)
            exact = [r for r in rows if r["gold_kind"] != "escalate"]
            autos = [r for r in rows if r["confidence"] is not None and r["confidence"] >= 0.85]
            met = {"columns": n, "accuracy": round(m.mean([r["outcome"] in ("correct", "correct_but_escalated") for r in exact]), 3) if exact else None,
                   "confident_error_rate": round(sum(r["outcome"] in ("wrong_confident", "overconfident_on_ambiguous") for r in autos) / len(autos), 3) if autos else 0.0,
                   "outcomes": dict(Counter(r["outcome"] for r in rows))}
            return {"score": round(m.mean([r["score"] for r in rows]), 4), "metrics": met, "details": rows, **({"error": error} if error else {})}
        out.append(_guard(case, run_case))
    return out


# ======================================================================================================== 2. CLEANER
def cleaner_suite(ctx: Ctx) -> list[dict]:
    agent, out = CleanerAgent(ctx.llm), []
    for case in _select(_load("cleaner", "cases.json"), ctx):
        def run_case(case=case) -> dict:
            engine = ctx.engine()
            run = engine.create_run([{"name": case["file"], "path": str(DS / "cleaner" / "files" / case["file"])}])
            engine.ingest(run)
            for p in run["profiles"]:                                   # inject GOLD classification: isolates the cleaner from the classifier
                g = case["mapping"].get(p["header"])
                run["classifications"].append({"key": p["key"], "source_file": p["file"], "source_header": p["header"], "target_field": g["target_field"] if g else None,
                                               "transform": g["transform"] if g else "ignore", "confidence": 1.0, "rationale": "gold", "alternatives": [], "status": "accepted" if g else "ignored"})
            fields = engine.plannable_fields(run)
            plan, error = agent.generate(fields, {})
            run["cleaning_plan"] = plan                                 # raw agent output, no ensure_plan() rescue
            engine.build(run)
            by_plan = {p["target_field"]: p["tools"] for p in plan}
            tool_rows = []
            for f in fields:
                req = case["required_tools"].get(f["target_field"], [])
                got = by_plan.get(f["target_field"], [])
                tool_rows.append({"field": f["target_field"], "required": req, "planned": got, "f1": round(m.tool_f1(got, req), 3)})
            by_emp = {r["employee_id"]: r for r in run["records"] if r.get("employee_id")}
            cells = []
            for emp, exp in case["expected"].items():
                for field, want in exp.items():
                    score, got = m.score_cell(by_emp.get(emp), field, want)
                    cells.append({"employee_id": emp, "field": field, "expected": "<flag for human>" if isinstance(want, dict) else want, "got": got, "score": score})
            tool_score, cell_score = m.mean([t["f1"] for t in tool_rows]), m.mean([c["score"] for c in cells])
            fam: dict[str, list[float]] = {}
            for c in cells:
                fam.setdefault(c["field"], []).append(c["score"])
            met = {"tool_selection_f1": round(tool_score, 3), "cleaned_value_accuracy": round(cell_score, 3), "cells": len(cells),
                   "by_field": {k: round(m.mean(v), 3) for k, v in sorted(fam.items())}, "plan_source": "llm" if ctx.llm_on else "deterministic-fallback"}
            return {"score": round(0.5 * tool_score + 0.5 * cell_score, 4), "metrics": met, "details": {"tools": tool_rows, "cells": [c for c in cells if c["score"] < 1.0]},
                    **({"error": error} if error else {})}
        out.append(_guard(case, run_case))
    return out


# ======================================================================================================== 3. VALIDATOR (evaluator agent)
def validator_suite(ctx: Ctx) -> list[dict]:
    validator, out = StrictValidatorAgent(ctx.llm), []
    critic_ok = ctx.mode == "live"                                       # the semantic critic can only be judged against a real LLM
    for case in _select(_load("validator", "classification_review.json"), ctx):
        if case["requires_llm"] and not critic_ok:
            out.append(_skipped(case, "needs the live LLM critic (semantic review)")); continue
        def run_case(case=case) -> dict:
            file, headers = case["file"], list(case["columns"])
            n = max(len(v) for v in case["columns"].values())
            run = {"profiles": [{"key": column_key(file, h), "file": file, "header": h, "samples": list(dict.fromkeys(case["columns"][h]))[:3]} for h in headers],
                   "source_rows": [{"_source": file, **{h: case["columns"][h][i] for h in headers}} for i in range(n)],
                   "classifications": [{"key": column_key(file, h), "source_file": file, "source_header": h, "rationale": "proposed", "alternatives": [], **case["proposed"][h]} for h in headers]}
            issues = validator.review_classification(run)
            flagged = {k.split("::", 1)[1] for k in issues}
            p, r, f1 = m.prf(flagged, set(case["expected_flagged"]))
            return {"score": round(f1, 4), "metrics": {"precision": round(p, 3), "recall": round(r, 3), "kind": "classification_review"},
                    "details": {"expected_flagged": case["expected_flagged"], "flagged": sorted(flagged), "missed": sorted(set(case["expected_flagged"]) - flagged),
                                "false_alarms": sorted(flagged - set(case["expected_flagged"])), "reasons": {k.split("::", 1)[1]: v for k, v in issues.items()}}}
        out.append(_guard(case, run_case))
    for case in _select(_load("validator", "record_validation.json"), ctx):
        def run_case(case=case) -> dict:
            plan = case["plan"] if case.get("plan") is not None else {f: safe_plan(f) for f in ["email","phone","first_name","last_name","date_of_birth","start_date","end_date","annual_salary","employment_status","manager_employee_id"]}
            found = validator.review_records({"unmapped_required": case["unmapped_required"], "waivers": case["waivers"]}, case["records"], plan)
            got = {(f["type"], f["subject"], f["field"]) for f in found if f["severity"] == "error"}
            want = {tuple(t) for t in case["expected"]}
            p, r, f1 = m.prf(got, want)
            return {"score": round(f1, 4), "metrics": {"precision": round(p, 3), "recall": round(r, 3), "expected": len(want), "reported": len(got), "kind": "record_validation"},
                    "details": {"missed": sorted(want - got), "false_alarms": sorted(got - want)}}
        out.append(_guard(case, run_case))
    return out


# ======================================================================================================== 4. END-TO-END (whole graph)
def e2e_suite(ctx: Ctx) -> list[dict]:
    out = []
    for sc in _select(_load("e2e", "scenarios.json"), ctx):
        def run_case(sc=sc) -> dict:
            engine = ctx.engine()
            graph = MigrationGraph(engine, ctx.llm)
            files = [{"name": Path(f).name, "path": str(DS / "e2e" / f), "size_bytes": "0"} for f in sc["files"]]
            run = graph.start(files)
            # ---- 1. mapping
            classes = {c["key"]: c for c in run["classifications"]}
            map_rows = []
            for col in sc["mapping"]:
                c = classes.get(column_key(col["file"], col["header"]))
                if col["kind"] == "map":
                    ok = bool(c) and c["status"] == "accepted" and c.get("target_field") == col["target_field"] and c["transform"] == col.get("transform", "direct")
                elif col["kind"] == "ignore":
                    ok = bool(c) and c["status"] == "ignored"
                else:
                    ok = bool(c) and c["status"] == "needs_review"
                map_rows.append({"file": col["file"], "header": col["header"], "expected": col["kind"], "status": c["status"] if c else "missing", "target": (c or {}).get("target_field"), "ok": ok})
            mapping_score = m.mean([r["ok"] for r in map_rows])
            # ---- 2. record accuracy (auto-approved records only)
            recs = {r["employee_id"]: r for r in run["records"] if r.get("employee_id")}
            cell_rows = []
            for gold in sc["approved_records"]:
                rec = recs.get(gold["employee_id"])
                approved = rec is not None and rec.get("_status") == "approved"
                for f, want in gold.items():
                    got = rec.get(f) if rec else None
                    ok = approved and m.value_equal(got, want)
                    cell_rows.append({"employee_id": gold["employee_id"], "field": f, "expected": want, "got": got, "approved": approved, "ok": ok})
            record_score = m.mean([r["ok"] for r in cell_rows])
            # ---- 3. escalation boundary
            def key(e: dict) -> tuple[str, str]:
                return (e["record_id"], e["field"]) if e["scope"] == "record" else ("column", e["field"])
            got_esc = {key(e) for e in run["escalations"] if e["status"] == "open"}
            want_esc = {(e["subject"], e["field"]) for e in sc["expected_escalations"]}
            p, r, f1 = m.prf(got_esc, want_esc)
            # ---- 4. push integrity + rollback
            after_push = graph.execute(run["id"], "push")
            target = engine.store.target_records()
            pushed = {t["employee_id"] for t in target}
            blob = json.dumps(target, ensure_ascii=False)
            leaked = [v for v in sc["forbidden_in_target"] if v in blob]
            pushed_ok = pushed == set(sc["pushed_ids"])
            graph.execute(run["id"], "rollback")
            rolled_ok = engine.store.target_records() == []
            integrity = m.mean([pushed_ok, not leaked, rolled_ok])
            events = {"retries": len(run.get("retry_log", []))}
            score = m.mean([mapping_score, record_score, f1, integrity])
            return {"score": round(score, 4),
                    "metrics": {"mapping_accuracy": round(mapping_score, 3), "record_field_accuracy": round(record_score, 3), "escalation_precision": round(p, 3), "escalation_recall": round(r, 3),
                                "escalation_f1": round(f1, 3), "push_integrity": round(integrity, 3), "pushed_set_correct": pushed_ok, "sensitive_values_leaked": len(leaked),
                                "rollback_clean": rolled_ok, "final_status": after_push["status"], "validator_retry_rounds": events["retries"]},
                    "details": {"missed_escalations": sorted(want_esc - got_esc), "unneeded_escalations": sorted(got_esc - want_esc),
                                "wrong_mappings": [r for r in map_rows if not r["ok"]], "wrong_cells": [c for c in cell_rows if not c["ok"]][:40],
                                "unexpected_push_ids": sorted(pushed - set(sc["pushed_ids"])), "missing_push_ids": sorted(set(sc["pushed_ids"]) - pushed)}}
        out.append(_guard(sc, run_case))
    return out


SUITES: dict[str, Callable[[Ctx], list[dict]]] = {"classifier": classifier_suite, "cleaner": cleaner_suite, "validator": validator_suite, "e2e": e2e_suite}
LABELS = {"classifier": "Classifier agent (generator 1)", "cleaner": "Cleaner agent (generator 2)", "validator": "Strict validator (evaluator)", "e2e": "End-to-end graph"}
NEEDS_LLM = {"classifier", "e2e"}
