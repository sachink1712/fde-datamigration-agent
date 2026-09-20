"""Deterministic evaluator checks. These are the final authority; the LLM critic can only add to them."""
from __future__ import annotations

import re
from collections import Counter as _Counter
from datetime import date, timedelta
from typing import Any

import re as _re

from .policies import CLEAN_MIN, COMPOUND_DEPT_LEGIT, DOMAIN_DOMINANCE, DOMAIN_EDIT_MAX, REQUIRED_FIELDS, TARGET_SCHEMA
from .tools import DATE_FIELDS, EMAIL_RE, FORMAT_TOOL, TOOLS

ENUM_STATUS = TARGET_SCHEMA["properties"]["employment_status"]["enum"]
E164_RE = re.compile(r"\+[1-9][0-9]{6,14}")
ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
PLACEHOLDER_DOMAINS = {"test.com", "example.com", "example.org", "example.net", "test.org", "mailinator.com", "invalid"}
PLACEHOLDER_WORDS = {"test", "dummy", "sample", "tbd", "xxx", "unknown", "na", "n/a", "none", "asdf"}
SENTINEL_DATES = {"1900-01-01", "1899-12-30", "0001-01-01", "1970-01-01", "9999-12-31"}
LEVEL_RE = re.compile(r"[A-Za-z]{1,3}[- ]?(\d{1,2})")

POLICY = {
    "placeholder_record": "Looks like a test/placeholder row. Reject it, or approve if it is a real employee.",
    "missing_required_value": "The agent never guesses a required value. Supply it (Correct) or withhold the record (Reject).",
    "source_conflict": "The source files disagree and both values are plausible. Pick the right one.",
    "identity_collision": "Approve to keep both (e.g. a rehire with a new id). Reject to withhold this one as a duplicate.",
    "manager_cycle": "Correct the manager id on this record (or leave it blank) to break the loop.",
    "unknown_manager": "The manager reference does not exist in any file. Supply the right id or leave blank.",
    "unresolved_reference": "The manager name matched nobody. Supply the manager's employee id.",
    "ambiguous_reference": "The manager name matches several people. Pick one.",
    "low_confidence_cleaning": "A cleaning tool could not clean this value with confidence. Correct it or reject the record.",
    "invalid_format": "Value is not in the target format after cleaning. Correct it or reject the record.",
    "invalid_value": "Value is impossible or implausible. Correct it or reject the record.",
    "implausible_value": "Value is implausible. Correct it, approve if genuine, or reject the record.",
    "ambiguous_salary_unit": "A bare small amount cannot be verified as an annual salary. Approve the suggested lakhs-per-annum reading, or correct it.",
    "foreign_currency_salary": "The salary is not in rupees. Conversion is never guessed: enter the annual amount in rupees, or reject the record.",
    "email_domain_outlier": "This email domain is one typo away from the domain everyone else uses. Approve the suggestion, or approve-as-is by correcting it to the same value.",
    "ambiguous_department": "This department names more than one team. Approve the suggestion, or enter the right department.",
}


def finding(type_: str, subject: str, field: str, feedback: str, *, severity: str = "error", retryable: bool = False,
            candidates: list[str] | None = None, value: Any = "", raw: Any = "", related: list[dict] | None = None,
            group: str | None = None, suggestion: dict | None = None, context: dict | None = None) -> dict[str, Any]:
    """`group` = root cause: findings of one record sharing a group become ONE card. `suggestion` = one-click fix for the human."""
    return {"key": f"{type_}:{subject}:{field}", "type": type_, "subject": subject, "field": field, "severity": severity,
            "feedback": feedback, "retryable": retryable, "candidates": candidates or [], "value": value, "raw_value": raw,
            "related": related or [], "group": group or field, "suggestion": suggestion, "context": context or {}}


def fix(label: str, **changes: Any) -> dict[str, Any]:
    return {"label": label, "action": None, "changes": [{"field": f, "to": str(v)} for f, v in changes.items()]}


