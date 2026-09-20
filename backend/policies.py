"""Guardrails: schema contract, sensitive-column policy, PII-safe tracing, deterministic write authorisation."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "employee.schema.json"
TARGET_SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
TARGET_FIELDS = set(TARGET_SCHEMA["properties"])
REQUIRED_FIELDS = set(TARGET_SCHEMA["required"])
PII_FIELDS = {"first_name", "last_name", "email", "phone", "employee_id", "date_of_birth", "annual_salary"}
# Columns that must never be mapped or sent to an LLM with sample values. Compensation is NOT here:
# it is part of the target contract (annual_salary), so it may map.
SENSITIVE_SOURCE_TOKENS = {"bank", "iban", "ssn", "aadhaar", "aadhar", "medical", "health", "passport", "pan", "swift"}

# Thresholds shared by generators and the validator.
CLASSIFY_MIN = 0.85     # below this a column mapping goes to a human
CLEAN_MIN = 0.80        # below this a cleaned value goes to a human
PLAN_MIN = 0.70         # below this the cleaner is asked to reconsider its tool plan
SHAPE_MIN = 0.70        # share of a column's values that must look like the target field
MAX_RETRIES = 2
SMALL_SALARY_MAX = 1000        # a bare annual amount below this is ambiguous (thousands? lakhs?) -> human
DOMAIN_EDIT_MAX = 2            # email domain within this edit distance of the dominant domain is a typo suspect
DOMAIN_DOMINANCE = 0.6         # ...but only when the dominant domain covers this share of the file (and >= 3 records)
COMPOUND_DEPT_LEGIT = 0.3      # a compound department shared by this share of records is a real department name

_VALIDATOR = Draft202012Validator(TARGET_SCHEMA, format_checker=FormatChecker())


def header_words(header: str) -> list[str]:
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", header)  # BankAccount -> Bank Account
    return re.findall(r"[a-z0-9]+", spaced.lower())


def classify_header(header: str) -> str:
    return "sensitive" if any(w in SENSITIVE_SOURCE_TOKENS for w in header_words(header)) else "allowed"


def safe_trace_value(field: str, value: Any) -> str:
    if value is None:
        return "<null>"
    if field in PII_FIELDS:
        return f"redacted:{hashlib.sha256(str(value).encode()).hexdigest()[:12]}"
    return str(value)[:80]


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str


def authorize_target_write(record: dict[str, Any], blocked_ids: set[str], action: str = "upsert") -> PolicyDecision:
    """Final deterministic gate before the target API is called. The LLM has no say here."""
    if action not in {"upsert", "retry"}:
        return PolicyDecision(False, "Only idempotent upsert/retry operations are allowed")
    if str(record.get("employee_id", "")) in blocked_ids:
        return PolicyDecision(False, "This employee record is awaiting or failed human review")
    payload = {k: v for k, v in record.items() if not k.startswith("_") and v is not None}
    errors = sorted(_VALIDATOR.iter_errors(payload), key=lambda e: list(e.path))
    if errors:
        e = errors[0]
        where = ".".join(str(p) for p in e.path) or "record"
        return PolicyDecision(False, f"Target schema violation at {where}: {e.message[:120]}")
    return PolicyDecision(True, "Record passed deterministic target policy")
