"""Authentication, roles and request security for the team edition.

Roles
-----
admin   - everything, including Team & users, Settings, audit log, backups,
          deleting any record, bulk actions and import.
user    - full CRM use (create / edit any shared record, own tasks, send email,
          connect their own mailbox). Can delete records they own (if enabled)
          and import/export when the admin allows it.
viewer  - read-only access (dashboards, records, reports).

Security controls
-----------------
* Passwords hashed with PBKDF2-SHA256 (werkzeug, 600k iterations) + minimum length.
* Account lock-out after 5 failed attempts (15 minutes).
* CSRF token required on every state-changing request.
* Idle session timeout (Settings > Sign-in & security) and server-side
  "sign out everywhere" via a per-user session version.
* Security headers (CSP, frame denial, no-sniff, referrer policy).
"""
import hmac
import secrets
from datetime import datetime, timedelta
from functools import wraps

from flask import abort, flash, g, jsonify, make_response, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from . import db

ROLES = {"admin": "Administrator", "user": "User", "viewer": "Read-only"}
MAX_FAILED = 5
LOCK_MINUTES = 15

# endpoints reachable without signing in
PUBLIC_ENDPOINTS = {"login", "static", "setup", "auth_ms_start", "auth_ms_callback", "healthz"}
# endpoints a viewer may POST to
VIEWER_POST_OK = {"logout", "profile", "profile_password", "login", "profile_signout_all",
                  "profile_ms_connect", "profile_ms_test", "profile_ms_disconnect", "profile_sync"}


# ------------------------------------------------------------------ passwords
def hash_password(pw):
    return generate_password_hash(pw, method="pbkdf2:sha256:600000")


def check_password(user, pw):
    return bool(user and user["password_hash"] and check_password_hash(user["password_hash"], pw or ""))


def password_problem(pw):
    """Return an error string if the password fails policy, else None."""
    n = int(db.get_setting("password_min_length") or 12)
    if len(pw or "") < n:
        return f"Password must be at least {n} characters."
    classes = sum([any(c.islower() for c in pw), any(c.isupper() for c in pw),
                   any(c.isdigit() for c in pw), any(not c.isalnum() for c in pw)])
    if classes < 3 and len(pw) < 16:
        return ("Use at least three of: lowercase, uppercase, numbers, symbols - "
                "or a passphrase of 16+ characters.")
    return None


def temp_password():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    core = "".join(secrets.choice(alphabet) for _ in range(12))
    return core[:4] + "-" + core[4:8] + "-" + core[8:] + "!7"


# ------------------------------------------------------------------ lock-out
def is_locked(user):
    if not user or not user["locked_until"]:
        return False
    return user["locked_until"] > db.now_iso()


def register_failure(user):
    if not user:
        return
    n = (user["failed_logins"] or 0) + 1
    locked = None
    if n >= MAX_FAILED:
        locked = (datetime.now() + timedelta(minutes=LOCK_MINUTES)).replace(microsecond=0).isoformat(sep=" ")
        n = 0
    db.execute("UPDATE users SET failed_logins=?, locked_until=? WHERE id=?", (n, locked, user["id"]))


# ------------------------------------------------------------------ sessions
def login_user(user, method="password"):
    session.clear()
    session["uid"] = user["id"]
    session["sv"] = user["session_version"]
    session["last"] = datetime.now().timestamp()
    session["csrf"] = secrets.token_urlsafe(32)
    session.permanent = True
    db.execute("UPDATE users SET last_login=?, failed_logins=0, locked_until=NULL WHERE id=?",
               (db.now_iso(), user["id"]))
    db.audit(user, "login", "user", user["id"], f"method={method}", client_ip())


def logout_user():
    u = getattr(g, "user", None)
    if u is not None:
        db.audit(u, "logout", "user", u["id"], "", client_ip())
    session.clear()


