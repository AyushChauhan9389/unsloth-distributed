"""Cloud job records: one SQLite table next to Studio's own data (central server only, never the bucket)."""
import json
import sqlite3
import threading

from utils.paths import studio_root

STATES = ("scheduled", "launching", "running", "succeeded", "failed", "cancelled")
_JSON = ("resources", "schedule", "request", "config", "secrets")
_COLS = ("id", "name", "state", "resources", "schedule", "timezone", "request", "config", "secrets", "folder",
         "created_at", "next_run_at", "started_at", "ended_at", "external_id", "parent_id", "error", "attempts")
_lock = threading.Lock()
_conn = None


def _db():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(studio_root() / "cloud_jobs.db"), check_same_thread = False)
        _conn.row_factory = sqlite3.Row
        _conn.execute(f"create table if not exists jobs ({', '.join(c + (' text primary key' if c == 'id' else '') for c in _COLS)})")
        _conn.commit()
    return _conn


def _row(r) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for k in _JSON:
        d[k] = json.loads(d[k]) if d[k] else None
    return d


def _enc(fields: dict) -> dict:
    return {k: (json.dumps(v) if k in _JSON and v is not None else v) for k, v in fields.items()}


def insert(job: dict) -> None:
    job = _enc({c: job.get(c) for c in _COLS})
    with _lock:
        _db().execute(f"insert into jobs ({', '.join(job)}) values ({', '.join('?' * len(job))})", list(job.values()))
        _db().commit()


def update(job_id: str, **fields) -> None:
    fields = _enc(fields)
    with _lock:
        _db().execute(f"update jobs set {', '.join(k + ' = ?' for k in fields)} where id = ?", [*fields.values(), job_id])
        _db().commit()


def get(job_id: str) -> dict | None:
    with _lock:
        return _row(_db().execute("select * from jobs where id = ?", (job_id,)).fetchone())


def list_jobs(states = None) -> list[dict]:
    with _lock:
        if states:
            rows = _db().execute(f"select * from jobs where state in ({', '.join('?' * len(states))}) order by created_at desc", list(states))
        else:
            rows = _db().execute("select * from jobs order by created_at desc")
        return [_row(r) for r in rows.fetchall()]
