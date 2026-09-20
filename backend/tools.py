"""Deterministic cleaning tools (regex + phonenumbers). The Cleaner agent only *chooses* among them;
it never edits values itself, so it cannot invent data."""
from __future__ import annotations

import calendar
import difflib
import re
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any, Callable

import phonenumbers

from .policies import SMALL_SALARY_MAX

EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
DATE_FIELDS = ("date_of_birth", "start_date", "end_date")


@dataclass
class Cell:
    """One cleaned value plus provenance and the confidence the tool has in it."""
    value: Any
    raw: str = ""
    confidence: float = 1.0
    note: str = ""
    flag: str = ""                     # dangling_reference | unresolved_reference | ambiguous_reference
    derived: bool = False              # computed (e.g. monthly x 12) rather than copied
    candidates: list[str] = field(default_factory=list)
    source: str = ""
    suggestion: str = ""              # proposed replacement value for a human to approve in one click

    def to_dict(self) -> dict[str, Any]:
        return {"raw": self.raw, "confidence": round(self.confidence, 2), "note": self.note, "flag": self.flag, "source": self.source, "suggestion": self.suggestion}


def clean_text(value: str) -> str:
    value = unicodedata.normalize("NFC", value).replace("\u00a0", " ")
    return re.sub(r"\s+", " ", re.sub(r"[\u200b-\u200d\ufeff]", "", value)).strip()


def norm_name(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch)).casefold()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", value)).strip()


# --------------------------------------------------------------------------- dates
_MONTHS = {**{m.lower(): i for i, m in enumerate(calendar.month_abbr) if m},
           **{m.lower(): i for i, m in enumerate(calendar.month_name) if m}, "sept": 9}
_NUMERIC = re.compile(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})")
_ISO = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[ T].*)?")
_DMY_NAME = re.compile(r"(\d{1,2})[-/. ]+([A-Za-z]{3,9})[-/. ,]+(\d{4})")
_MDY_NAME = re.compile(r"([A-Za-z]{3,9})[-/. ]+(\d{1,2}),?[-/. ]+(\d{4})")


def infer_dayfirst(values: list[str]) -> bool | None | str:
    """Column/file-level evidence for dd/mm vs mm/dd. True, False, None (no evidence) or 'conflict'."""
    day_first = month_first = False
    for v in values:
        m = _NUMERIC.fullmatch(clean_text(str(v or "")))
        if m:
            a, b = int(m[1]), int(m[2])
            day_first |= a > 12 >= b
            month_first |= b > 12 >= a
    return "conflict" if day_first and month_first else True if day_first else False if month_first else None


def parse_date(value: str, dayfirst: bool | None | str = None) -> tuple[str | None, float, str]:
    """-> (iso or None, confidence, note)."""
    v = clean_text(value)
    try:
        if m := _ISO.fullmatch(v):
            return date(int(m[1]), int(m[2]), int(m[3])).isoformat(), 1.0, ""
        if m := _DMY_NAME.fullmatch(v):
            mon = _MONTHS.get(m[2].lower())
            return (date(int(m[3]), mon, int(m[1])).isoformat(), 1.0, "") if mon else (None, 0.2, f"unknown month '{m[2]}'")
        if m := _MDY_NAME.fullmatch(v):
            mon = _MONTHS.get(m[1].lower())
            return (date(int(m[3]), mon, int(m[2])).isoformat(), 1.0, "") if mon else (None, 0.2, f"unknown month '{m[1]}'")
        if m := _NUMERIC.fullmatch(v):
            a, b, y = int(m[1]), int(m[2]), int(m[3])
            if a > 12:
                return date(y, b, a).isoformat(), 0.98, "dd/mm (day > 12)"
            if b > 12:
                return date(y, a, b).isoformat(), 0.98, "mm/dd (day > 12)"
            if a == b or dayfirst is True:
                return date(y, b, a).isoformat(), 0.95 if a != b else 1.0, "dd/mm inferred from other dates in the file" if a != b else ""
            if dayfirst is False:
                return date(y, a, b).isoformat(), 0.95, "mm/dd inferred from other dates in the file"
            return None, 0.5, "ambiguous dd/mm vs mm/dd and nothing else in the file settles it"
    except ValueError:
        return None, 0.2, "not a real calendar date"
    return None, 0.2, "unrecognised date format"


# --------------------------------------------------------------------------- tools
def trim_whitespace(c: Cell, ctx: dict) -> Cell:
    if not isinstance(c.value, str):
        return c
    return replace(c, value=clean_text(c.value) or None)


