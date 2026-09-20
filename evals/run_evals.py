"""Run the migration-agent eval suite and write evals/eval_results.json.

    python -m evals.run_evals                      # live Gemini if GEMINI_API_KEY is set, else deterministic-only
    python -m evals.run_evals --mode live --min-interval 6
    python -m evals.run_evals --mode deterministic # no LLM: cleaner (fallback plan) + validator only
    python -m evals.run_evals --mode mock          # harness self-test with a scripted oracle (NOT a Gemini score)
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from . import metrics as m
from . import llm as llm_backends
from .suites import DS, LABELS, NEEDS_LLM, SUITES, Ctx, classifier_gold_lookup

ROOT = Path(__file__).resolve().parent
DEFAULT_OUT = ROOT / "eval_results.json"


def parse() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["auto", "live", "mock", "deterministic"], default="auto")
    ap.add_argument("--only", help="comma-separated suites: classifier,cleaner,validator,e2e")
    ap.add_argument("--case", help="run a single case id, e.g. CL07 or V06")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="results JSON path (default: evals/eval_results.json)")
    ap.add_argument("--pass-threshold", type=float, default=0.8, help="a case 'passes' at or above this score (default 0.8)")
    ap.add_argument("--min-score", type=float, default=None, help="exit with code 1 if the final average is below this (for CI)")
    ap.add_argument("--min-interval", type=float, default=0.0, help="min seconds between live LLM calls (use ~6 on the Gemini free tier)")
    ap.add_argument("--retries", type=int, default=3, help="live LLM retries on error/rate-limit")
    ap.add_argument("--mock-noise", type=float, default=0.0, help="mock mode only: fraction of answers the oracle deliberately gets wrong")
    ap.add_argument("--verbose", action="store_true", help="print every imperfect case's reasons")
    return ap.parse_args()


def brief(suite: str, c: dict) -> str:
    d = c.get("details")
    if c.get("error") and not d:
        return c["error"][:110]
    try:
        if suite == "classifier":
            bad = [r for r in d if r["score"] < 1]
            return "; ".join(f"{r['header']}: want {r['gold'] if r['gold_kind'] != 'escalate' else '<escalate>'}, got {r['pred']}@{r['confidence']}" for r in bad[:3])
        if suite == "cleaner":
            bad = d["cells"]
            return "; ".join(f"{r['employee_id']}.{r['field']}: want {r['expected']}, got {r['got']}" for r in bad[:3])
        if suite == "validator":
            return "; ".join(x for x in (f"missed {d.get('missed')}" if d.get("missed") else "", f"false alarms {d.get('false_alarms')}" if d.get("false_alarms") else "") if x)[:160]
        if suite == "e2e":
            return "; ".join(x for x in (f"missed esc {d['missed_escalations']}" if d["missed_escalations"] else "", f"extra esc {d['unneeded_escalations']}" if d["unneeded_escalations"] else "",
                                         f"{len(d['wrong_mappings'])} wrong mappings" if d["wrong_mappings"] else "", f"{len(d['wrong_cells'])} wrong cells" if d["wrong_cells"] else "") if x)[:200]
    except Exception:  # noqa: BLE001
        return ""
    return ""


def suite_metrics(suite: str, cases: list[dict]) -> dict:
    ok = [c for c in cases if not c.get("skipped") and not c.get("error")]
    if suite == "classifier":
        rows = [r for c in ok for r in c["details"]]
        exact = [r for r in rows if r["gold_kind"] != "escalate"]
        amb = [r for r in rows if r["gold_kind"] == "escalate"]
        autos = [r for r in rows if r["confidence"] is not None and r["confidence"] >= 0.85]
        return {"columns_scored": len(rows),
                "column_accuracy": round(m.mean([r["outcome"] in ("correct", "correct_but_escalated") for r in exact]), 4),
                "confident_error_rate": round(sum(r["outcome"] in ("wrong_confident", "overconfident_on_ambiguous") for r in autos) / len(autos), 4) if autos else 0.0,
                "ambiguous_columns_escalated": round(m.mean([r["outcome"] == "escalated_as_expected" for r in amb]), 4) if amb else None,
                "needless_escalation_rate": round(m.mean([r["outcome"] in ("correct_but_escalated", "wrong_but_escalated") for r in exact]), 4)}
    if suite == "cleaner":
        return {"tool_selection_f1": round(m.mean([c["metrics"]["tool_selection_f1"] for c in ok]), 4),
                "cleaned_value_accuracy": round(m.mean([c["metrics"]["cleaned_value_accuracy"] for c in ok]), 4), "cells_scored": sum(c["metrics"]["cells"] for c in ok)}
    if suite == "validator":
        tp = fp = fn = 0
        for c in ok:
            d = c["details"]
            miss, fa = len(d["missed"]), len(d["false_alarms"])
            exp = len(d["expected_flagged"]) if "expected_flagged" in d else c["metrics"]["expected"]
            tp += exp - miss; fn += miss; fp += fa
        p, r = (tp / (tp + fp) if tp + fp else 1.0), (tp / (tp + fn) if tp + fn else 1.0)
        return {"pooled_precision": round(p, 4), "pooled_recall": round(r, 4), "pooled_f1": round(2 * p * r / (p + r), 4) if p + r else 0.0, "expected_findings": tp + fn}
    if suite == "e2e":
        keys = ["mapping_accuracy", "record_field_accuracy", "escalation_precision", "escalation_recall", "push_integrity"]
        return {k: round(m.mean([c["metrics"][k] for c in ok]), 4) for k in keys} | {"sensitive_values_leaked": sum(c["metrics"]["sensitive_values_leaked"] for c in ok)}
    return {}


def main() -> int:
    a = parse()
    mode = a.mode
    if mode == "auto":
        mode = "live" if (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")) else "deterministic"
        print(f"[auto] mode -> {mode}" + ("" if mode == "live" else "  (no GEMINI_API_KEY: classifier + e2e will be skipped)"))
    if mode == "live":
        provider, stats = llm_backends.build_live(a.min_interval, a.retries)
    elif mode == "mock":
        provider, stats = llm_backends.build_mock(classifier_gold_lookup(), a.mock_noise)
    else:
        provider, stats = llm_backends.build_none()
    wanted = [s.strip() for s in a.only.split(",")] if a.only else list(SUITES)
    started = time.monotonic()
    results: dict[str, dict] = {}
    with tempfile.TemporaryDirectory() as tmp:
        ctx = Ctx(llm=provider, tmp=Path(tmp), mode=mode, only_case=a.case)
        for name in wanted:
            if name in NEEDS_LLM and not provider.available:
                results[name] = {"label": LABELS[name], "skipped": True, "reason": "requires an LLM (run with --mode live or --mode mock)", "score": None, "cases": []}
                continue
            print(f"running {name} ...", flush=True)
            cases = SUITES[name](ctx)
            scored = [c["score"] for c in cases if c.get("score") is not None]
            results[name] = {"label": LABELS[name], "skipped": not scored, "score": round(m.mean(scored), 4) if scored else None,
                             "cases_scored": len(scored), "cases_skipped": sum(bool(c.get("skipped")) for c in cases),
                             "cases_passed": sum(s >= a.pass_threshold for s in scored), "metrics": suite_metrics(name, cases) if scored else {}, "cases": cases}
    for r in results.values():
        for c in r["cases"]:
            c["passed"] = None if c.get("score") is None else c["score"] >= a.pass_threshold
    suite_scores = {k: v["score"] for k, v in results.items() if v["score"] is not None}
    all_cases = [c["score"] for v in results.values() for c in v["cases"] if c.get("score") is not None]
    final = round(m.mean(list(suite_scores.values())), 4) if suite_scores else None
    report = {
        "final_average_score": final,
        "final_average_definition": "unweighted mean of the per-suite scores; each suite score is the unweighted mean of its case scores",
        "mean_of_all_cases": round(m.mean(all_cases), 4) if all_cases else None,
        "suite_scores": suite_scores,
        "complete": set(suite_scores) == set(SUITES),
        "skipped_suites": {k: v["reason"] for k, v in results.items() if v.get("skipped") and v.get("reason")},
        "meta": {"timestamp": datetime.now(UTC).isoformat(timespec="seconds"), "mode": mode, "model": getattr(provider, "model_name", None) if mode != "deterministic" else None,
                 "harness_self_test_only": mode == "mock", "pass_threshold": a.pass_threshold, "cases_scored": len(all_cases), "duration_seconds": round(time.monotonic() - started, 1),
                 "python": platform.python_version(), **stats.as_dict()},
        "suites": results,
    }
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    # ------------------------------------------------------------------ console report
    print("\n" + "=" * 92)
    for name, r in results.items():
        if r.get("skipped") and not r["cases"]:
            print(f"\n{r['label']}: SKIPPED - {r['reason']}")
            continue
        print(f"\n{r['label']}   score {r['score']:.3f}   ({r['cases_passed']}/{r['cases_scored']} cases >= {a.pass_threshold})")
        for c in r["cases"]:
            if c.get("skipped"):
                print(f"  {c['id']:<6}{c['name'][:40]:<42}  skipped ({c['reason']})")
                continue
            flag = "PASS" if c["passed"] else "FAIL"
            print(f"  {c['id']:<6}{c['name'][:40]:<42}{c['score']:.3f}  {flag}" + (f"   {brief(name, c)}" if (c["score"] < 1 or a.verbose) and brief(name, c) else ""))
        print("  metrics:", json.dumps(r["metrics"]))
    print("\n" + "=" * 92)
    for k, v in suite_scores.items():
        print(f"  {LABELS[k]:<36}{v:.3f}")
    print(f"  {'FINAL AVERAGE SCORE':<36}{final if final is None else f'{final:.3f}'}   ({'all 4 suites' if report['complete'] else ('selected suites only' if a.only else 'PARTIAL: ' + ', '.join(report['skipped_suites']) + ' skipped')})")
    if mode == "mock":
        print("  NOTE: mock mode = harness self-test with a scripted oracle; these numbers do NOT measure Gemini.")
    if stats.calls:
        print(f"  LLM calls {stats.calls}, errors {stats.errors}, retries {stats.retries}")
    print(f"  results -> {out}")
    return 1 if (a.min_score is not None and (final is None or final < a.min_score)) else 0


if __name__ == "__main__":
    sys.exit(main())
