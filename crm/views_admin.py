"""Administrator pages: Team & users, Settings, Audit log, Backups, demo/live mode."""
import os
import re
import tempfile
from datetime import date

from flask import Response, abort, flash, g, redirect, render_template, request, send_file, url_for

from . import auth, backup, crypto, db, demo, graph
from .web import audit, csv_response

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# settings an admin may change from the Settings page (by section)
BOOL_KEYS = {"sync_auto", "sync_capture", "autocreate_cc", "skip_bulk", "rule_awaiting_reply",
             "rule_reply_needed", "auto_promote", "allow_password_login", "allow_ms_login", "auto_provision",
             "user_can_export", "user_can_import", "user_can_delete_own"}
TEXT_KEYS = {"firm_name", "ms_cloud", "ms_tenant", "ms_client_id", "base_url", "capture_folder", "capture_mailbox",
             "capture_reader_id", "sync_interval_min", "initial_lookback_days", "internal_domains", "autocreate",
             "ignore_patterns", "followup_days", "reply_days", "stale_days", "stages", "stage_probabilities",
             "contact_statuses", "org_types", "auto_provision_domains", "session_timeout_min",
             "password_min_length", "backup_keep_days"}
INT_KEYS = {"sync_interval_min": (2, 1440), "initial_lookback_days": (1, 365), "followup_days": (1, 90),
            "reply_days": (1, 30), "stale_days": (7, 365), "session_timeout_min": (5, 10080),
            "password_min_length": (8, 64), "backup_keep_days": (1, 365)}
CHOICES = {"ms_cloud": set(graph.CLOUDS), "autocreate": {"both", "outbound", "off"}}
BACKUP_NAME_RE = re.compile(r"^relay-\d{8}-\d{6}\.db$")


def _active_admins(exclude=None):
    rows = db.query("SELECT id FROM users WHERE role='admin' AND active=1 AND is_demo=0")
    return [r["id"] for r in rows if r["id"] != exclude]


def _clean_aliases(text, uid=None):
    aliases = sorted({a.strip().lower() for a in re.split(r"[;,\s]+", text or "") if EMAIL_RE.match(a.strip())})
    clash = [a for a in aliases if (lambda o: o and o["id"] != uid)(db.user_by_email(a))]
    return aliases, clash