def normalize_email(c: Cell, ctx: dict) -> Cell:
    if c.value is None:
        return c
    v = re.sub(r"\s+", "", str(c.value)).lower().removeprefix("mailto:")
    ok = bool(EMAIL_RE.fullmatch(v))
    return replace(c, value=v, confidence=min(c.confidence, 0.98 if ok else 0.3), note=c.note or ("" if ok else "not a valid email address"))


def normalize_phone(c: Cell, ctx: dict) -> Cell:
    if c.value is None:
        return c
    s = str(c.value)
    try:
        num = phonenumbers.parse(s, ctx.get("region", "IN"))
    except phonenumbers.NumberParseException:
        return replace(c, confidence=min(c.confidence, 0.3), note="unparseable phone number")
    if phonenumbers.is_valid_number(num):
        return replace(c, value=phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164), confidence=min(c.confidence, 0.95))
    return replace(c, confidence=min(c.confidence, 0.3), note="not a valid phone number")


def smart_case(s: str) -> str:
    """Fix ALL-CAPS / all-lower names only; mixed-case input (McDonald, de Souza, Siobhán) is left alone."""
    if not (s.isupper() or s.islower()):
        return s
    return re.sub(r"[^\W\d_]+", lambda m: m.group(0).capitalize(), s.lower())


def normalize_name(c: Cell, ctx: dict) -> Cell:
    if c.value is None:
        return c
    v = smart_case(clean_text(str(c.value)))
    if re.search(r"\d", v):
        return replace(c, value=v, confidence=min(c.confidence, 0.4), note="name contains digits")
    return replace(c, value=v)


def normalize_date(c: Cell, ctx: dict) -> Cell:
    if c.value is None:
        return c
    iso, conf, note = parse_date(str(c.value), ctx.get("dayfirst"))
    if iso is None:
        return replace(c, confidence=min(c.confidence, conf), note=note)
    return replace(c, value=iso, confidence=min(c.confidence, conf), note=note or c.note)


_FOREIGN = re.compile(r"[$€£]|\b(usd|eur|gbp|aed|sgd|cad|aud)\b", re.I)
_AMOUNT = re.compile(r"([\d,]+(?:\.\d+)?)\s*(lpa|lakhs?|lacs?|l)?(?:\s*p\.?a\.?)?", re.I)
LAKH = 100_000


def parse_amount(c: Cell, ctx: dict) -> Cell:
    """Deterministic salary parsing. Auto-fixes: rupee symbol/Rs/INR, comma and lakh grouping, '16 LPA', '16 lakh', '1.5L'.
    Escalates (never auto-fixes): foreign currency, and bare small numbers whose unit is ambiguous. Negatives stay negative for the validator."""
    if c.value is None or isinstance(c.value, (int, float)):
        return c
    raw = str(c.value)
    if _FOREIGN.search(raw):
        return replace(c, confidence=min(c.confidence, 0.3), flag="foreign_currency", note=f"'{raw}' is not in rupees; currency conversion is never guessed")
    s = re.sub(r"(?i)\b(rs|inr)\b\.?|₹|/-", "", raw).strip()
    neg = (s.startswith("(") and s.endswith(")")) or s.startswith("-")
    m = _AMOUNT.fullmatch(s.strip("()- ").strip())
    if not m:
        return replace(c, confidence=min(c.confidence, 0.2), note="not a number")
    n = float(m[1].replace(",", ""))
    if m[2]:
        n, note = n * LAKH, f"'{raw}' read as lakhs (x100,000)"
    else:
        note = ""
    n = -n if neg else n
    if not m[2] and 0 < n < SMALL_SALARY_MAX:
        return replace(c, value=int(n) if n.is_integer() else n, confidence=min(c.confidence, 0.3), flag="ambiguous_salary_unit", suggestion=str(int(round(n * LAKH))),
                       note=f"bare amount {raw.strip()} is too small for an annual salary; it may mean {raw.strip()} lakhs per annum ({round(n * LAKH):,})")
    return replace(c, value=int(n) if n.is_integer() else round(n, 2), note=note or c.note)


def normalize_status(c: Cell, ctx: dict) -> Cell:
    if c.value is None:
        return c
    v = re.sub(r"[\s\-]+", "_", clean_text(str(c.value)).lower())
    enum = ctx.get("enum", [])
    if v in enum:
        return replace(c, value=v)
    close = difflib.get_close_matches(v, enum, n=1, cutoff=0.85)
    if close:
        return replace(c, value=close[0], confidence=min(c.confidence, 0.8), note=f"fuzzy match of '{c.value}'")
    return replace(c, value=v, confidence=min(c.confidence, 0.3), note=f"'{c.value}' is not a known status", candidates=list(enum))