def bump_session_version(uid):
    """Invalidate every existing session for this user (sign out everywhere)."""
    db.execute("UPDATE users SET session_version = session_version + 1 WHERE id=?", (uid,))


def client_ip():
    return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip() if request else "") or \
        (request.remote_addr if request else "")


def load_user():
    """Populate g.user from the session (None when signed out or expired)."""
    g.user = None
    uid = session.get("uid")
    if not uid:
        return
    u = db.user(uid)
    if not u or not u["active"] or u["session_version"] != session.get("sv"):
        session.clear()
        return
    timeout = int(db.get_setting("session_timeout_min") or 480)
    last = session.get("last", 0)
    if timeout and datetime.now().timestamp() - last > timeout * 60:
        session.clear()
        flash("Your session expired. Please sign in again.", "error")
        return
    session["last"] = datetime.now().timestamp()
    g.user = u


# ------------------------------------------------------------------ CSRF
def csrf_token():
    tok = session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        session["csrf"] = tok
    return tok


def check_csrf():
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or ""
    if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
        if request.headers.get("X-Requested-With") or request.is_json:
            abort(make_response(jsonify(ok=False, error="Security token expired - reload the page."), 400))
        flash("Your form expired (security token). Please try again.", "error")
        abort(redirect(request.referrer or url_for("dashboard")))


# ------------------------------------------------------------------ permissions
def is_admin(u=None):
    u = u if u is not None else getattr(g, "user", None)
    return bool(u and u["role"] == "admin")


def can_edit(u=None):
    u = u if u is not None else getattr(g, "user", None)
    return bool(u and u["role"] in ("admin", "user"))


def can_delete(owner_id, u=None):
    u = u if u is not None else getattr(g, "user", None)
    if not u:
        return False
    if u["role"] == "admin":
        return True
    return (u["role"] == "user" and db.get_setting("user_can_delete_own") == "1"
            and owner_id is not None and owner_id == u["id"])


def can_import(u=None):
    u = u if u is not None else getattr(g, "user", None)
    return bool(u and (u["role"] == "admin" or (u["role"] == "user" and db.get_setting("user_can_import") == "1")))


def can_export(u=None):
    u = u if u is not None else getattr(g, "user", None)
    return bool(u and (u["role"] == "admin" or db.get_setting("user_can_export") == "1"))


def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not is_admin():
            abort(403)
        return fn(*a, **kw)
    return wrapper


# Pages that only make sense for people who can change data (viewers get 403 even on GET)
EDITOR_PAGES = {"contact_new", "contact_edit", "opp_new", "org_new", "compose", "import_view", "import_run",
                "import_sample"}


def guard_request():
    """before_request hook: setup wizard, sign-in, viewer read-only, CSRF."""
    ep = request.endpoint or ""
    if ep == "static" or ep == "healthz":
        return None
    if db.user_count() == 0 and ep != "setup":
        return redirect(url_for("setup"))
    load_user()
    if ep in PUBLIC_ENDPOINTS:
        if request.method == "POST" and ep in ("login", "setup"):
            check_csrf()
        return None
    if g.user is None:
        if request.headers.get("X-Requested-With") or request.is_json:
            return jsonify(ok=False, error="signed out"), 401
        return redirect(url_for("login", next=request.full_path if request.query_string else request.path))
    if g.user["must_change_password"] and ep not in ("profile", "profile_password", "logout"):
        flash("Please choose a new password to continue.", "error")
        return redirect(url_for("profile") + "#password")
    if g.user["role"] == "viewer" and ep in EDITOR_PAGES:
        abort(403)
    if ep.startswith("import") and not can_import():
        abort(403)
    if request.method == "POST":
        check_csrf()
        if g.user["role"] == "viewer" and ep not in VIEWER_POST_OK:
            abort(403)
    return None


def security_headers(resp):
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self' "
        "https://login.microsoftonline.com https://login.microsoftonline.us")
    if request.is_secure:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    if request.endpoint not in ("static",):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp
