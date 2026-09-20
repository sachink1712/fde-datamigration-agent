"""Scoring primitives. Every case score is in [0, 1]."""
from __future__ import annotations

from typing import Any

from backend.policies import CLASSIFY_MIN, CLEAN_MIN
from backend.checks import format_issue

# ------------------------------------------------------------------------------------------------ set metrics
def prf(pred: set, gold: set) -> tuple[float, float, float]:
    """precision, recall, F1. Both empty = perfect; one empty = 0."""
    if not pred and not gold:
        return 1.0, 1.0, 1.0
    tp = len(pred & gold)
    p, r = (tp / len(pred) if pred else 0.0), (tp / len(gold) if gold else 0.0)
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


# ------------------------------------------------------------------------------------------------ classifier
# Autonomy-aware column score: a confident mistake is the worst outcome (silent data corruption),
# an escalated mistake is a safe failure, a needless escalation costs the human some time.
SCORE_CORRECT_AUTONOMOUS, SCORE_CORRECT_ESCALATED, SCORE_WRONG_ESCALATED, SCORE_WRONG_AUTONOMOUS = 1.0, 0.6, 0.3, 0.0


def score_column(gold: dict, pred: dict | None) -> tuple[float, str]:
    if pred is None:
        return 0.0, "missing"
    target, transform, conf = pred.get("target_field"), pred.get("transform", "direct"), float(pred.get("confidence", 0))
    ignored, autonomous = target is None or transform == "ignore", conf >= CLASSIFY_MIN
    if gold["kind"] == "escalate":                       # genuinely ambiguous: the RIGHT behaviour is to hand it to a human
        return (SCORE_CORRECT_AUTONOMOUS, "escalated_as_expected") if not autonomous else (0.0, "overconfident_on_ambiguous")
    if gold["kind"] == "ignore":
        correct = ignored
    else:
        correct = (not ignored) and target == gold["target_field"] and transform == gold.get("transform", "direct")
    if correct:
        return (SCORE_CORRECT_AUTONOMOUS, "correct") if autonomous else (SCORE_CORRECT_ESCALATED, "correct_but_escalated")
    return (SCORE_WRONG_AUTONOMOUS, "wrong_confident") if autonomous else (SCORE_WRONG_ESCALATED, "wrong_but_escalated")


# ------------------------------------------------------------------------------------------------ cleaner
def tool_f1(plan_tools: list[str], required: list[str]) -> float:
    pred, gold = set(plan_tools) - {"trim_whitespace"}, set(required) - {"trim_whitespace"}
    return prf(pred, gold)[2]


def _num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def value_equal(a: Any, b: Any) -> bool:
    if _num(a) and _num(b):
        return abs(a - b) <= 0.005
    return a == b


def is_flagged(rec: dict, field: str) -> bool:
    """True when the pipeline would NOT let this cell through silently (low confidence, unresolved reference, or invalid format)."""
    cell = (rec.get("_cells") or {}).get(field) or {}
    if cell.get("flag") in {"unresolved_reference", "ambiguous_reference"}:
        return True
    if cell and float(cell.get("confidence", 1)) < CLEAN_MIN:
        return True
    return format_issue(field, rec.get(field)) is not None


def score_cell(rec: dict | None, field: str, expected: Any) -> tuple[float, Any]:
    if rec is None:
        return 0.0, "<record missing>"
    got = rec.get(field)
    if isinstance(expected, dict) and expected.get("flag"):
        return (1.0 if is_flagged(rec, field) else 0.0), got
    if expected is None:
        return (1.0 if got in (None, "") else 0.0), got
    if not value_equal(got, expected):
        return 0.0, got
    return (0.5 if is_flagged(rec, field) else 1.0), got      # right value but needlessly sent to a human = half credit
