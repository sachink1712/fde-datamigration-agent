from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


class Store:
    def __init__(self, path: str = "data/migration.db") -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS target_records (employee_id TEXT PRIMARY KEY, payload TEXT NOT NULL, batch_id TEXT NOT NULL);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def save_run(self, run: dict[str, Any]) -> None:
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO runs VALUES (?, ?)", (run["id"], json.dumps(run)))

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT payload FROM runs WHERE id = ?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def upsert_target(self, record: dict[str, Any], batch_id: str) -> None:
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO target_records VALUES (?, ?, ?)", (record["employee_id"], json.dumps(record), batch_id))

    def rollback_batch(self, batch_id: str) -> int:
        with self._connect() as db:
            cursor = db.execute("DELETE FROM target_records WHERE batch_id = ?", (batch_id,))
        return cursor.rowcount
