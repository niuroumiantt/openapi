"""SQLite storage for API keys. Only key hashes are stored, never the raw key."""
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
    models      TEXT    NOT NULL DEFAULT '*'   -- JSON list of allowed public model names, or '*'
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
               models: list[str] | None = None) -> tuple[str, dict]:
        if kind == "time":
            if not minutes or minutes <= 0:
                raise ValueError("minutes must be > 0")
            expires_at, token_limit = time.time() + minutes * 60, None
        elif kind == "tokens":
            if not tokens or tokens <= 0:
                raise ValueError("tokens must be > 0")
            expires_at, token_limit = None, int(tokens)
        else:
            raise ValueError("kind must be 'time' or 'tokens'")

        raw = "sk-local-" + secrets.token_urlsafe(32)
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO keys (key_hash, prefix, label, kind, created_at, expires_at, token_limit, models)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (hash_key(raw), raw[:14], label, kind, time.time(), expires_at, token_limit,
                 json.dumps(sorted(models)) if models else "*"),
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
        else:
            status = "active" if d["tokens_used"] < d["token_limit"] else "exhausted"
            d["remaining_tokens"] = max(0, d["token_limit"] - d["tokens_used"])
        d["status"] = status
        return d