class Directory:
    """Every employee id and name seen across all source files, for resolving manager references."""
    def __init__(self) -> None:
        self.ids: dict[str, str] = {}
        self.names: dict[str, set[str]] = {}

    def add(self, emp_id: str | None, first: str | None, last: str | None) -> None:
        if not emp_id:
            return
        self.ids[str(emp_id).upper()] = str(emp_id)
        if first and last:
            self.names.setdefault(norm_name(f"{first} {last}"), set()).add(str(emp_id))

    def lookup(self, text: str) -> list[str]:
        if "," in text:
            last, _, first = text.partition(",")
            text = f"{first} {last}"
        return sorted(self.names.get(norm_name(text), set()))


def resolve_employee_reference(c: Cell, ctx: dict) -> Cell:
    d: Directory | None = ctx.get("directory")
    if c.value is None or d is None:
        return c
    s = clean_text(str(c.value))
    if s.upper() in d.ids:
        return replace(c, value=d.ids[s.upper()])
    hits = d.lookup(s)
    if len(hits) == 1:
        return replace(c, value=hits[0], note=f"resolved name '{s}' to {hits[0]}")
    if len(hits) > 1:
        return replace(c, value=s, confidence=min(c.confidence, 0.4), flag="ambiguous_reference", candidates=hits, note=f"'{s}' matches several employees")
    if re.fullmatch(r"[A-Za-z]{1,4}[-_]?\d+", s):
        return replace(c, value=s, flag="dangling_reference", note=f"{s} does not exist in any source file")
    return replace(c, value=s, confidence=min(c.confidence, 0.3), flag="unresolved_reference", note=f"'{s}' matches no employee")


@dataclass(frozen=True)
class Tool:
    fn: Callable[[Cell, dict], Cell]
    fields: frozenset[str] | None       # None = any field
    description: str
    stage: str = "cell"                 # 'reference' tools need the cross-file employee directory


TOOLS: dict[str, Tool] = {
    "trim_whitespace": Tool(trim_whitespace, None, "Collapse whitespace, strip zero-width characters, NFC-normalise."),
    "normalize_email": Tool(normalize_email, frozenset({"email"}), "Lower-case, remove spaces, validate address shape."),
    "normalize_phone": Tool(normalize_phone, frozenset({"phone"}), "Parse and format as E.164 (drops trunk prefix 0, keeps country codes)."),
    "normalize_name": Tool(normalize_name, frozenset({"first_name", "last_name"}), "Fix ALL-CAPS/lower-case names (O'CONNOR -> O'Connor); keeps accents and mixed case."),
    "normalize_date": Tool(normalize_date, frozenset(DATE_FIELDS), "Parse any common date format to ISO YYYY-MM-DD; infers dd/mm vs mm/dd from the file."),
    "parse_amount": Tool(parse_amount, frozenset({"annual_salary"}), "Strip currency symbols/thousand separators and return a number."),
    "normalize_status": Tool(normalize_status, frozenset({"employment_status"}), "Map to the allowed status enum; unknown values get low confidence."),
    "resolve_employee_reference": Tool(resolve_employee_reference, frozenset({"manager_employee_id"}), "Turn a manager name into an employee id using all source files.", "reference"),
}
FORMAT_TOOL = {"email": "normalize_email", "phone": "normalize_phone", "first_name": "normalize_name", "last_name": "normalize_name",
               **{f: "normalize_date" for f in DATE_FIELDS}, "annual_salary": "parse_amount",
               "employment_status": "normalize_status", "manager_employee_id": "resolve_employee_reference"}


def applicable_tools(target_field: str) -> list[str]:
    return [n for n, t in TOOLS.items() if t.fields is None or target_field in t.fields]


def safe_plan(target_field: str) -> list[str]:
    """Schema-driven fallback plan used when no LLM is available: trim + the field's format tool."""
    return ["trim_whitespace", *([FORMAT_TOOL[target_field]] if target_field in FORMAT_TOOL else [])]


def run_tools(target_field: str, cell: Cell, plan: list[str], ctx: dict, stage: str) -> Cell:
    names = ["trim_whitespace"] + [n for n in plan if n != "trim_whitespace"] if stage == "cell" else list(plan)
    for name in names:
        tool = TOOLS.get(name)
        if not tool or tool.stage != stage or (tool.fields and target_field not in tool.fields):
            continue
        cell = tool.fn(cell, ctx)
    return cell
