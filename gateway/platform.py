"""Identity, billing catalogue, and entitlement ledger for Semifly.

This is deliberately separate from the upstream-routing code.  Payment providers
are *evidence sources*, never the balance of record: only a verified provider
event may append a credit to an entitlement ledger.

SQLite keeps local development simple. Production must use the PostgreSQL
implementation planned for the release environment before customer launch.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member' CHECK(role IN ('member', 'system_admin')),
    email_verified INTEGER NOT NULL DEFAULT 0,
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    revoked_at REAL
);
CREATE TABLE IF NOT EXISTS email_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    purpose TEXT NOT NULL CHECK(purpose IN ('verify_email', 'reset_password')),
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    consumed_at REAL
);
CREATE TABLE IF NOT EXISTS login_attempts (
    attempt_key TEXT PRIMARY KEY,
    failures INTEGER NOT NULL,
    window_started_at REAL NOT NULL,
    locked_until REAL
);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_user_id INTEGER REFERENCES users(id),
    target_user_id INTEGER REFERENCES users(id),
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    created_at REAL NOT NULL,
    revoked_at REAL,
    UNIQUE(user_id, name)
);
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    model TEXT NOT NULL,
    token_amount INTEGER NOT NULL CHECK(token_amount > 0),
    price_cents INTEGER NOT NULL CHECK(price_cents >= 0),
    currency TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    provider TEXT NOT NULL CHECK(provider IN ('stripe', 'manual')),
    provider_checkout_id TEXT UNIQUE,
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'paid', 'expired', 'refunded')),
    created_at REAL NOT NULL,
    paid_at REAL
);
CREATE TABLE IF NOT EXISTS payment_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    provider_event_id TEXT NOT NULL,
    received_at REAL NOT NULL,
    payload_sha256 TEXT NOT NULL,
    UNIQUE(provider, provider_event_id)
);
CREATE TABLE IF NOT EXISTS entitlement_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    model TEXT NOT NULL,
    token_delta INTEGER NOT NULL,
    reason TEXT NOT NULL CHECK(reason IN ('purchase', 'usage', 'refund', 'admin_adjustment')),
    order_id INTEGER REFERENCES orders(id),
    request_id TEXT,
    created_at REAL NOT NULL,
    CHECK((reason = 'usage' AND token_delta <= 0) OR (reason != 'usage' AND token_delta >= 0)),
    UNIQUE(request_id, reason)
);
CREATE TABLE IF NOT EXISTS usage_reservations (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    model TEXT NOT NULL,
    reserved_tokens INTEGER NOT NULL CHECK(reserved_tokens > 0),
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_token_hash ON sessions(token_hash);
CREATE INDEX IF NOT EXISTS email_tokens_user_purpose ON email_tokens(user_id, purpose, expires_at);
CREATE INDEX IF NOT EXISTS entitlement_user_model ON entitlement_ledger(user_id, model);
CREATE INDEX IF NOT EXISTS reservations_user_model ON usage_reservations(user_id, model);
CREATE INDEX IF NOT EXISTS orders_user ON orders(user_id, created_at DESC);
"""

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^^@\s]{1,255}$")
USERNAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{2,31}$")
PASSWORDS = PasswordHasher()