def id_key(i: str) -> list:
    return [int(t) if t.isdigit() else t.lower() for t in _re.split(r"(\d+)", str(i)) if t]


def edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def comparison(members: list[dict]) -> dict[str, Any]:
    ids = [str(m["employee_id"]) for m in members]
    rows = []
    for f in TARGET_SCHEMA["properties"]:
        vals = {i: m.get(f, "") for i, m in zip(ids, members)}
        if any(v not in ("", None) for v in vals.values()):
            rows.append({"field": f, "values": vals, "differs": len({str(v) for v in vals.values()}) > 1})
    return {"records": ids, "fields": rows}


def format_issue(field: str, value: Any) -> str | None:
    if value in (None, ""):
        return None
    if field == "email" and not EMAIL_RE.fullmatch(str(value)):
        return "not a valid email address"
    if field == "phone" and not E164_RE.fullmatch(str(value)):
        return "not a valid E.164 phone number"
    if field in DATE_FIELDS:
        try:
            if not ISO_RE.fullmatch(str(value)):
                return "not an ISO YYYY-MM-DD date"
            date.fromisoformat(str(value))
        except ValueError:
            return "not a real calendar date"
    if field == "annual_salary" and (isinstance(value, bool) or not isinstance(value, (int, float))):
        return "not a number"
    if field == "employment_status" and value not in ENUM_STATUS:
        return f"not an allowed value ({', '.join(ENUM_STATUS)})"
    if field in {"first_name", "last_name"} and not re.search(r"[^\W\d_]", str(value)):
        return "not a name"
    return None


def intrinsic_issue(field: str, value: Any) -> str | None:
    """Context-free validity, used to auto-resolve a conflict when exactly one candidate is impossible."""
    if (msg := format_issue(field, value)):
        return msg
    if field == "annual_salary" and value is not None and value <= 0:
        return "salary must be greater than zero"
    return None


def placeholder_signals(rec: dict[str, Any]) -> list[str]:
    out = []
    email = str(rec.get("email") or "")
    if email and (email.rsplit("@", 1)[-1] in PLACEHOLDER_DOMAINS or email.split("@")[0] in PLACEHOLDER_WORDS):
        out.append("placeholder email")
    if any(str(rec.get(f) or "").casefold() in PLACEHOLDER_WORDS for f in ("first_name", "last_name")):
        out.append("placeholder name")
    digits = re.sub(r"\D", "", str(rec.get("_raw_phone") or rec.get("phone") or ""))
    if digits and re.fullmatch(r"(\d)\1{6,}", digits):
        out.append("repeated-digit phone")
    if any(str(rec.get(f) or "") in SENTINEL_DATES for f in DATE_FIELDS):
        out.append("sentinel date")
    if rec.get("annual_salary") == 0:
        out.append("zero salary")
    return out


def _level(rec: dict) -> int | None:
    for v in (rec.get("_extra") or {}).values():
        if m := LEVEL_RE.fullmatch(str(v).strip()):
            return int(m[1])
    return None


