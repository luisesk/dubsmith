"""SQLite-backed job queue for plex-dub-companion.

States: pending -> downloading -> syncing -> muxing -> done | failed | quarantined
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    series_id INTEGER NOT NULL,
    season INTEGER NOT NULL,
    episode INTEGER NOT NULL,
    target_path TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    sync_delay_ms INTEGER,
    sync_score REAL,
    progress REAL DEFAULT 0,
    bytes_total INTEGER DEFAULT 0,
    bytes_done INTEGER DEFAULT 0,
    phase TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    completed_at REAL,
    UNIQUE(series_id, season, episode)
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS idx_jobs_series ON jobs(series_id);
"""

# best-effort migrations
MIGRATIONS = [
    "ALTER TABLE jobs ADD COLUMN progress REAL DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN bytes_total INTEGER DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN bytes_done INTEGER DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN phase TEXT",
]

VALID_STATES = {"pending", "downloading", "syncing", "muxing", "done", "failed", "quarantined"}


@dataclass
class Job:
    id: int
    series_id: int
    season: int
    episode: int
    target_path: str
    state: str
    attempts: int
    last_error: str | None
    sync_delay_ms: int | None
    sync_score: float | None
    progress: float
    bytes_total: int
    bytes_done: int
    phase: str | None
    created_at: float
    updated_at: float
    completed_at: float | None


