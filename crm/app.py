"""Relay CRM - Flask application factory (team edition)."""
import logging
import os
from datetime import date, datetime, timedelta

from flask import Flask, g, render_template, request
from flask.sessions import SecureCookieSessionInterface

from . import __version__, auth, crypto, db, demo, sync
from .paths import STATIC_DIR, TEMPLATE_DIR
from .web import display_name, uname

log = logging.getLogger("relay")


class _SessionInterface(SecureCookieSessionInterface):
    """Mark the session cookie Secure whenever the request arrived over HTTPS
    (directly, or via a reverse proxy when RELAY_BEHIND_PROXY=1)."""

    def get_cookie_secure(self, app):
        try:
            return request.is_secure or app.config.get("SESSION_COOKIE_SECURE", False)
        except RuntimeError:
            return app.config.get("SESSION_COOKIE_SECURE", False)


def create_app(start_scheduler=True):
    db.init_db()
    app = Flask(__name__, template_folder=TEMPLATE_DIR, static_folder=STATIC_DIR)
    app.secret_key = crypto.secret_file("secret.key")
    app.session_interface = _SessionInterface()
    app.config.update(
        MAX_CONTENT_LENGTH=25 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_NAME="relay_session",
        SESSION_COOKIE_SECURE=os.environ.get("RELAY_SECURE_COOKIES") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    )
    if os.environ.get("RELAY_BEHIND_PROXY") == "1":
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

    if db.get_setting("mode") == "demo" and db.user_count() > 0:
        demo.seed()

    if start_scheduler and not os.environ.get("RELAY_NO_SCHEDULER"):
        sync.Scheduler().start()

    _filters(app)
    app.before_request(auth.guard_request)
    app.after_request(auth.security_headers)

    @app.context_processor
    def inject():
        s = db.get_settings()
        s["ms_client_secret"] = "set" if s.get("ms_client_secret") else ""
        u = getattr(g, "user", None)
        ctx = {"S": s, "version": __version__, "me": u, "ROLES": auth.ROLES,
               "stages": db.setting_list("stages"), "statuses": db.setting_list("contact_statuses"),
               "org_types": db.setting_list("org_types"), "today": date.today().isoformat(),
               "won_lost": db.closed_stages(), "csrf_token": auth.csrf_token,
               "is_admin": auth.is_admin(), "can_edit": auth.can_edit(), "can_import": auth.can_import(),
               "can_export": auth.can_export(), "can_delete": auth.can_delete}
        if u is not None:
            ctx["nav_tasks"] = db.query(
                "SELECT COUNT(*) n FROM tasks WHERE status='Open' AND assigned_to=? AND "
                "(due_date IS NULL OR due_date <= ?)", (u["id"], date.today().isoformat()), one=True)["n"]
            ctx["nav_review"] = db.query(
                "SELECT COUNT(*) n FROM contacts WHERE needs_review=1 AND (owner_id=? OR owner_id IS NULL)",
                (u["id"],), one=True)["n"]
            ctx["last_sync"] = db.query("SELECT * FROM sync_log WHERE user_id=? OR user_id IS NULL "
                                        "ORDER BY id DESC LIMIT 1", (u["id"],), one=True)
            ctx["team"] = [x for x in db.users(active_only=True) if x["role"] != "viewer"]
            h = datetime.now().hour
            ctx["greeting"] = "Good morning" if h < 12 else ("Good afternoon" if h < 17 else "Good evening")
        return ctx

    app.jinja_env.globals.update(display_name=display_name, uname=uname)

    from . import views_admin, views_auth, views_crm
    views_auth.register(app)
    views_crm.register(app)
    views_admin.register(app)

    @app.errorhandler(403)
    def forbidden(e):
        return render_template("error.html", code=403,
                               msg="You don't have permission to do that. Ask an administrator if you need access."), 403

    @app.errorhandler(404)
    def nf(e):
        return render_template("error.html", code=404, msg="That page or record doesn't exist."), 404

    @app.errorhandler(413)
    def too_big(e):
        return render_template("error.html", code=413, msg="That file is too large (limit 25 MB)."), 413

    return app


def _filters(app):
    @app.template_filter("money")
    def money_f(v):
        return f"${(v or 0):,.0f}"

    @app.template_filter("d")
    def date_f(v):
        if not v:
            return ""
        try:
            dt = datetime.fromisoformat(str(v))
        except ValueError:
            return str(v)[:10]
        return dt.strftime("%b %d, %Y")

    @app.template_filter("dt")
    def datetime_f(v):
        if not v:
            return ""
        try:
            dt = datetime.fromisoformat(str(v))
        except ValueError:
            return str(v)
        return dt.strftime("%b %d, %Y %I:%M %p").replace(" 0", " ")

    @app.template_filter("ago")
    def ago_f(v):
        if not v:
            return "never"
        try:
            dt = datetime.fromisoformat(str(v))
        except ValueError:
            return str(v)
        days = (datetime.now() - dt).days
        if days <= 0:
            secs = (datetime.now() - dt).seconds
            if secs < 3600:
                return f"{max(1, secs // 60)}m ago"
            return f"{secs // 3600}h ago"
        if days == 1:
            return "yesterday"
        if days < 30:
            return f"{days}d ago"
        if days < 365:
            return f"{days // 30}mo ago"
        return f"{days // 365}y ago"

    @app.template_filter("initials")
    def initials_f(c):
        a = (c["first_name"] or "")[:1] + (c["last_name"] or "")[:1]
        return (a or (c["email"] or "?")[:1]).upper()

    @app.template_filter("uinitials")
    def uinitials_f(u):
        if not u:
            return "?"
        parts = (u["name"] or u["email"] or "?").split()
        return "".join(p[0] for p in parts[:2]).upper()
