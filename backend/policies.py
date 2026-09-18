from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "employee.schema.json"
TARGET_SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

PII_FIELDS = {"first_name", "last_name", "email", "phone", "employee_id"}
SENSITIVE_SOURCE_TOKENS = {"salary", "ctc", "compensation", "bank", "iban", "ssn", "aadhaar", "medical", "health", "passport"}
REQUIRED_FIELDS = set(TARGET_SCHEMA["required"])


def normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


ALIASES = {field: set(spec.get("x-source-aliases", [])) for field, spec in TARGET_SCHEMA["properties"].items()}
COMPOSITE_SOURCE_ALIASES = {"fullname", "employeename", "name"}


def classify_header(header: str) -> str:
    clean = normalize_header(header)
    return "sensitive" if any(token in clean for token in SENSITIVE_SOURCE_TOKENS) else "allowed"


def safe_trace_value(field: str, value: Any) -> str:
    """Return a deterministic redaction safe for third-party observability."""
    if value is None:
        return "<null>"
    if field in PII_FIELDS:
        digest = hashlib.sha256(str(value).encode()).hexdigest()[:12]
        return f"redacted:{digest}"
    return str(value)[:80]


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str


def authorize_target_write(record: dict[str, Any], unresolved_count: int, action: str = "upsert") -> PolicyDecision:
    if action not in {"upsert", "retry"}:
        return PolicyDecision(False, "Only scoped upsert/retry operations are autonomous")
    if unresolved_count:
        return PolicyDecision(False, "Human review is required before target writes")
    missing = sorted(field for field in REQUIRED_FIELDS if not record.get(field))
    if missing:
        return PolicyDecision(False, f"Required fields missing: {', '.join(missing)}")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", str(record["email"])):
        return PolicyDecision(False, "Email failed deterministic validation")
    return PolicyDecision(True, "Validated record with no unresolved review")