class Queue:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
            row = c.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
            cur_v = (row["v"] if row else None) or 0
            for stmt in MIGRATIONS:
                try:
                    c.execute(stmt)
                except sqlite3.OperationalError:
                    pass  # column already exists
            if cur_v < SCHEMA_VERSION:
                c.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))

    @contextmanager
    def _conn(self):
        c = sqlite3.connect(self.db_path, isolation_level=None, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        c.execute("PRAGMA journal_mode = WAL")
        try:
            yield c
        finally:
            c.close()

    def upsert_pending(self, series_id: int, season: int, episode: int, target_path: str) -> int:
        """Insert a pending job. If exists & not done, leave alone. Returns job id."""
        now = time.time()
        with self._conn() as c:
            row = c.execute(
                "SELECT id, state FROM jobs WHERE series_id=? AND season=? AND episode=?",
                (series_id, season, episode),
            ).fetchone()
            if row:
                if row["state"] in ("done",):
                    return row["id"]
                return row["id"]
            cur = c.execute(
                """INSERT INTO jobs(series_id, season, episode, target_path, state,
                                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
                (series_id, season, episode, target_path, now, now),
            )
            return cur.lastrowid

    def claim_next(self, allowed_states: tuple[str, ...] = ("pending",)) -> Job | None:
        """Atomically pick the oldest job in given states, set state=downloading."""
        now = time.time()
        with self._conn() as c:
            placeholders = ",".join("?" for _ in allowed_states)
            row = c.execute(
                f"SELECT * FROM jobs WHERE state IN ({placeholders}) "
                f"ORDER BY created_at LIMIT 1",
                allowed_states,
            ).fetchone()
            if not row:
                return None
            c.execute(
                "UPDATE jobs SET state='downloading', updated_at=?, attempts=attempts+1 WHERE id=?",
                (now, row["id"]),
            )
            return self.get(row["id"])

    def set_state(self, job_id: int, state: str, **fields) -> None:
        if state not in VALID_STATES:
            raise ValueError(f"invalid state: {state}")
        now = time.time()
        cols = ["state=?", "updated_at=?"]
        vals: list = [state, now]
        for k, v in fields.items():
            cols.append(f"{k}=?")
            vals.append(v)
        if state == "done":
            cols.append("completed_at=?")
            vals.append(now)
        vals.append(job_id)
        with self._conn() as c:
            c.execute(f"UPDATE jobs SET {','.join(cols)} WHERE id=?", vals)

    def get(self, job_id: int) -> Job | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return _row_to_job(row) if row else None

    def list(self, state: str | None = None, limit: int = 200) -> list[Job]:
        with self._conn() as c:
            if state:
                rows = c.execute(
                    "SELECT * FROM jobs WHERE state=? ORDER BY updated_at DESC LIMIT ?",
                    (state, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM jobs ORDER BY updated_at DESC LIMIT ?", (limit,)
                ).fetchall()
            return [_row_to_job(r) for r in rows]

    def stats(self) -> dict[str, int]:
        with self._conn() as c:
            rows = c.execute("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state").fetchall()
            return {r["state"]: r["n"] for r in rows}

    def stats_per_series(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT series_id, state, COUNT(*) AS n FROM jobs GROUP BY series_id, state"
            ).fetchall()
            agg: dict[int, dict] = {}
            for r in rows:
                d = agg.setdefault(r["series_id"], {"series_id": r["series_id"]})
                d[r["state"]] = r["n"]
            return list(agg.values())

    def metrics(self) -> dict:
        with self._conn() as c:
            sync = c.execute(
                "SELECT AVG(ABS(sync_delay_ms)) AS avg_abs, AVG(sync_score) AS avg_score, "
                "COUNT(*) AS n FROM jobs WHERE state='done' AND sync_score IS NOT NULL"
            ).fetchone()
            recent_done = c.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE state='done' "
                "AND completed_at > strftime('%s','now')-86400"
            ).fetchone()
            return {
                "avg_abs_delay_ms": round(sync["avg_abs"] or 0, 1),
                "avg_sync_score": round(sync["avg_score"] or 0, 1),
                "done_total": sync["n"] or 0,
                "done_24h": recent_done["n"] or 0,
            }

    def by_series(self, series_id: int, season: int | None = None,
                   episode: int | None = None) -> Job | None:
        with self._conn() as c:
            sql = "SELECT * FROM jobs WHERE series_id=?"
            args: list = [series_id]
            if season is not None:
                sql += " AND season=?"
                args.append(season)
            if episode is not None:
                sql += " AND episode=?"
                args.append(episode)
            sql += " ORDER BY updated_at DESC LIMIT 1"
            row = c.execute(sql, args).fetchone()
            return _row_to_job(row) if row else None

    def update_progress(self, job_id: int, progress: float | None = None,
                        bytes_done: int | None = None, bytes_total: int | None = None,
                        phase: str | None = None) -> None:
        cols, vals = [], []
        if progress is not None:
            # use -1 to signal indeterminate progress
            if progress < 0:
                cols.append("progress=?"); vals.append(-1.0)
            else:
                cols.append("progress=?"); vals.append(max(0.0, min(1.0, progress)))
        if bytes_done is not None:
            cols.append("bytes_done=?"); vals.append(bytes_done)
        if bytes_total is not None:
            cols.append("bytes_total=?"); vals.append(bytes_total)
        if phase is not None:
            cols.append("phase=?"); vals.append(phase)
        if not cols:
            return
        cols.append("updated_at=?"); vals.append(time.time())
        vals.append(job_id)
        with self._conn() as c:
            c.execute(f"UPDATE jobs SET {','.join(cols)} WHERE id=?", vals)

    def reset_stale_running(self) -> int:
        """Reset jobs stuck in transient states (e.g. after a crash/restart) back to pending."""
        now = time.time()
        with self._conn() as c:
            cur = c.execute(
                "UPDATE jobs SET state='pending', updated_at=?, "
                "last_error='reset on restart' "
                "WHERE state IN ('downloading','syncing','muxing')",
                (now,),
            )
            return cur.rowcount

    def retry_failed(self, max_attempts: int = 3) -> int:
        """Reset failed jobs (under attempt cap) back to pending."""
        now = time.time()
        with self._conn() as c:
            cur = c.execute(
                "UPDATE jobs SET state='pending', updated_at=? "
                "WHERE state='failed' AND attempts < ?",
                (now, max_attempts),
            )
            return cur.rowcount

    def delete_where(self, state: str | None = None, error_like: str | None = None) -> int:
        with self._conn() as c:
            sql = "DELETE FROM jobs WHERE 1=1"
            args: list = []
            if state:
                sql += " AND state=?"
                args.append(state)
            if error_like:
                sql += " AND last_error LIKE ?"
                args.append(f"%{error_like}%")
            cur = c.execute(sql, args)
            return cur.rowcount


def _row_to_job(row: sqlite3.Row) -> Job:
    keys = row.keys()
    g = lambda k, default=None: row[k] if k in keys else default
    return Job(
        id=row["id"], series_id=row["series_id"], season=row["season"],
        episode=row["episode"], target_path=row["target_path"], state=row["state"],
        attempts=row["attempts"], last_error=row["last_error"],
        sync_delay_ms=row["sync_delay_ms"], sync_score=row["sync_score"],
        progress=g("progress", 0) or 0,
        bytes_total=g("bytes_total", 0) or 0,
        bytes_done=g("bytes_done", 0) or 0,
        phase=g("phase"),
        created_at=row["created_at"], updated_at=row["updated_at"],
        completed_at=row["completed_at"],
    )