def validate_records(records: list[dict[str, Any]], plan: dict[str, list[str]], unmapped_required: set[str], waived: set[str] | frozenset = frozenset(), today: date | None = None) -> list[dict[str, Any]]:
    today = today or date.today()
    out: list[dict[str, Any]] = []
    ids = {str(r["employee_id"]).upper() for r in records if r.get("employee_id")}
    by_id = {str(r["employee_id"]): r for r in records if r.get("employee_id")}

    def rid(r: dict) -> str:
        return str(r.get("employee_id") or r["_source_row"])

    for r in records:
        s = rid(r)
        signals = placeholder_signals(r)
        if len(signals) >= 2 and f"placeholder_record:{s}:record" not in waived:
            out.append(finding("placeholder_record", s, "record", f"{s} looks like a test/placeholder record ({', '.join(signals)}).", value=r.get("email", "")))
            continue
        for f in sorted(REQUIRED_FIELDS - unmapped_required):
            if r.get(f) in (None, ""):
                out.append(finding("missing_required_value", s, f, f"Required field {f} is blank in the source data; it is not guessed.", raw=r.get(f"_raw_{f}", "")))
        for f, conflict in (r.get("_conflicts") or {}).items():
            vals = [str(c["value"]) for c in conflict]
            out.append(finding("source_conflict", s, f, f"Sources disagree on {f}: " + " vs ".join(f"{c['value']} ({c['source']})" for c in conflict) + ".",
                               candidates=vals, value=r.get(f, ""), related=conflict))
        for f, cell in (r.get("_cells") or {}).items():
            if f in (r.get("_conflicts") or {}) or r.get(f) in (None, ""):
                continue
            tool = FORMAT_TOOL.get(f)
            retry = bool(tool and tool not in plan.get(f, []))
            msg = format_issue(f, r.get(f))
            flag = cell.get("flag")
            if flag == "ambiguous_salary_unit":
                out.append(finding(flag, s, f, cell["note"], value=r.get(f), raw=cell["raw"],
                                   suggestion=fix(f"Treat {cell['raw']} as {cell['raw']} LPA -> {int(cell['suggestion']):,}", **{f: cell["suggestion"]})))
            elif flag == "foreign_currency":
                out.append(finding("foreign_currency_salary", s, f, cell["note"], value=r.get(f), raw=cell["raw"]))
            elif flag in {"unresolved_reference", "ambiguous_reference"}:
                out.append(finding(flag, s, f, cell["note"], candidates=cell.get("candidates", []), value=r.get(f), raw=cell["raw"]))
            elif msg:
                out.append(finding("invalid_format", s, f, f"{f} '{r.get(f)}' is {msg}.", retryable=retry, candidates=ENUM_STATUS if f == "employment_status" else [], value=r.get(f), raw=cell["raw"]))
            elif cell["confidence"] < CLEAN_MIN and flag != "dangling_reference":
                out.append(finding("low_confidence_cleaning", s, f, f"{f} '{cell['raw']}': {cell['note'] or 'low confidence'} (confidence {cell['confidence']:.2f}).",
                                   candidates=cell.get("candidates", []), value=r.get(f), raw=cell["raw"]))
        # fields with a format tool but no cell record (tool skipped): plain format check
        for f in FORMAT_TOOL:
            if f in (r.get("_conflicts") or {}) or r.get(f) in (None, "") or f in (r.get("_cells") or {}):
                continue
            if f == "manager_employee_id":
                if str(r[f]).upper() not in ids and "resolve_employee_reference" not in plan.get(f, []):
                    out.append(finding("invalid_format", s, f, f"manager '{r[f]}' is not an employee id; names must be resolved to ids.", retryable=True, value=r[f]))
                continue
            if (msg := format_issue(f, r.get(f))):
                out.append(finding("invalid_format", s, f, f"{f} '{r.get(f)}' is {msg}.", retryable=f in FORMAT_TOOL and FORMAT_TOOL[f] not in plan.get(f, []), value=r.get(f)))
        # domain plausibility
        sal = r.get("annual_salary")
        if isinstance(sal, (int, float)) and sal <= 0:
            out.append(finding("invalid_value", s, "annual_salary", f"annual_salary {sal:,.0f} is not a valid salary.", value=sal))
        dob, start, end = (r.get(k) for k in DATE_FIELDS)
        if dob and not format_issue("date_of_birth", dob) and not (date(1920, 1, 1) <= date.fromisoformat(dob) <= today - timedelta(days=16 * 365)):
            out.append(finding("implausible_value", s, "date_of_birth", f"date_of_birth {dob} is implausible.", value=dob, group="dates"))
        if start and not format_issue("start_date", start) and not (date(1950, 1, 1) <= date.fromisoformat(start) <= today + timedelta(days=365)):
            out.append(finding("implausible_value", s, "start_date", f"start_date {start} is implausible.", value=start, group="dates"))
        if start and end and not format_issue("start_date", start) and not format_issue("end_date", end) and end < start:
            out.append(finding("implausible_value", s, "end_date", f"end_date {end} is before start_date {start}.", value=end, group="dates"))
        if start and dob and not format_issue("start_date", start) and not format_issue("date_of_birth", dob) and date.fromisoformat(start) < date.fromisoformat(dob) + timedelta(days=14 * 365):
            out.append(finding("implausible_value", s, "start_date", f"start_date {start} is less than 14 years after date_of_birth {dob}.", value=start, group="dates"))

    # ---- cross-record: identity collisions
    live = [r for r in records if r.get("employee_id") and (len(placeholder_signals(r)) < 2 or f"placeholder_record:{r['employee_id']}:record" in waived)]
    groups: dict[tuple, list[dict]] = {}
    for r in live:
        if r.get("email"):
            groups.setdefault(("email", r["email"]), []).append(r)
        if r.get("date_of_birth") and r.get("first_name") and r.get("last_name"):
            groups.setdefault(("dob+name", r["date_of_birth"], str(r["first_name"]).casefold(), str(r["last_name"]).casefold()), []).append(r)
    seen: set[str] = set()
    for key, members in groups.items():
        if len({m["employee_id"] for m in members}) < 2:
            continue
        members = sorted(members, key=lambda m: (m.get("start_date") or "9999", m["employee_id"]))
        rel = [{"employee_id": m["employee_id"], "start_date": m.get("start_date", ""), "employment_status": m.get("employment_status", "")} for m in members]
        for m in members[1:]:
            if m["employee_id"] in seen:
                continue
            seen.add(m["employee_id"])
            others = ", ".join(x["employee_id"] for x in members if x is not m)
            other = next(x for x in members if x is not m)
            survivor, merged = sorted([m["employee_id"], other["employee_id"]], key=id_key)
            sugg = {"label": f"Merge {merged} into {survivor} (lower ID survives)", "action": "merge", "survivor": survivor, "merged": merged, "changes": []}
            out.append(finding("identity_collision", m["employee_id"], "employee_id", f"{m['employee_id']} has the same {'email' if key[0]=='email' else 'name and date of birth'} as {others}: possible rehire or duplicate.",
                               value=m["employee_id"], related=rel, suggestion=sugg, context={"comparison": comparison([m, other])}))

    # ---- cross-record: email domain outliers and compound departments (verified against the rest of the file)
    with_email = [r for r in live if r.get("email") and "@" in str(r["email"]) and "email" not in (r.get("_conflicts") or {})]
    domains = _Counter(str(r["email"]).rsplit("@", 1)[-1] for r in with_email)
    if domains:
        dominant, n_dom = domains.most_common(1)[0]
        if n_dom >= 3 and n_dom / len(with_email) >= DOMAIN_DOMINANCE:
            for r in with_email:
                dom = str(r["email"]).rsplit("@", 1)[-1]
                if dom != dominant and (d := edit_distance(dom, dominant)) <= DOMAIN_EDIT_MAX:
                    local = str(r["email"]).rsplit("@", 1)[0]
                    out.append(finding("email_domain_outlier", rid(r), "email", f"Email domain '{dom}' differs from '{dominant}' (used by {n_dom} of {len(with_email)} records) by {d} character(s): possible typo.",
                                       value=r["email"], raw=r.get("_raw_email", ""), suggestion=fix(f"Change to {local}@{dominant}", email=f"{local}@{dominant}")))
    depts = _Counter(str(r["department"]) for r in live if r.get("department"))
    for r in live:
        dept = str(r.get("department") or "")
        parts = [p.strip() for p in _re.split(r"\s+(?:&|and|\+)\s+|\s*/\s*|\s*,\s*", dept, flags=_re.I) if p.strip()]
        if len(parts) < 2 or any(len(_re.findall(r"[^\W\d_]", p)) < 2 for p in parts) or depts[dept] / max(sum(depts.values()), 1) >= COMPOUND_DEPT_LEGIT:
            continue
        title = str(r.get("job_title") or "").casefold()
        hit = [p for p in parts if p.casefold() in title]
        out.append(finding("ambiguous_department", rid(r), "department", f"Department '{dept}' names more than one team" + (f"; job title '{r.get('job_title')}' implies '{hit[0]}'." if len(hit) == 1 else " and the job title does not settle which."),
                           value=dept, raw=r.get("_raw_department", ""), candidates=parts,
                           suggestion=fix(f"Use '{hit[0]}' (implied by job title '{r.get('job_title')}')", department=hit[0]) if len(hit) == 1 else None))

    # ---- manager references and cycles
    mgr = {str(r["employee_id"]): str(r["manager_employee_id"]) for r in live if r.get("manager_employee_id") and "manager_employee_id" not in (r.get("_conflicts") or {})}
    canon = {k.upper(): k for k in by_id}
    for e, m in mgr.items():
        if m.upper() not in canon:
            out.append(finding("unknown_manager", e, "manager_employee_id", f"Manager {m} of {e} does not exist in any source file.", value=m))
    in_cycle: set[str] = set()
    for start_id in mgr:
        path, cur = [], start_id
        while cur in mgr and cur not in path:
            path.append(cur)
            cur = canon.get(mgr[cur].upper(), mgr[cur])
        if cur in path:
            in_cycle |= set(path[path.index(cur):])
    for e in sorted(in_cycle):
        chain = [e]
        while (nxt := canon.get(mgr[chain[-1]].upper())) and nxt not in chain:
            chain.append(nxt)
        out.append(finding("manager_cycle", e, "manager_employee_id", f"Reporting cycle: {' -> '.join(chain + [chain[0]])}.",
                           value=mgr[e], related=[{"employee_id": c, "manager": mgr.get(c, "")} for c in chain]))

    # dedupe: one error per (record, field)
    deduped, seen_keys = [], set()
    for f in out:
        k = (f["subject"], f["field"])
        if k not in seen_keys:
            seen_keys.add(k)
            deduped.append(f)
    return deduped


