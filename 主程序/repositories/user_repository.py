"""Users and hashed session persistence; no plaintext credential storage."""

from uuid import uuid4

from 主程序.repositories.import_repository import now
from 主程序.repositories.xlsx_reader import InputError

SAFE_COLUMNS = "id,username,role,active,version"


def safe(row):
    return {"id": row[0], "username": row[1], "role": row[2], "active": bool(row[3]), "version": row[4]}


def get_safe(c, user_id):
    row = c.execute("SELECT " + SAFE_COLUMNS + " FROM users WHERE id=?", (user_id,)).fetchone()
    if row is None:
        raise InputError("NOT_FOUND", 404)
    return safe(row)


def create(c, username, password_hash, role):
    if c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
        raise InputError("DUPLICATE_USERNAME", 409)
    user_id = uuid4().hex
    c.execute("INSERT INTO users VALUES (?,?,?,?,?,?)", (user_id, username, password_hash, role, 1, 0))
    return get_safe(c, user_id)


def list_users(c, *, page=1, page_size=50):
    total = c.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    rows = c.execute('SELECT ' + SAFE_COLUMNS + ' FROM users ORDER BY username,id LIMIT ? OFFSET ?',
                     (page_size, (page - 1) * page_size))
    return {'items': [safe(row) for row in rows], 'total': total, 'page': page, 'page_size': page_size}


def handlers(c):
    return [{"id": row[0], "name": row[1]} for row in c.execute("SELECT id,username FROM users WHERE role='HANDLER' AND active=1 ORDER BY username,id")]


def set_active(c, user_id, active, expected_version):
    user = get_safe(c, user_id)
    if user["version"] != expected_version:
        raise InputError("VERSION_CONFLICT", 409, current_version=user["version"])
    if not active and user["active"] and user["role"] == "ADMIN":
        if c.execute("SELECT COUNT(*) FROM users WHERE active=1 AND role='ADMIN'").fetchone()[0] <= 1:
            raise InputError("LAST_ACTIVE_ADMIN", 409)
    c.execute("UPDATE users SET active=?,version=version+1 WHERE id=? AND version=?", (int(active), user_id, expected_version))
    if not active:
        c.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (now(), user_id))
    return user, get_safe(c, user_id)


def new_session(c, user_id, token_hash, csrf_hash, expires_at):
    session_id = uuid4().hex
    c.execute("INSERT INTO sessions VALUES (?,?,?,?,?,NULL)", (session_id, user_id, token_hash, csrf_hash, expires_at))
    return session_id


def session(c, token_hash, timestamp):
    row = c.execute("SELECT s.id,s.csrf_hash,u.id,u.username,u.role FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.revoked_at IS NULL AND s.expires_at>? AND u.active=1", (token_hash, timestamp)).fetchone()
    if row is None:
        raise InputError("SESSION_EXPIRED", 401)
    return row
