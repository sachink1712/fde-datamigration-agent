from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


class Store:
    """SQLite: run documents plus a mock target HRMS with per-batch history so rollback restores what was there before."""

    def __init__(self, path: str = "data/migration.db") -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with closing(self._connect()) as db, db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS target_records (employee_id TEXT PRIMARY KEY, payload TEXT NOT NULL, batch_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS target_history (seq INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL, employee_id TEXT NOT NULL, prev_payload TEXT, prev_batch TEXT);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def save_run(self, run: dict[str, Any]) -> None:
        with closing(self._connect()) as db, db:
            db.execute("INSERT OR REPLACE INTO runs VALUES (?, ?)", (run["id"], json.dumps(run)))

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT payload FROM runs WHERE id = ?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def upsert_target(self, record: dict[str, Any], batch_id: str) -> None:
        """Mock target API call. Idempotent per employee_id."""
        with closing(self._connect()) as db, db:
            prev = db.execute("SELECT payload, batch_id FROM target_records WHERE employee_id = ?", (record["employee_id"],)).fetchone()
            db.execute("INSERT INTO target_history (batch_id, employee_id, prev_payload, prev_batch) VALUES (?, ?, ?, ?)",
                       (batch_id, record["employee_id"], prev[0] if prev else None, prev[1] if prev else None))
            db.execute("INSERT OR REPLACE INTO target_records VALUES (?, ?, ?)", (record["employee_id"], json.dumps(record), batch_id))

    def rollback_batch(self, batch_id: str) -> int:
        with closing(self._connect()) as db, db:
            rows = db.execute("SELECT seq, employee_id, prev_payload, prev_batch FROM target_history WHERE batch_id = ? ORDER BY seq DESC", (batch_id,)).fetchall()
            for _, emp, prev_payload, prev_batch in rows:
                if prev_payload is None:
                    db.execute("DELETE FROM target_records WHERE employee_id = ?", (emp,))
                else:
                    db.execute("INSERT OR REPLACE INTO target_records VALUES (?, ?, ?)", (emp, prev_payload, prev_batch))
            db.execute("DELETE FROM target_history WHERE batch_id = ?", (batch_id,))
        return len(rows)

    def target_records(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            return [json.loads(p) for (p,) in db.execute("SELECT payload FROM target_records ORDER BY employee_id")]
