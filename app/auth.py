import hashlib
import hmac
import re
import secrets
from datetime import timedelta
from functools import wraps

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import InvalidToken
from flask import current_app, g, redirect, request, url_for

from .crypto import _fernet
from .db import audit, get_db, in_seconds, iso, new_id, parse_iso, tx, utcnow
from .errors import ApiFail

SESSION_COOKIE = "__Host-bridge_session"
PRELOGIN_COOKIE = "__Host-bridge_prelogin"
IDLE_SECONDS = 30 * 60
ABSOLUTE_SECONDS = 8 * 3600
PRELOGIN_TTL = 600
LOGIN_WINDOW = 15 * 60
LOGIN_MAX_FAILURES = 10
RECENT_LOGIN_SECONDS = 300
PASSWORD_MIN, PASSWORD_MAX = 15, 128
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
PASSWORD_EXEMPT = {"routes.password_page", "routes.change_password", "routes.logout", "routes.reauth"}
_EMAIL_LOGIN_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}$")

_hasher = PasswordHasher()
_DUMMY_HASH = _hasher.hash("dummy-password-for-timing-only")


def wants_json() -> bool:
    return request.path.startswith("/api/")


def normalize_email(email) -> str:
    value = str(email or "").strip().lower()
    if not _EMAIL_LOGIN_RE.match(value):
        raise ApiFail(422, "INVALID_EMAIL", "Enter a valid email address.", {"email": "Invalid email address."})
    return value


def validate_password(password) -> None:
    if not isinstance(password, str) or not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        raise ApiFail(422, "PASSWORD_POLICY", f"Password must be {PASSWORD_MIN}-{PASSWORD_MAX} characters.",
                      {"password": f"Use {PASSWORD_MIN}-{PASSWORD_MAX} characters."})


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def _verify(stored: str, password: str) -> bool:
    if not isinstance(password, str):
        return False
    try:
        return _hasher.verify(stored, password)
    except (VerificationError, InvalidHashError):
        return False


def temp_password() -> str:
    return secrets.token_urlsafe(16)


def create_user(conn, email, role, password, must_change, actor) -> str:
    validate_password(password)
    uid, now = new_id(), iso()
    with tx(conn):
        try:
            conn.execute(
                "INSERT INTO users (id, email, password_hash, role, active, must_change_password, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
                (uid, normalize_email(email), hash_password(password), role, int(must_change), now, now))
        except Exception as exc:
            if "UNIQUE" in str(exc):
                raise ApiFail(409, "USER_EXISTS", "An account with this email already exists.") from exc
            raise
        audit(conn, actor, "USER_CREATED", uid, {"role": role})
    return uid


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_session(conn, user_id: str) -> tuple[str, str]:
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    now = iso()
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, csrf_token, created_at, last_seen_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
        (digest(token), user_id, csrf, now, now, in_seconds(ABSOLUTE_SECONDS)))
    return token, csrf


def invalidate_sessions(conn, user_id: str) -> None:
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def set_session_cookie(response, token: str) -> None:
    response.set_cookie(SESSION_COOKIE, token, secure=True, httponly=True, samesite="Lax", path="/")


def load_session() -> None:
    g.user, g.session = None, None
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return
    conn = get_db()
    row = conn.execute(
        "SELECT s.*, u.email, u.role, u.active, u.must_change_password FROM sessions s"
        " JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?", (digest(token),)).fetchone()
    if row is None:
        return
    now = utcnow()
    idle_deadline = parse_iso(row["last_seen_at"]) + timedelta(seconds=IDLE_SECONDS)
    if not row["active"] or now >= parse_iso(row["expires_at"]) or now >= idle_deadline:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (row["token_hash"],))
        return
    if (now - parse_iso(row["last_seen_at"])).total_seconds() > 60:
        conn.execute("UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?", (iso(now), row["token_hash"]))
    g.session = row
    g.user = {"id": row["user_id"], "email": row["email"], "role": row["role"],
              "must_change_password": bool(row["must_change_password"]), "csrf": row["csrf_token"]}


def check_request_origin() -> None:
    if request.method not in UNSAFE:
        return
    origin = request.headers.get("Origin")
    # Referrer-Policy: no-referrer makes browsers send "Origin: null" on same-origin form posts.
    # Sec-Fetch-Site cannot be set by page script, so it proves same-origin for that case only.
    same_origin_null = origin == "null" and request.headers.get("Sec-Fetch-Site") == "same-origin"
    if origin != current_app.config["APP_ORIGIN"] and not same_origin_null:
        raise ApiFail(403, "CSRF_ORIGIN", "Request origin rejected.")
    if request.endpoint == "routes.login_post" or g.get("user") is None:
        return
    supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
    if not hmac.compare_digest(supplied.encode(), g.user["csrf"].encode()):
        raise ApiFail(403, "CSRF_INVALID", "CSRF token missing or invalid.")


