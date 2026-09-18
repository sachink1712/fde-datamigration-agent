from __future__ import annotations

import os
import json
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .agent import MigrationGraph
from .engine import MigrationEngine
from .store import Store
from .uploads import save_uploads
from .policies import TARGET_SCHEMA

app = FastAPI(title="Migration Agent API", version="0.1.0")
cors_origins = [origin.strip() for origin in os.getenv("CORS_ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if origin.strip()]
is_production = os.getenv("ENVIRONMENT", "development").lower() == "production"
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    # Local static servers often select an arbitrary port; production must use explicit origins above.
    allow_origin_regex=None if is_production else r"https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
engine = MigrationEngine(Store())
agent = MigrationGraph(engine)


class Resolution(BaseModel):
    action: str = Field(pattern="^(approved|corrected|rejected)$")
    selected_value: str | None = None


def require_run(run_id: str, run: dict | None) -> dict:
    if not run:
        raise HTTPException(404, f"Migration run {run_id} not found")
    return run


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/schema")
def get_schema() -> dict:
    return TARGET_SCHEMA


@app.get("/api/evals/latest")
def latest_evaluations() -> dict:
    path = Path("output/evals/latest.json")
    if not path.exists():
        raise HTTPException(404, "No evaluation report exists. Run: uv run python -m evals.run_evals")
    return json.loads(path.read_text(encoding="utf-8"))


@app.post("/api/runs")
async def create_run(files: list[UploadFile] = File(default=[])) -> dict:
    return agent.start(await save_uploads(files))


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict:
    return require_run(run_id, engine.store.get_run(run_id))


@app.post("/api/runs/{run_id}/escalations/{escalation_id}")
def resolve(run_id: str, escalation_id: str, body: Resolution) -> dict:
    return require_run(run_id, agent.resume_review(run_id, {"escalation_id": escalation_id, "action": body.action, "selected_value": body.selected_value}))


@app.post("/api/runs/{run_id}/push")
def push(run_id: str, retry: bool = False) -> dict:
    return require_run(run_id, agent.execute(run_id, "retry" if retry else "push"))


@app.post("/api/runs/{run_id}/rollback")
def rollback(run_id: str) -> dict:
    return require_run(run_id, agent.execute(run_id, "rollback"))