def soft_flags(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Warnings only: never block a record. Manager with a lower level than their report."""
    by_id = {str(r["employee_id"]): r for r in records if r.get("employee_id")}
    flags = []
    for r in records:
        m = by_id.get(str(r.get("manager_employee_id") or ""))
        lr, lm = _level(r), (_level(m) if m else None)
        if m and lr is not None and lm is not None and lm < lr:
            flags.append({"type": "hierarchy_oddity", "subject": str(r["employee_id"]), "field": "manager_employee_id",
                          "message": f"{r['employee_id']} (level {lr}) reports to {m['employee_id']} (level {lm}), a lower level."})
    return flags


# --------------------------------------------------------------------------- column-shape evidence (used to police the classifier)
def shape_score(target: str, values: list[str]) -> float:
    """Share of a column's non-empty values that plausibly belong to the proposed target field."""
    from .tools import Cell, parse_amount, parse_date
    vals = [v for v in values if v]
    if not vals:
        return 1.0

    def ok(v: str) -> bool:
        if target == "email":
            return "@" in v
        if target == "phone":
            return len(re.sub(r"\D", "", v)) >= 7 and not re.search(r"[A-Za-z]{3,}", v)
        if target in DATE_FIELDS:
            return parse_date(v, True)[0] is not None or parse_date(v, False)[0] is not None
        if target == "annual_salary":
            return parse_amount(Cell(v), {}).confidence >= 0.5
        if target == "employment_status":
            return bool(re.fullmatch(r"[A-Za-z][A-Za-z _\-/]*", v))
        if target in ("first_name", "last_name"):
            return bool(re.search(r"[^\W\d_]{2,}", v))
        if target in ("job_title", "department", "location"):
            return bool(re.search(r"[^\W\d_]{3,}", v)) and not re.fullmatch(r"[A-Za-z]{0,3}[-_ ]?\d+[A-Za-z]?", v)
        return True
    return sum(ok(v) for v in vals) / len(vals)