def register(app):
    # ================================================================ users
    @app.route("/admin/users")
    @auth.admin_required
    def admin_users():
        rows = db.query(
            "SELECT u.*, "
            " (SELECT COUNT(*) FROM contacts c WHERE c.owner_id=u.id) n_contacts, "
            " (SELECT COUNT(*) FROM opportunities o WHERE o.owner_id=u.id) n_opps, "
            " (SELECT COUNT(*) FROM tasks t WHERE t.assigned_to=u.id AND t.status='Open') n_tasks "
            "FROM users u ORDER BY u.active DESC, u.is_demo, u.name COLLATE NOCASE")
        return render_template("admin_users.html", rows=rows, temp=request.args.get("temp"))

    @app.route("/admin/users/new", methods=["GET", "POST"])
    @auth.admin_required
    def admin_user_new():
        form = {"role": "user", "active": 1}
        if request.method == "POST":
            f = request.form
            form = {k: f.get(k, "").strip() for k in ("name", "email", "role", "title", "aliases")}
            form["email"] = form["email"].lower()
            err = None
            if not form["name"] or not EMAIL_RE.match(form["email"]):
                err = "Enter a name and a valid email address."
            elif form["role"] not in auth.ROLES:
                err = "Choose a role."
            elif db.user_by_email(form["email"]):
                err = "A user with that email already exists."
            aliases, clash = _clean_aliases(form["aliases"])
            if not err and clash:
                err = f"{', '.join(clash)} already belongs to another user."
            if err:
                flash(err, "error")
                return render_template("admin_user_form.html", u=None, form=form)
            method = f.get("signin", "temp")
            temp = auth.temp_password() if method == "temp" else ""
            uid = db.execute(
                "INSERT INTO users(email, name, role, title, aliases, password_hash, must_change_password, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (form["email"], form["name"], form["role"], form["title"], ",".join(aliases),
                 auth.hash_password(temp) if temp else "", 1 if temp else 0, db.now_iso()))
            audit("create", "user", uid, f"{form['email']} role={form['role']} signin={method}")
            if temp:
                flash(f"User created. Give {form['name']} this one-time password (shown only once): {temp}  "
                      "They'll be asked to change it at first sign-in.", "ok")
            else:
                flash(f"User created. {form['name']} signs in with the 'Sign in with Microsoft' button.", "ok")
            return redirect(url_for("admin_users"))
        return render_template("admin_user_form.html", u=None, form=form)

    @app.route("/admin/users/<int:uid>", methods=["GET", "POST"])
    @auth.admin_required
    def admin_user_edit(uid):
        u = db.user(uid) or abort(404)
        if request.method == "POST":
            f = request.form
            name = f.get("name", "").strip() or u["name"]
            email = f.get("email", "").strip().lower() or u["email"]
            role = f.get("role", u["role"])
            active = 1 if f.get("active") else 0
            if role not in auth.ROLES:
                abort(400)
            if not EMAIL_RE.match(email):
                flash("Enter a valid email address.", "error")
                return redirect(url_for("admin_user_edit", uid=uid))
            other = db.user_by_email(email)
            if other and other["id"] != uid:
                flash("Another user already has that email.", "error")
                return redirect(url_for("admin_user_edit", uid=uid))
            if uid == g.user["id"] and not active:
                flash("You can't deactivate your own account.", "error")
                return redirect(url_for("admin_user_edit", uid=uid))
            if u["role"] == "admin" and (role != "admin" or not active) and not _active_admins(exclude=uid):
                flash("Relay needs at least one active administrator. Promote someone else first.", "error")
                return redirect(url_for("admin_user_edit", uid=uid))
            aliases, clash = _clean_aliases(f.get("aliases", ""), uid)
            if clash:
                flash(f"{', '.join(clash)} already belongs to another user.", "error")
                return redirect(url_for("admin_user_edit", uid=uid))
            changes = [k for k, old, new in (("name", u["name"], name), ("email", u["email"], email),
                                             ("role", u["role"], role), ("active", u["active"], active),
                                             ("title", u["title"], f.get("title", "").strip()),
                                             ("aliases", u["aliases"], ",".join(aliases))) if (old or "") != (new or "")]
            db.execute("UPDATE users SET name=?, email=?, role=?, active=?, title=?, aliases=?, sync_enabled=? "
                       "WHERE id=?",
                       (name, email, role, active, f.get("title", "").strip(), ",".join(aliases),
                        1 if f.get("sync_enabled") else 0, uid))
            if not active or role != u["role"]:
                auth.bump_session_version(uid)   # take effect immediately
            audit("update", "user", uid, "changed: " + (", ".join(changes) or "nothing")
                  + (f" (role {u['role']} -> {role})" if role != u["role"] else ""))
            flash("User saved.", "ok")
            return redirect(url_for("admin_users"))
        stats = db.query(
            "SELECT (SELECT COUNT(*) FROM contacts WHERE owner_id=:u) contacts, "
            "(SELECT COUNT(*) FROM organizations WHERE owner_id=:u) orgs, "
            "(SELECT COUNT(*) FROM opportunities WHERE owner_id=:u) opps, "
            "(SELECT COUNT(*) FROM tasks WHERE assigned_to=:u AND status='Open') tasks, "
            "(SELECT COUNT(*) FROM activities WHERE user_id=:u) acts", {"u": uid}, one=True)
        recent = db.query("SELECT * FROM audit_log WHERE user_id=? ORDER BY id DESC LIMIT 15", (uid,))
        others = [x for x in db.users(active_only=True) if x["id"] != uid and x["role"] != "viewer"]
        return render_template("admin_user_form.html", u=u, form=dict(u), stats=stats, recent=recent,
                               others=others)

    @app.route("/admin/users/<int:uid>/reset-password", methods=["POST"])
    @auth.admin_required
    def admin_user_reset(uid):
        u = db.user(uid) or abort(404)
        temp = auth.temp_password()
        db.execute("UPDATE users SET password_hash=?, must_change_password=1, failed_logins=0, locked_until=NULL "
                   "WHERE id=?", (auth.hash_password(temp), uid))
        auth.bump_session_version(uid)
        audit("password_reset", "user", uid, u["email"])
        flash(f"New one-time password for {u['name']} (shown only once): {temp}", "ok")
        return redirect(url_for("admin_user_edit", uid=uid))

    @app.route("/admin/users/<int:uid>/unlock", methods=["POST"])
    @auth.admin_required
    def admin_user_unlock(uid):
        u = db.user(uid) or abort(404)
        db.execute("UPDATE users SET failed_logins=0, locked_until=NULL WHERE id=?", (uid,))
        audit("unlock", "user", uid, u["email"])
        flash(f"{u['name']} unlocked.", "ok")
        return redirect(url_for("admin_user_edit", uid=uid))

    @app.route("/admin/users/<int:uid>/signout", methods=["POST"])
    @auth.admin_required
    def admin_user_signout(uid):
        u = db.user(uid) or abort(404)
        auth.bump_session_version(uid)
        audit("signout_everywhere", "user", uid, u["email"])
        if uid == g.user["id"]:
            auth.login_user(db.user(uid), "signout-others")
        flash(f"{u['name']} has been signed out of every device.", "ok")
        return redirect(url_for("admin_user_edit", uid=uid))

    @app.route("/admin/users/<int:uid>/disconnect", methods=["POST"])
    @auth.admin_required
    def admin_user_disconnect(uid):
        u = db.user(uid) or abort(404)
        graph.GraphClient(u).disconnect()
        audit("mailbox_disconnect", "user", uid, f"by admin ({u['ms_email']})")
        flash(f"{u['name']}'s mailbox disconnected and stored tokens deleted.", "ok")
        return redirect(url_for("admin_user_edit", uid=uid))

    @app.route("/admin/users/<int:uid>/delete", methods=["POST"])
    @auth.admin_required
    def admin_user_delete(uid):
        u = db.user(uid) or abort(404)
        if uid == g.user["id"]:
            flash("You can't delete your own account.", "error")
            return redirect(url_for("admin_user_edit", uid=uid))
        if u["role"] == "admin" and not _active_admins(exclude=uid):
            flash("You can't delete the last administrator.", "error")
            return redirect(url_for("admin_user_edit", uid=uid))
        to = request.form.get("reassign_to", "")
        target = db.user(int(to)) if to.isdigit() else None
        tid = target["id"] if target and target["active"] and target["id"] != uid else None
        for t, col in (("contacts", "owner_id"), ("organizations", "owner_id"), ("opportunities", "owner_id"),
                       ("tasks", "assigned_to")):
            db.execute(f"UPDATE {t} SET {col}=? WHERE {col}=?", (tid, uid))
        # history keeps the name via the audit label; activity authorship becomes "former user"
        for t, col in (("contacts", "created_by"), ("tasks", "created_by"), ("activities", "user_id"),
                       ("sync_log", "user_id")):
            db.execute(f"UPDATE {t} SET {col}=NULL WHERE {col}=?", (uid,))
        if db.get_setting("capture_reader_id") == str(uid):
            db.set_setting("capture_reader_id", "")
        db.execute("DELETE FROM users WHERE id=?", (uid,))
        audit("delete", "user", uid, f"{u['name']} <{u['email']}>; records reassigned to "
              + (target["name"] if tid else "nobody (unassigned)"))
        flash(f"{u['name']} deleted. Their records were "
              + (f"reassigned to {target['name']}." if tid else "left unassigned."), "ok")
        return redirect(url_for("admin_users"))

    # ================================================================ audit log
    def _audit_rows(limit=500):
        a = request.args
        sql, args = "SELECT * FROM audit_log WHERE 1=1", []
        if a.get("user", "").isdigit():
            sql += " AND user_id=?"
            args.append(int(a["user"]))
        if a.get("action"):
            sql += " AND action=?"
            args.append(a["action"])
        if a.get("entity"):
            sql += " AND entity=?"
            args.append(a["entity"])
        if a.get("since"):
            sql += " AND ts >= ?"
            args.append(a["since"])
        if a.get("until"):
            sql += " AND ts <= ?"
            args.append(a["until"] + " 23:59:59")
        if a.get("q"):
            sql += " AND (detail LIKE ? OR user_label LIKE ?)"
            args += [f"%{a['q']}%"] * 2
        sql += " ORDER BY id DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return db.query(sql, args)

    @app.route("/admin/audit")
    @auth.admin_required
    def audit_view():
        rows = _audit_rows()
        actions = [r["action"] for r in db.query("SELECT DISTINCT action FROM audit_log ORDER BY action")]
        entities = [r["entity"] for r in db.query("SELECT DISTINCT entity FROM audit_log WHERE entity<>'' "
                                                   "ORDER BY entity")]
        return render_template("audit.html", rows=rows, actions=actions, entities=entities,
                               all_users=db.users(active_only=False), f=request.args)

    @app.route("/admin/audit.csv")
    @auth.admin_required
    def audit_csv():
        rows = _audit_rows(limit=None)
        audit("export", "audit_log", None, f"{len(rows)} rows")
        return csv_response(f"relay-audit-{date.today()}.csv",
                            ["time", "user", "action", "entity", "entity_id", "detail", "ip"],
                            [[r["ts"], r["user_label"], r["action"], r["entity"], r["entity_id"], r["detail"],
                              r["ip"]] for r in rows])

    # ================================================================ settings
    @app.route("/admin/settings", methods=["GET", "POST"])
    @auth.admin_required
    def settings():
        if request.method == "POST":
            f = request.form
            section = re.sub(r"[^a-z0-9_-]", "", f.get("section", ""))
            changed = []
            for k in TEXT_KEYS:
                if k not in f:
                    continue
                v = f.get(k, "").strip()
                if k in INT_KEYS:
                    lo, hi = INT_KEYS[k]
                    try:
                        v = str(max(lo, min(hi, int(v))))
                    except ValueError:
                        flash(f"'{k.replace('_', ' ')}' must be a number.", "error")
                        continue
                if k in CHOICES and v not in CHOICES[k]:
                    continue
                if k == "base_url":
                    v = v.rstrip("/")
                    if v and not re.match(r"^https?://", v):
                        flash("Server address must start with https:// (or http:// for testing).", "error")
                        continue
                if k == "capture_reader_id" and v and not (v.isdigit() and db.user(int(v))):
                    continue
                if (db.get_setting(k) or "") != v:
                    db.set_setting(k, v)
                    changed.append(k)
            if "ms_client_secret" in f:
                sec = f.get("ms_client_secret", "").strip()
                if f.get("clear_secret"):
                    db.set_setting("ms_client_secret", "")
                    changed.append("ms_client_secret (cleared)")
                elif sec:
                    db.set_setting("ms_client_secret", crypto.encrypt(sec))
                    changed.append("ms_client_secret")
            for k in f.getlist("bool_keys"):
                if k in BOOL_KEYS:
                    v = "1" if f.get(k) else "0"
                    if db.get_setting(k) != v:
                        db.set_setting(k, v)
                        changed.append(f"{k}={v}")
            if db.get_setting("allow_password_login") == "0" and db.get_setting("allow_ms_login") == "0":
                db.set_setting("allow_ms_login", "1")
                flash("At least one sign-in method must stay on - Microsoft sign-in was re-enabled.", "error")
            if changed:
                audit("update", "settings", None, ", ".join(changed))
                flash("Settings saved.", "ok")
            else:
                flash("No changes.", "ok")
            return redirect(url_for("settings") + (f"#{section}" if section else ""))
        reader = db.user(int(db.get_setting("capture_reader_id"))) if (db.get_setting("capture_reader_id") or ""
                                                                         ).isdigit() else None
        connected = db.query("SELECT * FROM users WHERE ms_token_cache<>'' AND active=1 ORDER BY name")
        return render_template("settings.html", clouds=graph.CLOUDS, configured=graph.configured(),
                               web_ok=graph.web_signin_available(),
                               redirect_uri=graph.redirect_uri() if db.get_setting("base_url") else
                               request.host_url.rstrip("/") + graph.CALLBACK_PATH,
                               secret_set=bool(db.get_setting("ms_client_secret")),
                               reader=reader, connected=connected, members=db.users(active_only=True))

    @app.route("/admin/settings/mode", methods=["POST"])
    @auth.admin_required
    def settings_mode():
        mode = request.form.get("mode")
        if mode == "live":
            if request.form.get("wipe_demo"):
                demo.wipe_crm_data()
                demo.remove_demo_users()
            for u in db.users(active_only=False):
                db.execute("UPDATE users SET last_sync_inbox='', last_sync_sent='', last_sync_capture='' WHERE id=?",
                           (u["id"],))
            db.set_setting("last_sync_shared", "")
            db.set_setting("mode", "live")
            audit("mode", "settings", None, "live" + (" (demo data wiped)" if request.form.get("wipe_demo") else ""))
            flash("Live mode is on. Next: add Microsoft 365 details below, then each person connects their mailbox "
                  "from My profile.", "ok")
        elif mode == "demo":
            db.set_setting("mode", "demo")
            demo.seed()
            audit("mode", "settings", None, "demo")
            flash("Demo mode is on.", "ok")
        return redirect(url_for("settings") + "#m365")

    @app.route("/admin/demo/reset", methods=["POST"])
    @auth.admin_required
    def demo_reset():
        if db.get_setting("mode") != "demo":
            flash("Demo reset is only available in demo mode.", "error")
            return redirect(url_for("settings"))
        demo.reset_demo()
        audit("demo_reset", "settings", None, "")
        flash("Demo data reset.", "ok")
        return redirect(url_for("dashboard"))

    # ================================================================ backups
    @app.route("/admin/backups")
    @auth.admin_required
    def backups():
        return render_template("backups.html", files=backup.list_backups(), folder=backup.BACKUP_DIR,
                               last=db.get_setting("last_auto_backup"))

    @app.route("/admin/backups/now", methods=["POST"])
    @auth.admin_required
    def backup_now():
        p = backup.daily_backup(force=True)
        audit("backup", "database", None, os.path.basename(p))
        flash(f"Backup saved: {os.path.basename(p)}", "ok")
        return redirect(url_for("backups"))

    @app.route("/admin/backup")
    @auth.admin_required
    def backup_download():
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        tmp.close()
        try:
            backup.snapshot(tmp.name)
            with open(tmp.name, "rb") as fh:
                data = fh.read()
        finally:
            os.remove(tmp.name)
        audit("export", "database", None, "full backup downloaded")
        return Response(data, mimetype="application/octet-stream",
                        headers={"Content-Disposition": f"attachment; filename=relay-backup-{date.today()}.db"})

    @app.route("/admin/backups/<name>")
    @auth.admin_required
    def backup_file(name):
        if not BACKUP_NAME_RE.match(name):
            abort(404)
        path = os.path.join(backup.BACKUP_DIR, name)
        if not os.path.isfile(path):
            abort(404)
        audit("export", "database", None, f"downloaded {name}")
        return send_file(path, as_attachment=True, download_name=name)