class PlatformStore:
    def __init__(self, path: str):
        self.path = path
        with self._conn() as c:
            c.executescript(SCHEMA)
            reservation_columns = {r['name'] for r in c.execute('PRAGMA table_info(usage_reservations)')}
            if 'expires_at' not in reservation_columns:
                c.execute('ALTER TABLE usage_reservations ADD COLUMN expires_at REAL')
                c.execute('UPDATE usage_reservations SET expires_at=created_at+660 WHERE expires_at IS NULL')

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    @staticmethod
    def _user(row: sqlite3.Row) -> dict:
        return {k: row[k] for k in ('id', 'email', 'username', 'role', 'email_verified', 'disabled', 'created_at')}

    def create_user(self, *, email: str, username: str, password: str,
                    role: str = 'member', verified: bool = False) -> dict:
        email, username = email.strip().lower(), username.strip()
        if not EMAIL_RE.fullmatch(email):
            raise ValueError('a valid email address is required')
        if not USERNAME_RE.fullmatch(username):
            raise ValueError('username must be 3–32 letters, numbers, dots, dashes, or underscores')
        if len(password) < 12:
            raise ValueError('password must be at least 12 characters')
        if role not in ('member', 'system_admin'):
            raise ValueError('invalid role')
        with self._conn() as c:
            try:
                cur = c.execute('INSERT INTO users(email,username,password_hash,role,email_verified,created_at) VALUES(?,?,?,?,?,?)',
                    (email, username, PASSWORDS.hash(password), role, int(verified), time.time()))
            except sqlite3.IntegrityError as e:
                raise ValueError('email or username is already registered') from e
            row = c.execute('SELECT * FROM users WHERE id=?', (cur.lastrowid,)).fetchone()
        return self._user(row)

    def bootstrap_admin(self, *, email: str, password: str, username: str = 'admin') -> dict | None:
        """Create the one initial administrator. Never changes an existing account."""
        with self._conn() as c:
            existing = c.execute("SELECT * FROM users WHERE role='system_admin' LIMIT 1").fetchone()
        return self._user(existing) if existing else self.create_user(
            email=email, username=username, password=password, role='system_admin', verified=True)

    def authenticate(self, identity: str, password: str) -> dict | None:
        with self._conn() as c:
            row = c.execute('SELECT * FROM users WHERE email=? OR username=?',
                            (identity.strip().lower(), identity.strip())).fetchone()
        if not row or row['disabled']:
            return None
        try:
            valid = PASSWORDS.verify(row['password_hash'], password)
        except (VerifyMismatchError, InvalidHashError):
            return None
        if valid and PASSWORDS.check_needs_rehash(row['password_hash']):
            with self._conn() as c:
                c.execute('UPDATE users SET password_hash=? WHERE id=?', (PASSWORDS.hash(password), row['id']))
        return self._user(row) if valid else None

    def _attempt_key(self, identity: str, remote_addr: str) -> str:
        return self._hash(f"{identity.strip().lower()}\0{remote_addr}")

    def login_allowed(self, identity: str, remote_addr: str) -> bool:
        with self._conn() as c:
            row = c.execute('SELECT locked_until FROM login_attempts WHERE attempt_key=?',
                            (self._attempt_key(identity, remote_addr),)).fetchone()
        return not row or row['locked_until'] is None or row['locked_until'] <= time.time()

    def record_login_failure(self, identity: str, remote_addr: str) -> None:
        """Five failures in 15 minutes locks this identity/IP combination for 15 minutes."""
        key, now = self._attempt_key(identity, remote_addr), time.time()
        with self._conn() as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                row = c.execute('SELECT * FROM login_attempts WHERE attempt_key=?', (key,)).fetchone()
                failures = 1 if not row or row['window_started_at'] < now - 900 else row['failures'] + 1
                started = now if not row or row['window_started_at'] < now - 900 else row['window_started_at']
                locked = now + 900 if failures >= 5 else None
                c.execute('INSERT INTO login_attempts(attempt_key,failures,window_started_at,locked_until) VALUES(?,?,?,?) '
                          'ON CONFLICT(attempt_key) DO UPDATE SET failures=excluded.failures,window_started_at=excluded.window_started_at,locked_until=excluded.locked_until',
                          (key, failures, started, locked))
                c.execute('COMMIT')
            except Exception:
                c.execute('ROLLBACK')
                raise

    def clear_login_failures(self, identity: str, remote_addr: str) -> None:
        with self._conn() as c:
            c.execute('DELETE FROM login_attempts WHERE attempt_key=?', (self._attempt_key(identity, remote_addr),))

    def create_session(self, user_id: int, *, lifetime_seconds: int = 60 * 60 * 24 * 14) -> str:
        raw, now = secrets.token_urlsafe(48), time.time()
        with self._conn() as c:
            c.execute('INSERT INTO sessions(token_hash,user_id,created_at,expires_at,last_seen_at) VALUES(?,?,?,?,?)',
                      (self._hash(raw), user_id, now, now + lifetime_seconds, now))
        return raw

    def issue_email_token(self, user_id: int, purpose: str, *, lifetime_seconds: int) -> str:
        if purpose not in ('verify_email', 'reset_password'):
            raise ValueError('invalid email token purpose')
        raw, now = secrets.token_urlsafe(48), time.time()
        with self._conn() as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                recent = c.execute('SELECT 1 FROM email_tokens WHERE user_id=? AND purpose=? AND created_at>? LIMIT 1',
                                   (user_id, purpose, now - 60)).fetchone()
                if recent:
                    raise ValueError('please wait before requesting another email')
                # There is only one live link of each purpose per account.
                c.execute('UPDATE email_tokens SET consumed_at=? WHERE user_id=? AND purpose=? AND consumed_at IS NULL',
                          (now, user_id, purpose))
                c.execute('INSERT INTO email_tokens(token_hash,user_id,purpose,created_at,expires_at) VALUES(?,?,?,?,?)',
                          (self._hash(raw), user_id, purpose, now, now + lifetime_seconds))
                c.execute('COMMIT')
            except Exception:
                c.execute('ROLLBACK')
                raise
        return raw

    def consume_email_token(self, raw: str, purpose: str) -> dict | None:
        now = time.time()
        with self._conn() as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                token = c.execute('SELECT * FROM email_tokens WHERE token_hash=? AND purpose=? AND consumed_at IS NULL AND expires_at>?',
                                  (self._hash(raw), purpose, now)).fetchone()
                if not token:
                    c.execute('ROLLBACK')
                    return None
                c.execute('UPDATE email_tokens SET consumed_at=? WHERE id=?', (now, token['id']))
                user = c.execute('SELECT * FROM users WHERE id=?', (token['user_id'],)).fetchone()
                c.execute('COMMIT')
            except Exception:
                c.execute('ROLLBACK')
                raise
        return self._user(user)

    def verify_email(self, raw: str) -> dict | None:
        user = self.consume_email_token(raw, 'verify_email')
        if not user:
            return None
        with self._conn() as c:
            c.execute('UPDATE users SET email_verified=1 WHERE id=?', (user['id'],))
            row = c.execute('SELECT * FROM users WHERE id=?', (user['id'],)).fetchone()
        return self._user(row)

    def reset_password(self, raw: str, password: str) -> bool:
        if len(password) < 12:
            raise ValueError('password must be at least 12 characters')
        user = self.consume_email_token(raw, 'reset_password')
        if not user:
            return False
        with self._conn() as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                c.execute('UPDATE users SET password_hash=? WHERE id=?', (PASSWORDS.hash(password), user['id']))
                # Password reset invalidates every pre-existing browser session.
                c.execute('UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL', (time.time(), user['id']))
                c.execute('COMMIT')
            except Exception:
                c.execute('ROLLBACK')
                raise
        return True

    def pending_user_by_email(self, email: str) -> dict | None:
        with self._conn() as c:
            row = c.execute('SELECT * FROM users WHERE email=? AND disabled=0', (email.strip().lower(),)).fetchone()
        return self._user(row) if row else None

    def audit(self, *, actor_user_id: int | None, action: str, target_user_id: int | None = None,
              detail: dict | None = None) -> None:
        with self._conn() as c:
            c.execute('INSERT INTO audit_events(actor_user_id,target_user_id,action,detail,created_at) VALUES(?,?,?,?,?)',
                      (actor_user_id, target_user_id, action, json.dumps(detail or {}, ensure_ascii=False), time.time()))

    def admin_users(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute('SELECT id,email,username,role,email_verified,disabled,created_at FROM users ORDER BY id').fetchall()
        return [dict(r) for r in rows]

    def update_user(self, *, actor_user_id: int, target_user_id: int, role: str | None = None,
                    disabled: bool | None = None) -> dict:
        if role is not None and role not in ('member', 'system_admin'):
            raise ValueError('invalid role')
        if disabled is not None and type(disabled) is not bool:
            raise ValueError('disabled must be a boolean')
        with self._conn() as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                target = c.execute('SELECT * FROM users WHERE id=?', (target_user_id,)).fetchone()
                if not target:
                    raise ValueError('user was not found')
                final_role = role if role is not None else target['role']
                final_disabled = int(disabled) if disabled is not None else target['disabled']
                losing_last_admin = target['role'] == 'system_admin' and not target['disabled'] and \
                    (final_role != 'system_admin' or final_disabled)
                if losing_last_admin:
                    admins = c.execute("SELECT COUNT(*) AS n FROM users WHERE role='system_admin' AND disabled=0").fetchone()['n']
                    if admins <= 1:
                        raise ValueError('cannot remove the last active system administrator')
                c.execute('UPDATE users SET role=?,disabled=? WHERE id=?', (final_role, final_disabled, target_user_id))
                c.execute('INSERT INTO audit_events(actor_user_id,target_user_id,action,detail,created_at) VALUES(?,?,?,?,?)',
                          (actor_user_id, target_user_id, 'user.updated',
                           json.dumps({'role': final_role, 'disabled': bool(final_disabled)}), time.time()))
                row = c.execute('SELECT * FROM users WHERE id=?', (target_user_id,)).fetchone()
                c.execute('COMMIT')
            except Exception:
                c.execute('ROLLBACK')
                raise
        return self._user(row)

    def session_user(self, raw: str) -> dict | None:
        now = time.time()
        with self._conn() as c:
            row = c.execute('SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id '
                            'WHERE s.token_hash=? AND s.revoked_at IS NULL AND s.expires_at>?',
                            (self._hash(raw), now)).fetchone()
            if row:
                c.execute('UPDATE sessions SET last_seen_at=? WHERE token_hash=?', (now, self._hash(raw)))
        return self._user(row) if row and not row['disabled'] else None

    def revoke_session(self, raw: str) -> None:
        with self._conn() as c:
            c.execute('UPDATE sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL',
                      (time.time(), self._hash(raw)))

    def create_project(self, user_id: int, name: str) -> dict:
        name = name.strip()
        if not 1 <= len(name) <= 64:
            raise ValueError('project name must be 1–64 characters')
        with self._conn() as c:
            try:
                cur = c.execute('INSERT INTO projects(user_id,name,created_at) VALUES(?,?,?)', (user_id, name, time.time()))
            except sqlite3.IntegrityError as e:
                raise ValueError('a project with that name already exists') from e
            return dict(c.execute('SELECT * FROM projects WHERE id=?', (cur.lastrowid,)).fetchone())

    def projects(self, user_id: int) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute('SELECT * FROM projects WHERE user_id=? AND revoked_at IS NULL ORDER BY id', (user_id,))]

    def owns_project(self, user_id: int, project_id: int) -> bool:
        with self._conn() as c:
            return c.execute('SELECT 1 FROM projects WHERE id=? AND user_id=? AND revoked_at IS NULL',
                             (project_id, user_id)).fetchone() is not None

    def api_keys(self, user_id: int) -> list[dict]:
        """Safe customer key metadata. The raw key hash is never selected."""
        with self._conn() as c:
            rows = c.execute('SELECT k.id,k.prefix,k.label,k.models,k.project_id,k.created_at,k.revoked,p.name AS project '
                             'FROM keys k JOIN projects p ON p.id=k.project_id '
                             'WHERE k.user_id=? AND k.kind=\'account\' ORDER BY k.id DESC', (user_id,)).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value['models'] = json.loads(value['models'])
            value['status'] = 'revoked' if value.pop('revoked') else 'active'
            result.append(value)
        return result

    def create_product(self, *, code: str, model: str, token_amount: int,
                       price_cents: int, currency: str = 'usd') -> dict:
        code, model, currency = code.strip().lower(), model.strip(), currency.strip().lower()
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{2,63}', code):
            raise ValueError('invalid product code')
        if not model or token_amount <= 0 or price_cents < 0 or not re.fullmatch(r'[a-z]{3}', currency):
            raise ValueError('invalid product')
        with self._conn() as c:
            try:
                cur = c.execute('INSERT INTO products(code,model,token_amount,price_cents,currency,created_at) VALUES(?,?,?,?,?,?)',
                    (code, model, token_amount, price_cents, currency, time.time()))
            except sqlite3.IntegrityError as e:
                raise ValueError('product code already exists') from e
            return dict(c.execute('SELECT * FROM products WHERE id=?', (cur.lastrowid,)).fetchone())

    def catalogue(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute('SELECT * FROM products WHERE active=1 ORDER BY price_cents, id')]

    def product(self, product_id: int) -> dict | None:
        with self._conn() as c:
            row = c.execute('SELECT * FROM products WHERE id=? AND active=1', (product_id,)).fetchone()
        return dict(row) if row else None

    def create_order(self, user_id: int, product: dict, *, provider: str, checkout_id: str | None = None) -> dict:
        with self._conn() as c:
            cur = c.execute('INSERT INTO orders(user_id,product_id,provider,provider_checkout_id,amount_cents,currency,status,created_at) VALUES(?,?,?,?,?,?,?,?)',
                (user_id, product['id'], provider, checkout_id, product['price_cents'], product['currency'], 'pending', time.time()))
            return dict(c.execute('SELECT * FROM orders WHERE id=?', (cur.lastrowid,)).fetchone())

    def attach_checkout(self, order_id: int, checkout_id: str) -> None:
        with self._conn() as c:
            cur = c.execute('UPDATE orders SET provider_checkout_id=? WHERE id=? AND status=\'pending\'',
                            (checkout_id, order_id))
        if cur.rowcount != 1:
            raise ValueError('pending order was not found')

    def record_paid_order(self, *, provider: str, event_id: str, payload: bytes, checkout_id: str) -> dict | None:
        """Idempotently turn a verified provider event into a purchase entitlement."""
        with self._conn() as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                try:
                    c.execute('INSERT INTO payment_events(provider,provider_event_id,received_at,payload_sha256) VALUES(?,?,?,?)',
                              (provider, event_id, time.time(), hashlib.sha256(payload).hexdigest()))
                except sqlite3.IntegrityError:
                    c.execute('ROLLBACK')
                    return None
                order = c.execute('SELECT o.*, p.model, p.token_amount FROM orders o JOIN products p ON p.id=o.product_id '
                                  'WHERE o.provider=? AND o.provider_checkout_id=?', (provider, checkout_id)).fetchone()
                if not order or order['status'] != 'pending':
                    c.execute('COMMIT')
                    return None
                c.execute("UPDATE orders SET status='paid',paid_at=? WHERE id=?", (time.time(), order['id']))
                c.execute('INSERT INTO entitlement_ledger(user_id,model,token_delta,reason,order_id,created_at) VALUES(?,?,?,?,?,?)',
                          (order['user_id'], order['model'], order['token_amount'], 'purchase', order['id'], time.time()))
                c.execute('COMMIT')
                return dict(order)
            except Exception:
                c.execute('ROLLBACK')
                raise

    def balances(self, user_id: int) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                'SELECT l.model, SUM(l.token_delta) - COALESCE((SELECT SUM(r.reserved_tokens) FROM usage_reservations r '
                'WHERE r.user_id=l.user_id AND r.model=l.model AND r.expires_at>?),0) AS remaining_tokens '
                'FROM entitlement_ledger l WHERE l.user_id=? GROUP BY l.user_id,l.model ORDER BY l.model', (time.time(), user_id))]

    def can_use_model(self, user_id: int, model: str) -> bool:
        return any(b['model'] == model and b['remaining_tokens'] > 0 for b in self.balances(user_id))

    def reserve_model_tokens(self, user_id: int, model: str, tokens: int) -> str | None:
        """Reserve a conservative upper bound before forwarding a customer request."""
        if tokens <= 0:
            return None
        reservation_id, now = secrets.token_urlsafe(18), time.time()
        with self._conn() as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                balance = c.execute('SELECT COALESCE(SUM(token_delta),0) AS balance FROM entitlement_ledger '
                                    'WHERE user_id=? AND model=?', (user_id, model)).fetchone()['balance']
                c.execute('DELETE FROM usage_reservations WHERE expires_at<=?', (now,))
                reserved = c.execute('SELECT COALESCE(SUM(reserved_tokens),0) AS reserved FROM usage_reservations '
                                     'WHERE user_id=? AND model=?', (user_id, model)).fetchone()['reserved']
                if balance - reserved < tokens:
                    c.execute('ROLLBACK')
                    return None
                c.execute('INSERT INTO usage_reservations(id,user_id,model,reserved_tokens,created_at,expires_at) VALUES(?,?,?,?,?,?)',
                          (reservation_id, user_id, model, tokens, now, now + 660))
                c.execute('COMMIT')
                return reservation_id
            except Exception:
                c.execute('ROLLBACK')
                raise

    def settle_reservation(self, reservation_id: str, actual_tokens: int) -> bool:
        """Replace an active reservation with its actual, provider-reported usage."""
        if actual_tokens < 0:
            raise ValueError('actual token usage cannot be negative')
        with self._conn() as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                reservation = c.execute('SELECT * FROM usage_reservations WHERE id=?', (reservation_id,)).fetchone()
                if not reservation:
                    c.execute('ROLLBACK')
                    return False
                capped = actual_tokens > reservation['reserved_tokens']
                charged_tokens = min(actual_tokens, reservation['reserved_tokens'])
                c.execute('DELETE FROM usage_reservations WHERE id=?', (reservation_id,))
                if charged_tokens:
                    c.execute('INSERT INTO entitlement_ledger(user_id,model,token_delta,reason,request_id,created_at) VALUES(?,?,?,?,?,?)',
                              (reservation['user_id'], reservation['model'], -charged_tokens, 'usage', reservation_id, time.time()))
                c.execute('COMMIT')
                return not capped
            except Exception:
                c.execute('ROLLBACK')
                raise

    def dashboard(self, user_id: int) -> dict:
        with self._conn() as c:
            orders = [dict(r) for r in c.execute('SELECT o.*,p.code,p.model,p.token_amount FROM orders o JOIN products p ON p.id=o.product_id '
                                                 'WHERE o.user_id=? ORDER BY o.id DESC LIMIT 20', (user_id,))]
            usage = [dict(r) for r in c.execute(
                'SELECT u.id,u.recorded_at,u.model,u.actual_model,u.elapsed_ms,u.status,u.prompt_tokens,'
                'u.completion_tokens,u.total_tokens,u.usage_source,k.project_id,p.name AS project '
                'FROM usage_events u JOIN keys k ON k.id=u.key_id JOIN projects p ON p.id=k.project_id '
                'WHERE k.user_id=? AND k.kind=\'account\' ORDER BY u.id DESC LIMIT 100', (user_id,))]
        return {'balances': self.balances(user_id), 'projects': self.projects(user_id),
                'api_keys': self.api_keys(user_id), 'orders': orders, 'usage': usage}
