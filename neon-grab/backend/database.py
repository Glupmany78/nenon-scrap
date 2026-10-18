"""SQLite persistence for NEON GRAB jobs and history."""
import sqlite3
import threading
import time
import json
from pathlib import Path
from typing import Optional, List, Dict, Any

DB_PATH = Path.home() / ".neon-grab" / "neon-grab.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    with _lock:
        conn = _conn()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    page_url TEXT,
                    media_type TEXT,
                    format_id TEXT,
                    title TEXT,
                    filename TEXT,
                    status TEXT NOT NULL,
                    progress REAL DEFAULT 0,
                    downloaded_bytes INTEGER DEFAULT 0,
                    total_bytes INTEGER DEFAULT 0,
                    speed REAL DEFAULT 0,
                    eta INTEGER,
                    error TEXT,
                    output_path TEXT,
                    referer TEXT,
                    created_at REAL NOT NULL,
                    started_at REAL,
                    completed_at REAL,
                    meta TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
            """)
            conn.commit()
        finally:
            conn.close()


def create_job(job_id: str, url: str, page_url: Optional[str] = None,
               media_type: Optional[str] = None, format_id: Optional[str] = None,
               title: Optional[str] = None, filename: Optional[str] = None,
               referer: Optional[str] = None, meta: Optional[Dict] = None) -> Dict:
    now = time.time()
    with _lock:
        conn = _conn()
        try:
            conn.execute("""
                INSERT INTO jobs (id, url, page_url, media_type, format_id, title,
                                  filename, status, created_at, referer, meta)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
            """, (job_id, url, page_url, media_type, format_id, title, filename,
                  now, referer, json.dumps(meta or {})))
            conn.commit()
        finally:
            conn.close()
    return get_job(job_id)


def update_job(job_id: str, **fields) -> None:
    if not fields:
        return
    # Whitelist fields
    allowed = {"status", "progress", "downloaded_bytes", "total_bytes", "speed",
               "eta", "error", "output_path", "started_at", "completed_at", "filename"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    set_clause = ", ".join(f"{k}=?" for k in updates.keys())
    values = list(updates.values()) + [job_id]
    with _lock:
        conn = _conn()
        try:
            conn.execute(f"UPDATE jobs SET {set_clause} WHERE id=?", values)
            conn.commit()
        finally:
            conn.close()


def get_job(job_id: str) -> Optional[Dict]:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_jobs(include_finished: bool = True, limit: int = 500) -> List[Dict]:
    conn = _conn()
    try:
        if include_finished:
            rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status IN ('queued','running') ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def clear_history() -> int:
    """Delete finished/failed/cancelled jobs. Active jobs remain."""
    with _lock:
        conn = _conn()
        try:
            cur = conn.execute(
                "DELETE FROM jobs WHERE status IN ('completed','failed','cancelled')"
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()