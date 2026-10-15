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
    error TEXT,
    timeline TEXT,                 -- JSON: editable scenes, captions, voice
    version INTEGER DEFAULT 0,
    extra TEXT                     -- JSON: framejam review id, versions, edit history
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    name TEXT NOT NULL,
    data TEXT NOT NULL             -- JSON: notes, website, brand_id, language, format, context files
);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    project_id TEXT,
    kind TEXT NOT NULL,            -- script | ideas | edit | motion | motion-edit | brand-import
    model TEXT,                    -- requested model (alias or id)
    effort TEXT,
    source TEXT,                   -- app | system | api_key
    models TEXT,                   -- JSON: per-model usage as reported by Claude
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    turns INTEGER DEFAULT 0,
    duration_s REAL DEFAULT 0,
    ok INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS usage_at ON usage(at);
CREATE INDEX IF NOT EXISTS usage_project ON usage(project_id);
CREATE TABLE IF NOT EXISTS brands (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    name TEXT NOT NULL,
    data TEXT NOT NULL             -- JSON: see brands.EMPTY
);
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    platform TEXT NOT NULL,        -- youtube | tiktok | facebook
    external_id TEXT NOT NULL,     -- channel / open_id / page id
    name TEXT,
    avatar TEXT,
    data TEXT NOT NULL,            -- JSON: tokens
    UNIQUE (platform, external_id)
);
"""

JSON_FIELDS = ("request", "script", "publish_result", "data", "timeline", "extra")


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


MIGRATIONS = {"timeline": "TEXT", "version": "INTEGER DEFAULT 0", "extra": "TEXT", "workspace_id": "TEXT"}


def init():
    with conn() as c:
        c.executescript(SCHEMA)
        have = {r["name"] for r in c.execute("PRAGMA table_info(projects)")}
        for col, typ in MIGRATIONS.items():
            if col not in have:
                c.execute(f"ALTER TABLE projects ADD COLUMN {col} {typ}")
        # Jobs interrupted by a restart can't resume; mark them failed.
        c.execute(
            "UPDATE projects SET error='Interrupted by server restart', "
            "status=CASE WHEN video_path IS NOT NULL THEN 'ready' ELSE 'failed' END "
            "WHERE status IN ('queued','scripting','designing','editing','rendering','publishing')"
        )


def _row(r):
    if r is None:
        return None
    d = dict(r)
    for f in JSON_FIELDS:
        if d.get(f):
            d[f] = json.loads(d[f])
    return d


def create_project(request: dict, workspace_id: str | None = None) -> dict:
    pid = uuid.uuid4().hex[:12]
    with conn() as c:
        c.execute(
            "INSERT INTO projects (id, created_at, status, request, workspace_id) VALUES (?,?,?,?,?)",
            (pid, time.time(), "queued", json.dumps(request, ensure_ascii=False), workspace_id),
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


def list_projects(limit: int = 100, workspace_id: str | None = None):
    with conn() as c:
        if workspace_id:
            rows = c.execute("SELECT * FROM projects WHERE workspace_id=? ORDER BY created_at DESC LIMIT ?",
                             (workspace_id, limit)).fetchall()
        else:
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


def upsert_account(platform: str, external_id: str, name: str, avatar: str, data: dict) -> dict:
    with conn() as c:
        row = c.execute(
            "SELECT id FROM accounts WHERE platform=? AND external_id=?", (platform, external_id)
        ).fetchone()
        aid = row["id"] if row else uuid.uuid4().hex[:12]
        c.execute(
            "INSERT INTO accounts (id, created_at, platform, external_id, name, avatar, data) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "name=excluded.name, avatar=excluded.avatar, data=excluded.data",
            (aid, time.time(), platform, external_id, name, avatar, json.dumps(data)),
        )
    return get_account(aid)


def update_account_data(aid: str, data: dict):
    with conn() as c:
        c.execute("UPDATE accounts SET data=? WHERE id=?", (json.dumps(data), aid))


def get_account(aid: str):
    with conn() as c:
        return _row(c.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone())


def list_accounts():
    with conn() as c:
        rows = c.execute("SELECT * FROM accounts ORDER BY platform, created_at").fetchall()
    return [_row(r) for r in rows]


def delete_account(aid: str):
    with conn() as c:
        c.execute("DELETE FROM accounts WHERE id=?", (aid,))


def save_brand(bid: str, name: str, data: dict):
    now = time.time()
    with conn() as c:
        c.execute(
            "INSERT INTO brands (id, created_at, updated_at, name, data) VALUES (?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, data=excluded.data, updated_at=excluded.updated_at",
            (bid, now, now, name, json.dumps(data, ensure_ascii=False)),
        )


def get_brand(bid: str):
    with conn() as c:
        return _row(c.execute("SELECT * FROM brands WHERE id=?", (bid,)).fetchone())


def list_brands():
    with conn() as c:
        rows = c.execute("SELECT * FROM brands ORDER BY updated_at DESC").fetchall()
    return [_row(r) for r in rows]


def delete_brand(bid: str):
    with conn() as c:
        c.execute("DELETE FROM brands WHERE id=?", (bid,))


USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "cost_usd", "turns", "duration_s")


def add_usage(row: dict):
    cols = ["at", "project_id", "kind", "model", "effort", "source", "models", *USAGE_FIELDS, "ok"]
    vals = [row.get(c) for c in cols]
    vals[cols.index("models")] = json.dumps(row.get("models") or {})
    with conn() as c:
        c.execute(f"INSERT INTO usage ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)


def _sum_select():
    return ", ".join(f"COALESCE(SUM({f}),0) AS {f}" for f in USAGE_FIELDS) + ", COUNT(*) AS runs"


def _sources(sources: tuple[str, ...] | None, col: str = "source") -> tuple[str, list]:
    """SQL filter on how Claude was connected for a run (None = any)."""
    if not sources:
        return "", []
    return f" AND {col} IN ({', '.join('?' * len(sources))})", list(sources)


def usage_totals(since: float, project_id: str | None = None, sources: tuple[str, ...] | None = None) -> dict:
    q = f"SELECT {_sum_select()} FROM usage WHERE at >= ?"
    args = [since]
    if project_id:
        q += " AND project_id = ?"
        args.append(project_id)
    where, extra = _sources(sources)
    with conn() as c:
        return dict(c.execute(q + where, args + extra).fetchone())


def usage_grouped(since: float, key: str, sources: tuple[str, ...] | None = None) -> list[dict]:
    expr = {"day": "date(at, 'unixepoch', 'localtime')", "model": "COALESCE(model, '')", "kind": "kind"}[key]
    where, extra = _sources(sources)
    with conn() as c:
        rows = c.execute(f"SELECT {expr} AS k, {_sum_select()} FROM usage WHERE at >= ?{where} GROUP BY k ORDER BY k",
                         [since, *extra]).fetchall()
    return [dict(r) for r in rows]


def usage_recent(limit: int = 30, project_id: str | None = None, sources: tuple[str, ...] | None = None) -> list[dict]:
    q = "SELECT u.*, p.script AS project_script, p.request AS project_request FROM usage u LEFT JOIN projects p ON p.id = u.project_id WHERE 1=1"
    args = []
    if project_id:
        q += " AND u.project_id = ?"
        args.append(project_id)
    where, extra = _sources(sources, "u.source")
    q += where
    args += extra
    q += " ORDER BY u.at DESC LIMIT ?"
    args.append(limit)
    with conn() as c:
        rows = c.execute(q, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["models"] = json.loads(d["models"] or "{}")
        script = json.loads(d.pop("project_script") or "null") or {}
        req = json.loads(d.pop("project_request") or "null") or {}
        d["project_title"] = script.get("title") or req.get("topic", "")[:80] or None
        out.append(d)
    return out


def save_workspace(wid: str, name: str, data: dict, created_at: float | None = None):
    with conn() as c:
        c.execute(
            "INSERT INTO workspaces (id, created_at, name, data) VALUES (?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, data=excluded.data",
            (wid, created_at or time.time(), name, json.dumps(data, ensure_ascii=False)),
        )


def get_workspace(wid: str):
    with conn() as c:
        return _row(c.execute("SELECT * FROM workspaces WHERE id=?", (wid,)).fetchone())


def list_workspaces():
    with conn() as c:
        rows = c.execute(
            "SELECT w.*, (SELECT COUNT(*) FROM projects p WHERE p.workspace_id = w.id) AS videos "
            "FROM workspaces w ORDER BY w.created_at"
        ).fetchall()
    return [_row(r) for r in rows]


def delete_workspace(wid: str):
    with conn() as c:
        c.execute("UPDATE projects SET workspace_id=NULL WHERE workspace_id=?", (wid,))
        c.execute("DELETE FROM workspaces WHERE id=?", (wid,))


def project_ids() -> set[str]:
    with conn() as c:
        return {r[0] for r in c.execute("SELECT id FROM projects")}


def detach_usage(project_id: str):
    with conn() as c:
        c.execute("UPDATE usage SET project_id=NULL WHERE project_id=?", (project_id,))
