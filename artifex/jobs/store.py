"""SQLite persistence for jobs (one JSON document per job)."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from .models import Job


class JobStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, created REAL, status TEXT, data TEXT NOT NULL)"
        )
        self._db.commit()

    def save(self, job: Job) -> None:
        job.updated = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO jobs (id, created, status, data) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET status=excluded.status, data=excluded.data",
                (job.id, job.created, job.status, job.model_dump_json()),
            )
            self._db.commit()

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            row = self._db.execute("SELECT data FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return Job.model_validate_json(row[0]) if row else None

    def list(self, limit: int = 50) -> list[Job]:
        with self._lock:
            rows = self._db.execute("SELECT data FROM jobs ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
        return [Job.model_validate_json(r[0]) for r in rows]

    def by_status(self, *statuses: str) -> list[Job]:
        q = ",".join("?" for _ in statuses)
        with self._lock:
            rows = self._db.execute(f"SELECT data FROM jobs WHERE status IN ({q}) ORDER BY created", statuses).fetchall()
        return [Job.model_validate_json(r[0]) for r in rows]

    def delete(self, job_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            self._db.commit()
