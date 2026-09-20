from __future__ import annotations

import os
from typing import Any

from .policies import safe_trace_value


class Observability:
    """Emits PII-safe local events and optional LangSmith traces. Raw source rows never cross this boundary."""

    def __init__(self) -> None:
        self.enabled = bool(os.getenv("LANGSMITH_API_KEY") and os.getenv("LANGSMITH_PROJECT"))
        self._traceable = None
        if self.enabled:
            try:
                from langsmith import traceable
                self._traceable = traceable
            except ImportError:
                self.enabled = False

    def emit(self, stage: str, run_id: str, metrics: dict[str, Any]) -> dict[str, Any]:
        payload = {"run_id": run_id, "stage": stage, "metrics": {k: safe_trace_value(k, v) for k, v in metrics.items()}}
        if self._traceable:
            @self._traceable(name=f"migration.{stage}")
            def send(data: dict[str, Any]) -> dict[str, Any]:
                return data
            send(payload)
        return payload
