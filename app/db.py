import json
import sqlite3
import time
import uuid
from contextlib import contextmanager

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    status TEXT NOT NULL,          -- queued | scripting | rendering | ready | publishing | published | failed
    progress INTEGER DEFAULT 0,
    request TEXT NOT NULL,         -- JSON: topic, language, aspect, duration, voice, platforms...
    script TEXT,                   -- JSON from Claude: title, script, search_terms, captions...
    mpt_task_id TEXT,
    video_path TEXT,
    publish_result TEXT,           -- JSON
    error TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

JSON_FIELDS = ("request", "script", "publish_result")


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    with conn() as c:
        c.executescript(SCHEMA)
        # Jobs interrupted by a restart can't resume; mark them failed.
        c.execute(
            "UPDATE projects SET status='failed', error='Interrupted by server restart' "
            "WHERE status IN ('queued','scripting','rendering','publishing')"
        )


def _row(r):
    if r is None:
        return None
    d = dict(r)
    for f in JSON_FIELDS:
        if d.get(f):
            d[f] = json.loads(d[f])
    return d


def create_project(request: dict) -> dict:
    pid = uuid.uuid4().hex[:12]
    with conn() as c:
        c.execute(
            "INSERT INTO projects (id, created_at, status, request) VALUES (?,?,?,?)",
            (pid, time.time(), "queued", json.dumps(request, ensure_ascii=False)),
        )
    return get_project(pid)


def update_project(pid: str, **fields):
    for f in JSON_FIELDS:
        if f in fields and fields[f] is not None:
            fields[f] = json.dumps(fields[f], ensure_ascii=False)
    cols = ", ".join(f"{k}=?" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE projects SET {cols} WHERE id=?", (*fields.values(), pid))


def get_project(pid: str):
    with conn() as c:
        return _row(c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone())


def list_projects(limit: int = 100):
    with conn() as c:
        rows = c.execute("SELECT * FROM projects ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [_row(r) for r in rows]


def delete_project(pid: str):
    with conn() as c:
        c.execute("DELETE FROM projects WHERE id=?", (pid,))


def get_setting(key: str, default=None):
    with conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(r["value"]) if r else default


def set_setting(key: str, value):
    with conn() as c:
        c.execute(
            "INSERT INTO settings (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )
