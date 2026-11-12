"""Setup wizard, sign-in (password + Microsoft), sign-out, My profile and the
per-user mailbox connection."""
import logging
import re

from flask import flash, g, jsonify, redirect, render_template, request, session, url_for

from . import __version__
from . import auth, db, demo, graph, sync
from .web import audit, safe_next

log = logging.getLogger("relay")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def register(app):
    # ------------------------------------------------------------ health
    @app.route("/healthz")
    def healthz():
        try:
            db.query("SELECT 1", one=True)
            return jsonify(ok=True, version=__version__)
        except Exception as e:  # pragma: no cover
            return jsonify(ok=False, error=str(e)), 500

    # ------------------------------------------------------------ first-run setup
    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        if db.user_count() > 0:
            return redirect(url_for("login"))
        upgrading = db.get_setting("v1_upgrade_pending") == "1"
        form = {}
        if request.method == "POST":
            f = request.form
            form = {"name": f.get("name", "").strip(), "email": f.get("email", "").strip().lower(),
                    "firm_name": f.get("firm_name", "").strip()}
            pw, pw2 = f.get("password", ""), f.get("password2", "")
            err = None
            if not form["name"] or not EMAIL_RE.match(form["email"]):
                err = "Enter your name and a valid work email address."
            elif pw != pw2:
                err = "The two passwords don't match."
            else:
                err = auth.password_problem(pw)
            if err:
                flash(err, "error")
                return render_template("setup.html", form=form, upgrading=upgrading)
            uid = db.execute("INSERT INTO users(email, name, role, password_hash, created_at) VALUES (?,?,?,?,?)",
                             (form["email"], form["name"], "admin", auth.hash_password(pw), db.now_iso()))
            if form["firm_name"]:
                db.set_setting("firm_name", form["firm_name"])
            if upgrading:
                _adopt_v1_data(uid)
            db.set_setting("setup_complete", "1")
            u = db.user(uid)
            db.audit(u, "create", "user", uid, "first administrator (setup wizard)", auth.client_ip())
            if db.get_setting("mode") == "demo":
                demo.seed()
            auth.login_user(u, "password")
            flash("Welcome to Relay! You're the administrator. Add your team under Admin > Team & users, "
                  "and connect Microsoft 365 under Admin > Settings when you're ready.", "ok")
            return redirect(url_for("dashboard"))
        return render_template("setup.html", form=form, upgrading=upgrading)

    def _adopt_v1_data(uid):
        """Assign every record from a single-user v1 install to the first admin."""
        for t, col in (("contacts", "owner_id"), ("contacts", "created_by"), ("organizations", "owner_id"),
                       ("opportunities", "owner_id"), ("activities", "user_id"), ("tasks", "assigned_to"),
                       ("tasks", "created_by"), ("sync_log", "user_id")):
            db.execute(f"UPDATE {t} SET {col}=? WHERE {col} IS NULL", (uid,))
        old_email = (db.get_setting("my_email") or "").strip().lower()
        aliases = [a.strip().lower() for a in (db.get_setting("my_addresses") or "").split(",") if "@" in a]
        u = db.user(uid)
        if old_email and old_email != u["email"]:
            aliases.append(old_email)
        sets = {"aliases": ",".join(sorted(set(aliases)))}
        if db.get_setting("signature"):
            sets["signature"] = db.get_setting("signature")
        # carry over sync bookmarks so the first v2 sync doesn't re-scan
        for k in ("inbox", "sent", "capture"):
            v = db.get_setting(f"last_sync_{k}")
            if v:
                sets[f"last_sync_{k}"] = v
        db.execute(f"UPDATE users SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), uid))
        for k in ("v1_upgrade_pending", "app_password", "my_email", "my_name", "my_addresses", "signature"):
            db.execute("DELETE FROM settings WHERE key=?", (k,))

    # ------------------------------------------------------------ sign in / out
    @app.route("/login", methods=["GET", "POST"])
    def login():
        if g.user is not None:
            return redirect(url_for("dashboard"))
        s = db.get_settings()
        nxt = safe_next(request.args.get("next"), url_for("dashboard"))
        ms_ok = graph.web_signin_available() and s.get("allow_ms_login") == "1"
        pw_ok = s.get("allow_password_login") == "1"
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            pw = request.form.get("password", "")
            u = db.user_by_email(email)
            if u and u["is_demo"]:
                u = None
            if u and auth.is_locked(u):
                flash("Too many failed attempts. This account is locked for a few minutes.", "error")
                return render_template("login.html", ms_ok=ms_ok, pw_ok=pw_ok, email=email), 429
            if u and u["active"] and auth.check_password(u, pw):
                if not pw_ok and u["role"] != "admin":
                    flash("Password sign-in is turned off. Use 'Sign in with Microsoft'.", "error")
                    return render_template("login.html", ms_ok=ms_ok, pw_ok=pw_ok, email=email), 403
                auth.login_user(u, "password")
                return redirect(nxt)
            if u:
                auth.register_failure(u)
            db.audit(u, "login_failed", "user", u["id"] if u else None, f"email={email}", auth.client_ip())
            flash("That email and password don't match an active account.", "error")
            return render_template("login.html", ms_ok=ms_ok, pw_ok=pw_ok, email=email), 401
        return render_template("login.html", ms_ok=ms_ok, pw_ok=pw_ok, email="", nxt=nxt)

    @app.route("/logout", methods=["GET", "POST"])
    def logout():
        auth.logout_user()
        flash("You've been signed out.", "ok")
        return redirect(url_for("login"))

    # ------------------------------------------------------------ Microsoft (web) sign-in
    @app.route("/auth/microsoft")
    def auth_ms_start():
        purpose = "connect" if request.args.get("connect") and g.user is not None else "login"
        if purpose == "login" and db.get_setting("allow_ms_login") != "1":
            flash("Microsoft sign-in is turned off.", "error")
            return redirect(url_for("login"))
        if not graph.web_signin_available():
            flash("Microsoft sign-in isn't configured yet (an administrator needs to add the client secret and "
                  "server address in Admin > Settings).", "error")
            return redirect(url_for("profile") if purpose == "connect" else url_for("login"))
        shared = purpose == "connect" and str(g.user["id"]) == (db.get_setting("capture_reader_id") or "")
        try:
            auth_uri, flow = graph.start_web_flow(shared=shared)
        except Exception as e:
            log.exception("start web flow")
            flash(f"Couldn't start Microsoft sign-in: {e}", "error")
            return redirect(url_for("login"))
        session["ms_flow"] = flow
        session["ms_purpose"] = purpose
        session["ms_next"] = safe_next(request.args.get("next"), url_for("dashboard"))
        return redirect(auth_uri)

    @app.route("/auth/microsoft/callback")
    def auth_ms_callback():
        flow = session.pop("ms_flow", None)
        purpose = session.pop("ms_purpose", "login")
        nxt = session.pop("ms_next", url_for("dashboard"))
        if not flow:
            flash("Microsoft sign-in expired. Please try again.", "error")
            return redirect(url_for("login"))
        try:
            claims, cache = graph.finish_web_flow(flow, request.args.to_dict())
        except Exception as e:
            log.warning("Microsoft sign-in failed: %s", e)
            db.audit(None, "login_failed", "user", None, f"microsoft: {e}", auth.client_ip())
            flash(f"Microsoft sign-in failed: {e}", "error")
            return redirect(url_for("profile") if purpose == "connect" and g.user else url_for("login"))
        oid = claims.get("oid")
        email = (claims.get("preferred_username") or claims.get("email") or "").lower()

        if purpose == "connect" and g.user is not None:
            addr = graph.store_web_connection(g.user["id"], claims, cache)
            audit("mailbox_connect", "user", g.user["id"], f"{addr} (web sign-in)")
            flash(f"Mailbox connected: {addr}. Relay will now sync it automatically.", "ok")
            return redirect(url_for("profile") + "#mailbox")

        u = db.query("SELECT * FROM users WHERE ms_oid=? AND is_demo=0", (oid,), one=True) if oid else None
        if not u and email:
            u = db.query("SELECT * FROM users WHERE is_demo=0 AND (email=? COLLATE NOCASE OR ms_email=? COLLATE NOCASE)",
                         (email, email), one=True)
        if not u:
            doms = [d.lower().lstrip("@") for d in db.setting_list("auto_provision_domains")]
            dom = email.split("@")[-1] if "@" in email else ""
            if db.get_setting("auto_provision") == "1" and email and (not doms or dom in doms):
                uid = db.execute("INSERT INTO users(email, name, role, ms_oid, created_at) VALUES (?,?,?,?,?)",
                                 (email, claims.get("name") or email, "user", oid, db.now_iso()))
                u = db.user(uid)
                db.audit(u, "create", "user", uid, "auto-provisioned on first Microsoft sign-in", auth.client_ip())
            else:
                db.audit(None, "login_failed", "user", None, f"microsoft: no account for {email}", auth.client_ip())
                flash(f"{email or 'That account'} doesn't have access to Relay yet. Ask an administrator to add you.",
                      "error")
                return redirect(url_for("login"))
        if not u["active"]:
            flash("Your Relay account is deactivated. Contact an administrator.", "error")
            return redirect(url_for("login"))
        if oid and not u["ms_oid"]:
            db.execute("UPDATE users SET ms_oid=? WHERE id=?", (oid, u["id"]))
        graph.store_web_connection(u["id"], claims, cache)   # signing in also connects the mailbox
        u = db.user(u["id"])
        auth.login_user(u, "microsoft")
        if u["must_change_password"]:
            db.execute("UPDATE users SET must_change_password=0 WHERE id=?", (u["id"],))
        return redirect(nxt)

    # ------------------------------------------------------------ My profile
    @app.route("/profile", methods=["GET", "POST"])
    def profile():
        u = g.user
        if request.method == "POST":
            f = request.form
            aliases = sorted({a.strip().lower() for a in re.split(r"[;,\s]+", f.get("aliases", ""))
                              if EMAIL_RE.match(a.strip())})
            clash = [a for a in aliases if (lambda o: o and o["id"] != u["id"])(db.user_by_email(a))]
            if clash:
                flash(f"{', '.join(clash)} belongs to another team member.", "error")
                return redirect(url_for("profile"))
            db.execute("UPDATE users SET name=?, title=?, signature=?, aliases=?, default_scope=?, sync_enabled=? "
                       "WHERE id=?",
                       (f.get("name", "").strip() or u["name"], f.get("title", "").strip(), f.get("signature", ""),
                        ",".join(aliases), "team" if f.get("default_scope") == "team" else "mine",
                        1 if f.get("sync_enabled") else 0, u["id"]))
            audit("update", "user", u["id"], "profile")
            flash("Profile saved.", "ok")
            return redirect(url_for("profile"))
        gc = graph.GraphClient(u)
        acct = None
        try:
            acct = gc.account() if u["ms_token_cache"] else None
        except Exception:
            acct = None
        my_syncs = db.query("SELECT * FROM sync_log WHERE user_id=? ORDER BY id DESC LIMIT 5", (u["id"],))
        return render_template("profile.html", acct=acct, configured=graph.configured(),
                               web_ok=graph.web_signin_available(), flow=graph.DEVICE_FLOWS.get(u["id"]),
                               my_syncs=my_syncs,
                               is_reader=str(u["id"]) == (db.get_setting("capture_reader_id") or ""))

    @app.route("/profile/password", methods=["POST"])
    def profile_password():
        u = g.user
        f = request.form
        if u["password_hash"] and not u["must_change_password"] and not auth.check_password(u, f.get("current", "")):
            flash("Your current password is incorrect.", "error")
            return redirect(url_for("profile") + "#password")
        new, new2 = f.get("new", ""), f.get("new2", "")
        if new != new2:
            flash("The new passwords don't match.", "error")
            return redirect(url_for("profile") + "#password")
        err = auth.password_problem(new)
        if err:
            flash(err, "error")
            return redirect(url_for("profile") + "#password")
        db.execute("UPDATE users SET password_hash=?, must_change_password=0 WHERE id=?",
                   (auth.hash_password(new), u["id"]))
        auth.bump_session_version(u["id"])          # sign out other sessions
        auth.login_user(db.user(u["id"]), "password-change")
        audit("password_change", "user", u["id"], "")
        flash("Password changed. Any other signed-in sessions were signed out.", "ok")
        return redirect(url_for("profile"))

    @app.route("/profile/signout-everywhere", methods=["POST"])
    def profile_signout_all():
        auth.bump_session_version(g.user["id"])
        auth.login_user(db.user(g.user["id"]), "signout-others")
        flash("All your other sessions have been signed out.", "ok")
        return redirect(url_for("profile"))

    # ------------------------------------------------------------ mailbox connection (device sign-in)
    @app.route("/profile/ms/connect", methods=["POST"])
    def profile_ms_connect():
        try:
            st = graph.start_device_flow(g.user)
        except Exception as e:
            return jsonify(status="error", error=str(e))
        return jsonify(st)

    @app.route("/profile/ms/status")
    def profile_ms_status():
        st = dict(graph.DEVICE_FLOWS.get(g.user["id"]) or {"status": "idle"})
        return jsonify(st)

    @app.route("/profile/ms/test", methods=["POST"])
    def profile_ms_test():
        gc = graph.GraphClient(g.user)
        try:
            me = gc.me()
            msg = f"Connected as {me.get('displayName')} <{me.get('mail') or me.get('userPrincipalName')}>."
            if db.get_setting("sync_capture") == "1":
                folder = gc.find_folder(db.get_setting("capture_folder") or "CRM")
                msg += (" Capture folder found." if folder else
                        f" Note: there's no '{db.get_setting('capture_folder')}' folder in your mailbox yet. Create it "
                        "in Outlook if you want to use BCC / drag-to-log.")
            flash(msg, "ok")
        except Exception as e:
            flash(f"Connection test failed: {e}", "error")
        return redirect(url_for("profile") + "#mailbox")

    @app.route("/profile/ms/disconnect", methods=["POST"])
    def profile_ms_disconnect():
        graph.GraphClient(g.user).disconnect()
        audit("mailbox_disconnect", "user", g.user["id"], "")
        flash("Mailbox disconnected and its stored tokens deleted. Email already logged stays in the CRM.", "ok")
        return redirect(url_for("profile") + "#mailbox")

    @app.route("/profile/sync", methods=["POST"])
    def profile_sync():
        res = sync.run_sync("manual", user_ids=[g.user["id"]], include_shared=False)
        if request.headers.get("X-Requested-With"):
            return jsonify(res)
        flash(res.get("message", ""), "ok" if res.get("status") == "ok" else "error")
        return redirect(request.referrer or url_for("profile"))
