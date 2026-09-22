"""SQLite storage for API keys. Only key hashes are stored, never the raw key."""
from __future__ import annotations
import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS keys (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash    TEXT    NOT NULL UNIQUE,
    prefix      TEXT    NOT NULL,
    label       TEXT    NOT NULL DEFAULT '',
    kind        TEXT    NOT NULL,             -- 'time' or 'tokens'
    created_at  REAL    NOT NULL,
    expires_at  REAL,                         -- unix ts, only for kind='time'
    token_limit INTEGER,                      -- only for kind='tokens'
    tokens_used INTEGER NOT NULL DEFAULT 0,
    requests    INTEGER NOT NULL DEFAULT 0,
    revoked     INTEGER NOT NULL DEFAULT 0,
    models      TEXT    NOT NULL DEFAULT '*',  -- JSON list of allowed public model names, or '*'
    user_id     INTEGER,                       -- Semifly account owner; null for legacy operator keys
    project_id  INTEGER                        -- customer project; null for legacy operator keys
);
CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at REAL NOT NULL,
    key_id INTEGER NOT NULL,
    model TEXT NOT NULL,
    upstream TEXT NOT NULL,
    actual_model TEXT NOT NULL,
    elapsed_ms INTEGER NOT NULL,
    status TEXT NOT NULL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,
    points INTEGER NOT NULL,
    usage_source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS request_content (
    event_id INTEGER PRIMARY KEY REFERENCES usage_events(id),
    project TEXT NOT NULL,
    messages TEXT NOT NULL,
    answer TEXT NOT NULL
);
"""


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


class KeyStore:
    def __init__(self, path: str):
        self.path = path
        with self._conn() as c:
            c.executescript(SCHEMA)
            cols = {r["name"] for r in c.execute("PRAGMA table_info(keys)")}
            if "models" not in cols:  # upgrade a v1 database in place
                c.execute("ALTER TABLE keys ADD COLUMN models TEXT NOT NULL DEFAULT '*'")
            if "user_id" not in cols:
                c.execute("ALTER TABLE keys ADD COLUMN user_id INTEGER")
            if "project_id" not in cols:
                c.execute("ALTER TABLE keys ADD COLUMN project_id INTEGER")

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    # ---- create / list / revoke -------------------------------------------------

    def create(self, kind: str, *, minutes: int | None = None,
               tokens: int | None = None, label: str = "",
               models: list[str] | None = None, user_id: int | None = None,
               project_id: int | None = None) -> tuple[str, dict]:
        if kind == "time":
            if not minutes or minutes <= 0:
                raise ValueError("minutes must be > 0")
            expires_at, token_limit = time.time() + minutes * 60, None
        elif kind == "tokens":
            if not tokens or tokens <= 0:
                raise ValueError("tokens must be > 0")
            expires_at, token_limit = None, int(tokens)
        elif kind == "account":
            if not user_id or not project_id or not models:
                raise ValueError("account keys require an owner, project, and at least one model")
            expires_at, token_limit = None, None
        else:
            raise ValueError("kind must be 'time', 'tokens', or 'account'")

        raw = "sk-local-" + secrets.token_urlsafe(32)
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO keys (key_hash, prefix, label, kind, created_at, expires_at, token_limit, models,user_id,project_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (hash_key(raw), raw[:14], label, kind, time.time(), expires_at, token_limit,
                 json.dumps(sorted(models)) if models else "*", user_id, project_id),
            )
            row = c.execute("SELECT * FROM keys WHERE id=?", (cur.lastrowid,)).fetchone()
        return raw, self._view(row)

    def list(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM keys ORDER BY id DESC").fetchall()
        return [self._view(r) for r in rows]

    def revoke(self, key_id: int) -> bool:
        with self._conn() as c:
            cur = c.execute("UPDATE keys SET revoked=1 WHERE id=?", (key_id,))
        return cur.rowcount > 0

    # ---- auth / accounting ------------------------------------------------------

    def lookup(self, raw: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM keys WHERE key_hash=?", (hash_key(raw),)).fetchone()
        return self._view(row) if row else None

    def add_usage(self, key_id: int, tokens: int) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE keys SET tokens_used = tokens_used + ?, requests = requests + 1 WHERE id=?",
                (int(tokens), key_id),
            )

    def record_usage(self, key_id: int, *, model: str, upstream: str,
                     actual_model: str, elapsed_ms: int, status: str,
                     usage: dict, points: int, usage_source: str, content: dict | None = None) -> None:
        def count(name):
            value = usage.get(name)
            return value if type(value) is int and value >= 0 else None
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                cur = c.execute("INSERT INTO usage_events (recorded_at,key_id,model,upstream,"
                          "actual_model,elapsed_ms,status,prompt_tokens,completion_tokens,"
                          "total_tokens,points,usage_source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          (time.time(), key_id, model, upstream, actual_model, elapsed_ms,
                           status, count('prompt_tokens'), count('completion_tokens'),
                           count('total_tokens'), points, usage_source))
                if content is not None:
                    c.execute("INSERT INTO request_content VALUES (?,?,?,?)",
                              (cur.lastrowid, content['project'],
                               json.dumps(content['messages'], ensure_ascii=False),
                               content['answer']))
                c.execute("UPDATE keys SET tokens_used=tokens_used+?,requests=requests+1 WHERE id=?",
                          (points, key_id))
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise

    def usage(self, limit: int = 100) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT u.*, k.label AS project, EXISTS(SELECT 1 FROM request_content c "
                "WHERE c.event_id=u.id) AS has_content FROM usage_events u "
                "JOIN keys k ON k.id=u.key_id ORDER BY u.id DESC LIMIT ?",
                (min(max(limit, 1), 1000),))]

    def content(self, event_id: int) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM request_content WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            return None
        return {**dict(row), 'messages': json.loads(row['messages'])}

    # ---- helpers ---------------------------------------------------------------

    @staticmethod
    def _view(row: sqlite3.Row) -> dict:
        d = dict(row)
        d.pop("key_hash", None)
        d["models"] = "*" if d["models"] == "*" else json.loads(d["models"])
        now = time.time()
        if d["revoked"]:
            status = "revoked"
        elif d["kind"] == "time":
            status = "active" if d["expires_at"] > now else "expired"
            d["remaining_seconds"] = max(0, int(d["expires_at"] - now))
        elif d["kind"] == "tokens":
            status = "active" if d["tokens_used"] < d["token_limit"] else "exhausted"
            d["remaining_tokens"] = max(0, d["token_limit"] - d["tokens_used"])
        else:
            status = "active"
        d["status"] = status
        return d
