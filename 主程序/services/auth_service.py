"""Bcrypt credentials, stable independent session secrets, and user administration."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import secrets
from threading import Lock
from time import monotonic

import bcrypt

from 主程序.domain.permissions import Actor, ROLES
from 主程序.repositories.database import database_transaction
from 主程序.repositories.import_repository import now
from 主程序.repositories import user_repository as users, audit_repository
from 主程序.repositories.workflow_repository import require_workflow
from 主程序.repositories.xlsx_reader import InputError
from 主程序.services.workflow_service import authorize, reject, verify_session


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def password_bytes(password, *, minimum=8):
    try:
        encoded = password.encode("utf-8") if isinstance(password, str) else b""
    except UnicodeEncodeError as exc:
        raise InputError("VALIDATION_ERROR", 422, fields=["password"]) from exc
    if not minimum <= len(encoded) <= 72:
        raise InputError("VALIDATION_ERROR", 422, fields=["password"])
    return encoded


def validate_credentials(username, password, role=None):
    if not isinstance(username, str) or not 1 <= len(username.strip()) <= 100 or username != username.strip():
        raise InputError("VALIDATION_ERROR", 422, fields=["username"])
    password_bytes(password)
    if role is not None and role not in ROLES:
        raise InputError("VALIDATION_ERROR", 422, fields=["role"])


@dataclass(frozen=True)
class SessionIdentity:
    actor: Actor
    username: str
    session_id: str
    csrf_hash: str

    def user(self):
        return {"id": self.actor.id, "username": self.username, "role": self.actor.role}


class LoginLimiter:
    """Single-worker bounded client limiter, shared across usernames to resist enumeration."""
    def __init__(self, limit=10, window=60, max_clients=4096):
        self.limit, self.window, self.max_clients = limit, window, max_clients
        self.entries, self.lock = {}, Lock()

    def check(self, client):
        timestamp = monotonic()
        with self.lock:
            self.entries = {k: v for k, v in self.entries.items() if timestamp - v[0] < self.window}
            start, count = self.entries.get(client, (timestamp, 0))
            if count >= self.limit or (client not in self.entries and len(self.entries) >= self.max_clients):
                raise InputError("LOGIN_RATE_LIMIT", 429)
            self.entries[client] = (start, count + 1)


class AuthService:
    def __init__(self, database, *, session_seconds=28800):
        self.database, self.session_seconds = database, session_seconds
        self.limiter = LoginLimiter()
        # Unknown usernames still pay bcrypt cost; no fixed/default login password.
        self.dummy_hash = bcrypt.hashpw(secrets.token_bytes(32), bcrypt.gensalt())

    def workflow_available(self):
        with database_transaction(self.database, readonly=True) as c:
            return c.execute("PRAGMA user_version").fetchone()[0] == 4

    def login(self, username, password, request_id):
        with database_transaction(self.database, readonly=True) as c:
            require_workflow(c)
            row = c.execute("SELECT id,password_hash,role,active FROM users WHERE username=?", (username,)).fetchone()
        encoded = password_bytes(password, minimum=1)
        valid = bcrypt.checkpw(encoded, row[1].encode("ascii") if row else self.dummy_hash)
        if not valid or row is None or not row[3]:
            error = InputError("INVALID_CREDENTIALS", 401)
            reject(self.database, None, request_id, "auth", "login", "LOGIN", error)
            raise error
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        expires = (datetime.now(timezone.utc) + timedelta(seconds=self.session_seconds)).isoformat()
        with database_transaction(self.database) as c:
            current = c.execute("SELECT role,active,password_hash FROM users WHERE id=?", (row[0],)).fetchone()
            if current is None or not current[1] or current[2] != row[1]:
                raise InputError("INVALID_CREDENTIALS", 401)
            users.new_session(c, row[0], digest(token), digest(csrf), expires)
            audit_repository.append(c, row[0], request_id, "user", row[0], "LOGIN", "SUCCESS")
        return {"id": row[0], "username": username, "role": current[0]}, token, csrf

    def identity(self, token):
        if not isinstance(token, str) or not 1 <= len(token) <= 256:
            raise InputError("SESSION_EXPIRED", 401)
        with database_transaction(self.database, readonly=True) as c:
            require_workflow(c)
            row = users.session(c, digest(token), now())
        return SessionIdentity(Actor(row[2], row[4]), row[3], row[0], row[1])

    def csrf(self, identity, token):
        if not isinstance(token, str) or len(token) > 256 or not hmac.compare_digest(digest(token), identity.csrf_hash):
            raise InputError("CSRF_FAILED", 403)

    def logout(self, identity, request_id):
        with database_transaction(self.database) as c:
            changed = c.execute("UPDATE sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL AND expires_at>? AND EXISTS(SELECT 1 FROM users WHERE id=? AND active=1)",
                                (now(), identity.session_id, now(), identity.actor.id))
            if changed.rowcount != 1:
                raise InputError("SESSION_EXPIRED", 401)
            audit_repository.append(c, identity.actor.id, request_id, "user", identity.actor.id, "LOGOUT", "SUCCESS")


def create_user(database, actor, username, password, role, request_id, *, bootstrap=False, session_id=None):
    if not bootstrap:
        authorize(actor, "users")
    validate_credentials(username, password, role)
    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")
    try:
        with database_transaction(database) as c:
            require_workflow(c)
            if bootstrap:
                if role != "ADMIN" or c.execute("SELECT 1 FROM users").fetchone() is not None:
                    raise InputError("BOOTSTRAP_NOT_EMPTY", 409)
            else:
                verify_session(c, actor, session_id)
                verify_admin(c, actor)
            user = users.create(c, username, password_hash, role)
            audit_repository.append(c, actor.id if actor else None, request_id, "user", user["id"], "CREATE_USER", "SUCCESS", after=user)
        return user
    except InputError as exc:
        reject(database, actor, request_id, "user", "create", "CREATE_USER", exc)
        raise


def verify_admin(c, actor):
    if c.execute("SELECT 1 FROM users WHERE id=? AND active=1 AND role='ADMIN'", (actor.id,)).fetchone() is None:
        raise InputError("SESSION_EXPIRED", 401)


def set_active(database, actor, user_id, active, expected_version, request_id, *, session_id=None):
    try:
        authorize(actor, "users")
        with database_transaction(database) as c:
            require_workflow(c)
            verify_session(c, actor, session_id)
            verify_admin(c, actor)
            before, after = users.set_active(c, user_id, active, expected_version)
            audit_repository.append(c, actor.id, request_id, "user", user_id, "SET_ACTIVE", "SUCCESS", before=before, after=after)
        return after
    except InputError as exc:
        reject(database, actor, request_id, "user", user_id, "SET_ACTIVE", exc)
        raise


def handlers(database, actor):
    authorize(actor, "handlers")
    with database_transaction(database, readonly=True) as c:
        require_workflow(c)
        return {"items": users.handlers(c)}
