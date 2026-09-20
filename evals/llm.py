"""LLM back-ends for the eval harness.

  live  : real Gemini (needs GEMINI_API_KEY), wrapped with throttling + retry + call accounting.
  mock  : scripted "oracle" that answers from the gold labels. It exists ONLY to prove the harness itself works
          (a perfect oracle must score ~1.0; --mock-noise must lower the score). Its scores say nothing about Gemini.
  none  : no LLM at all (deterministic fallbacks only).
"""
from __future__ import annotations

import json
import os
import re
import time
import zlib
from dataclasses import dataclass, field
from typing import Any

from backend.agents import LLMProvider
from backend.models import (ClassificationOutput, CleaningInstruction, CleaningOutput, ColumnClassification, ValidationOutput)
from backend.tools import safe_plan

WRONG_TARGETS = ["department", "location", "job_title", "last_name", "first_name", "email"]


@dataclass
class CallStats:
    calls: int = 0
    errors: int = 0
    retries: int = 0
    seconds: float = 0.0
    last_error: str = ""
    _last_call: float = field(default=0.0, repr=False)

    def as_dict(self) -> dict[str, Any]:
        return {"llm_calls": self.calls, "llm_errors": self.errors, "llm_retries": self.retries, "llm_seconds": round(self.seconds, 1), "last_error": self.last_error}


# ------------------------------------------------------------------------------------------------ live
class _Throttled:
    def __init__(self, runnable: Any, stats: CallStats, min_interval: float, retries: int) -> None:
        self.runnable, self.stats, self.min_interval, self.retries = runnable, stats, min_interval, retries

    def invoke(self, prompt: str) -> Any:
        for attempt in range(self.retries + 1):
            wait = self.min_interval - (time.monotonic() - self.stats._last_call)
            if wait > 0:
                time.sleep(wait)
            self.stats._last_call = time.monotonic()
            started = time.monotonic()
            try:
                self.stats.calls += 1
                out = self.runnable.invoke(prompt)
                self.stats.seconds += time.monotonic() - started
                return out
            except Exception as error:  # noqa: BLE001 - rate limits / transient 5xx
                self.stats.seconds += time.monotonic() - started
                self.stats.last_error = f"{type(error).__name__}: {str(error)[:160]}"
                if attempt == self.retries:
                    self.stats.errors += 1
                    raise
                self.stats.retries += 1
                time.sleep(min(60, 5 * 2 ** attempt))


def build_live(min_interval: float = 0.0, retries: int = 3) -> tuple[LLMProvider, CallStats]:
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key:
        raise SystemExit("GEMINI_API_KEY (or GOOGLE_API_KEY) is not set. Put it in .env or export it, or use --mode deterministic / --mode mock.")
    from langchain_google_genai import ChatGoogleGenerativeAI
    name = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    chat, stats = ChatGoogleGenerativeAI(model=name, api_key=key, temperature=0), CallStats()

    class Factory:
        def with_structured_output(self, schema: Any) -> _Throttled:
            return _Throttled(chat.with_structured_output(schema), stats, min_interval, retries)

    provider = LLMProvider(factory=lambda: Factory())
    provider.model_name = name
    return provider, stats


def build_none() -> tuple[LLMProvider, CallStats]:
    provider = LLMProvider()
    provider.api_key = None            # force "no LLM" even if a key is in the environment
    return provider, CallStats()


# ------------------------------------------------------------------------------------------------ mock oracle
def _noisy(key: str, rate: float) -> bool:
    return rate > 0 and (zlib.crc32(key.encode()) % 1000) / 1000 < rate


class _MockRunnable:
    def __init__(self, schema: Any, gold: dict[tuple[str, str], dict], noise: float, stats: CallStats) -> None:
        self.schema, self.gold, self.noise, self.stats = schema, gold, noise, stats

    def invoke(self, prompt: str) -> Any:
        self.stats.calls += 1
        if self.schema is ClassificationOutput:
            return ClassificationOutput(classifications=[self._classify(c) for c in self._payload(prompt, "columns")])
        if self.schema is CleaningOutput:
            out = []
            for f in self._payload(prompt, "fields"):
                name = f["target_field"]
                tools = ["trim_whitespace"] if _noisy("clean:" + name, self.noise) else safe_plan(name)
                out.append(CleaningInstruction(target_field=name, tools=tools, confidence=0.95, rationale="oracle"))
            return CleaningOutput(instructions=out)
        return ValidationOutput(approved=True, confidence=0.9, findings=[])      # mock critic never objects

    @staticmethod
    def _payload(prompt: str, tag: str) -> list[dict]:
        return json.loads(re.search(rf"<{tag}>(.*)</{tag}>", prompt, re.S).group(1))

    def _classify(self, col: dict) -> ColumnClassification:
        file, header = col["source_file"], col["source_header"]
        g = self.gold.get((file, header), {"kind": "ignore"})
        base = {"source_file": file, "source_header": header, "rationale": "oracle"}
        wrong = _noisy(f"cls:{file}:{header}", self.noise)
        if g["kind"] == "map":
            if wrong:
                bad = next(t for t in WRONG_TARGETS if t != g["target_field"])
                return ColumnClassification(**base, target_field=bad, transform="direct", confidence=0.93)
            return ColumnClassification(**base, target_field=g["target_field"], transform=g["transform"], confidence=0.95)
        if g["kind"] == "ignore":
            if wrong:
                return ColumnClassification(**base, target_field="department", transform="direct", confidence=0.90)
            return ColumnClassification(**base, target_field=None, transform="ignore", confidence=0.96)
        return ColumnClassification(**base, target_field=g.get("target_field"), transform="direct", confidence=0.95 if wrong else 0.50)


def build_mock(gold: dict[tuple[str, str], dict], noise: float = 0.0) -> tuple[LLMProvider, CallStats]:
    stats = CallStats()

    class Factory:
        def with_structured_output(self, schema: Any) -> _MockRunnable:
            return _MockRunnable(schema, gold, noise, stats)

    provider = LLMProvider(factory=lambda: Factory())
    provider.model_name = f"mock-oracle(noise={noise})"
    return provider, stats
