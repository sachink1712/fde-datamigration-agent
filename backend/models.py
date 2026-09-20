"""Pydantic contracts for every structured LLM output."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Transform = Literal["direct", "split_full_name", "monthly_to_annual", "ignore"]
ToolName = Literal[
    "trim_whitespace", "normalize_email", "normalize_phone", "normalize_name",
    "normalize_date", "parse_amount", "normalize_status", "resolve_employee_reference",
]


class ColumnClassification(BaseModel):
    source_file: str = Field(description="Copied verbatim from the input column")
    source_header: str = Field(description="Copied verbatim from the input column")
    target_field: str | None = Field(default=None, description="A target schema field, or null when unrelated")
    transform: Transform = "direct"
    confidence: float = Field(ge=0, le=1)
    rationale: str
    alternatives: list[str] = Field(default_factory=list, description="Other plausible target fields")


class ClassificationOutput(BaseModel):
    classifications: list[ColumnClassification]


class CleaningInstruction(BaseModel):
    target_field: str
    tools: list[ToolName] = Field(description="Tools to run, in order")
    confidence: float = Field(ge=0, le=1)
    rationale: str


class CleaningOutput(BaseModel):
    instructions: list[CleaningInstruction]


class Finding(BaseModel):
    subject: str = Field(description="Column key 'file::header' or target field the finding is about")
    severity: Literal["error", "warning"]
    feedback: str


class ValidationOutput(BaseModel):
    approved: bool
    confidence: float = Field(ge=0, le=1)
    findings: list[Finding]