def prelogin_issue(response) -> str:
    nonce = secrets.token_urlsafe(24)
    response.set_cookie(PRELOGIN_COOKIE, _fernet().encrypt(nonce.encode()).decode(), max_age=PRELOGIN_TTL,
                        secure=True, httponly=True, samesite="Strict", path="/")
    return nonce


# Login CSRF: form nonce must equal the Fernet-encrypted nonce in the cookie.
def prelogin_valid() -> bool:
    cookie, supplied = request.cookies.get(PRELOGIN_COOKIE, ""), request.form.get("csrf_token", "")
    try:
        nonce = _fernet().decrypt(cookie.encode(), ttl=PRELOGIN_TTL).decode()
    except (InvalidToken, ValueError):
        return False
    return hmac.compare_digest(nonce.encode(), supplied.encode())


def _limit_keys(email: str, ip: str) -> list[str]:
    return [digest("acct:" + email), digest("ip:" + (ip or "unknown"))]


def login_blocked(conn, email: str, ip: str) -> bool:
    now = iso()
    for key in _limit_keys(email, ip):
        row = conn.execute("SELECT cooldown_until FROM login_limits WHERE key_hash = ?", (key,)).fetchone()
        if row and row["cooldown_until"] and row["cooldown_until"] > now:
            return True
    return False


def _record_failure(conn, email: str, ip: str) -> None:
    now = utcnow()
    with tx(conn):
        for key in _limit_keys(email, ip):
            row = conn.execute("SELECT * FROM login_limits WHERE key_hash = ?", (key,)).fetchone()
            fresh = row is None or (now - parse_iso(row["window_start"])).total_seconds() > LOGIN_WINDOW
            count = 1 if fresh else row["count"] + 1
            start = iso(now) if fresh else row["window_start"]
            cooldown = in_seconds(LOGIN_WINDOW) if count >= LOGIN_MAX_FAILURES else None
            conn.execute(
                "INSERT INTO login_limits (key_hash, window_start, count, cooldown_until) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(key_hash) DO UPDATE SET window_start = excluded.window_start,"
                " count = excluded.count, cooldown_until = excluded.cooldown_until",
                (key, start, count, cooldown))


def authenticate(email: str, password: str, ip: str):
    conn = get_db()
    email = str(email or "").strip().lower()
    if login_blocked(conn, email, ip):
        _verify(_DUMMY_HASH, "x")
        return None
    user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    # Dummy hash keeps timing equal for unknown accounts.
    ok = _verify(user["password_hash"] if user else _DUMMY_HASH, password or "")
    if not (user and ok and user["active"]):
        _record_failure(conn, email, ip)
        return None
    with tx(conn):
        conn.execute("DELETE FROM login_limits WHERE key_hash = ?", (_limit_keys(email, ip)[0],))
        conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (iso(), user["id"]))
        audit(conn, user["id"], "LOGIN", user["id"])
    return user


def start_session(user_id: str, response) -> None:
    conn = get_db()
    with tx(conn):
        token, _ = issue_session(conn, user_id)
    set_session_cookie(response, token)


def rotate_session(user_id: str, old_hash: str) -> tuple[str, str]:
    conn = get_db()
    with tx(conn):
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (old_hash,))
        return issue_session(conn, user_id)


def change_password(user_id: str, new_password: str, response) -> None:
    validate_password(new_password)
    conn = get_db()
    with tx(conn):
        conn.execute("UPDATE users SET password_hash = ?, must_change_password = 0, updated_at = ? WHERE id = ?",
                     (hash_password(new_password), iso(), user_id))
        audit(conn, user_id, "PASSWORD_CHANGED", user_id)
        invalidate_sessions(conn, user_id)
        token, _ = issue_session(conn, user_id)
    set_session_cookie(response, token)


def verify_current_password(user_id: str, password: str) -> bool:
    row = get_db().execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
    return bool(row) and _verify(row["password_hash"], password or "")


def require_recent_login() -> None:
    age = (utcnow() - parse_iso(g.session["created_at"])).total_seconds()
    if age > RECENT_LOGIN_SECONDS:
        raise ApiFail(403, "REAUTH_REQUIRED", "Confirm your password to continue.")


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if g.user is None:
            if wants_json():
                raise ApiFail(401, "UNAUTHENTICATED", "Sign in required.")
            return redirect(url_for("routes.login_get"))
        if g.user["must_change_password"] and request.endpoint not in PASSWORD_EXEMPT:
            if wants_json():
                raise ApiFail(403, "PASSWORD_CHANGE_REQUIRED", "Change your temporary password first.")
            return redirect(url_for("routes.password_page"))
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @login_required
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if g.user["role"] != "ADMIN":
            if wants_json():
                raise ApiFail(403, "FORBIDDEN", "Administrator access required.")
            return redirect(url_for("routes.intake"))
        return fn(*args, **kwargs)
    return wrapper
