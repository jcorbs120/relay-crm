"""Relay CRM web UI (Flask). Runs on 127.0.0.1 only; shown inside the desktop window."""
import csv
import io
import json
import logging
import secrets
from datetime import date, datetime, timedelta, timezone
from functools import wraps

from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template, request,
                   send_file, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from . import __version__, classify, demo, importer, paths, reports
from .db import DB, now_iso
from .graph import CLOUD_LABELS, M365, GraphError, NotConnected
from .sync import OPEN_STAGES, Engine, Scheduler, iso, run_sync

log = logging.getLogger("relay.app")

STAGES = reports.STAGES
LIFECYCLES = ["Lead", "Prospect", "Customer", "Partner", "Inactive"]
ACT_KINDS = {"email_out": "Email sent", "email_in": "Email received", "call": "Call", "meeting": "Meeting", "note": "Note"}
STAGE_PROB = {"Lead": 10, "Qualified": 25, "Proposal": 50, "Submitted": 70, "Awarded": 100, "Lost": 0}


def create_app(db=None, start_scheduler=True, allowed_ports=None):
    app = Flask(__name__, template_folder=str(paths.resource_dir() / "templates"),
                static_folder=str(paths.resource_dir() / "static"))
    db = db or DB()
    m365 = M365(db)
    if not db.get("secret_key"):
        db.set("secret_key", secrets.token_hex(32))
    app.secret_key = db.get("secret_key")
    app.config.update(MAX_CONTENT_LENGTH=25 * 1024 * 1024, SESSION_COOKIE_SAMESITE="Strict")
    app.extensions["relay_db"] = db
    app.extensions["relay_m365"] = m365
    if db.get("mode") == "demo" and not db.val("SELECT count(*) FROM contacts"):
        demo.seed(db)
    if start_scheduler:
        Scheduler(db, m365).start()

    def mailbox():
        return demo.DemoMailbox(db) if db.get("mode") == "demo" else None

    # ------------------------------------------------------------ security
    @app.before_request
    def guard():
        host = (request.host or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            abort(400)  # blocks DNS-rebinding; the app is local-only
        if request.method == "POST":
            tok = request.form.get("_csrf") or request.headers.get("X-CSRF")
            if not tok or tok != session.get("csrf"):
                abort(400, "Your session expired - please reload the page and try again.")
        if db.get("password_hash") and not session.get("ok") and request.endpoint not in ("login", "static", "health"):
            return redirect(url_for("login", next=request.path))

    @app.context_processor
    def inject():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        today = date.today().isoformat()
        return {
            "csrf": session["csrf"], "today": today, "S": db.settings(), "version": __version__,
            "STAGES": STAGES, "LIFECYCLES": LIFECYCLES, "ORG_TYPES": reports.ORG_TYPES, "ACT_KINDS": ACT_KINDS,
            "money": reports.money,
            "nav_counts": {
                "review": db.val("SELECT count(*) FROM contacts WHERE needs_review=1", default=0),
                "due": db.val("SELECT count(*) FROM tasks WHERE done=0 AND due_date<=?", (today,), default=0),
            },
        }

    @app.template_filter("dt")
    def fmt_dt(s, with_time=True):
        if not s:
            return ""
        try:
            d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        except ValueError:
            return s
        if d.tzinfo:
            d = d.astimezone()  # local time
        return d.strftime("%b %d, %Y %I:%M %p" if with_time and len(str(s)) > 10 else "%b %d, %Y")

    @app.template_filter("ago")
    def ago(s):
        if not s:
            return "never"
        try:
            d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        except ValueError:
            return s
        if not d.tzinfo:
            d = d.replace(tzinfo=timezone.utc)
        days = (datetime.now(timezone.utc) - d).days
        if days <= 0:
            return "today"
        if days == 1:
            return "yesterday"
        if days < 60:
            return f"{days} days ago"
        return f"{days // 30} months ago"

    def contact_name(c):
        return (f"{c['first_name'] or ''} {c['last_name'] or ''}".strip() or c["email"] or "(no name)")
    app.jinja_env.globals["cname"] = contact_name

    # ------------------------------------------------------------ auth / health
    @app.route("/health")
    def health():
        return jsonify(app="relay-crm", version=__version__)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            if check_password_hash(db.get("password_hash"), request.form.get("password", "")):
                session["ok"] = True
                nxt = request.args.get("next") or "/"
                return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
            flash("Wrong password.", "error")
        return render_template("login.html")

    @app.route("/logout", methods=["POST"])
    def logout():
        session.pop("ok", None)
        return redirect(url_for("login"))

    # ------------------------------------------------------------ dashboard
    @app.route("/")
    def dashboard():
        today = date.today().isoformat()
        tasks = db.q("""SELECT t.*, c.first_name, c.last_name, c.email FROM tasks t LEFT JOIN contacts c ON c.id=t.contact_id
                        WHERE t.done=0 AND (t.due_date IS NULL OR t.due_date <= ?) ORDER BY t.due_date, t.priority='High' DESC
                        LIMIT 12""", ((date.today() + timedelta(days=2)).isoformat(),))
        recent = db.q("""SELECT a.*, c.first_name, c.last_name, c.email, c.id cid FROM activities a
                         JOIN contacts c ON c.id=a.contact_id ORDER BY a.occurred_at DESC LIMIT 12""")
        review = db.q("""SELECT c.*, g.name org FROM contacts c LEFT JOIN orgs g ON g.id=c.org_id
                         WHERE c.needs_review=1 ORDER BY c.created_at DESC LIMIT 8""")
        pipe = {r["stage"]: r for r in db.q("SELECT stage, count(*) n, sum(value) v FROM opportunities GROUP BY stage")}
        kpi = {
            "open_value": db.val(f"SELECT sum(value) FROM opportunities WHERE stage IN ({','.join('?'*4)})", OPEN_STAGES, 0),
            "weighted": db.val(f"SELECT sum(value*probability/100.0) FROM opportunities WHERE stage IN ({','.join('?'*4)})", OPEN_STAGES, 0),
            "contacts": db.val("SELECT count(*) FROM contacts", default=0),
            "emails_week": db.val("SELECT count(*) FROM activities WHERE kind LIKE 'email_%' AND occurred_at >= ?",
                                  ((datetime.now(timezone.utc) - timedelta(days=7)).isoformat(),), 0),
            "overdue": db.val("SELECT count(*) FROM tasks WHERE done=0 AND due_date < ?", (today,), 0),
        }
        closing = db.q(f"""SELECT o.*, g.name org FROM opportunities o LEFT JOIN orgs g ON g.id=o.org_id
                           WHERE o.stage IN ({','.join('?'*4)}) ORDER BY o.close_date LIMIT 6""", OPEN_STAGES)
        last = db.one("SELECT * FROM sync_log ORDER BY id DESC LIMIT 1")
        return render_template("dashboard.html", tasks=tasks, recent=recent, review=review, pipe=pipe, kpi=kpi,
                               closing=closing, last_sync=last)

    # ------------------------------------------------------------ sync
    @app.route("/sync", methods=["POST"])
    def sync_now():
        mb = mailbox()
        if not mb:
            if not m365.configured():
                flash("Connect Microsoft 365 in Settings first (or switch back to demo mode).", "error")
                return redirect(url_for("settings"))
        res = run_sync(db, m365, mailbox=mb)
        if res.get("error") and not res.get("fetched"):
            flash(f"Sync problem: {res['error']}", "error")
        else:
            flash(f"Sync complete: {res['fetched']} emails checked, {res['logged']} logged, "
                  f"{res['contacts_created']} new contacts, {res['tasks_created']} follow-ups opened, "
                  f"{res['tasks_closed']} closed." + (f" Note: {res['error']}" if res.get("error") else ""), "ok")
        return redirect(request.form.get("next") or request.referrer or "/")

    @app.route("/sync/log")
    def sync_log():
        return render_template("sync_log.html", rows=db.q("SELECT * FROM sync_log ORDER BY id DESC LIMIT 200"))

    # ------------------------------------------------------------ contacts
    @app.route("/contacts")
    def contacts():
        q = request.args.get("q", "").strip()
        life = request.args.get("lifecycle", "")
        otype = request.args.get("org_type", "")
        review = request.args.get("review", "")
        tag = request.args.get("tag", "")
        sort = request.args.get("sort", "name")
        where, args = ["1=1"], []
        if q:
            like = f"%{q}%"
            where.append("(c.first_name LIKE ? OR c.last_name LIKE ? OR c.email LIKE ? OR g.name LIKE ? OR c.title LIKE ?"
                         " OR (c.first_name || ' ' || c.last_name) LIKE ?)")
            args += [like] * 6
        if life:
            where.append("c.lifecycle=?"); args.append(life)
        if otype:
            where.append("g.org_type=?"); args.append(otype)
        if review:
            where.append("c.needs_review=1")
        if tag:
            where.append("(',' || c.tags || ',') LIKE ?"); args.append(f"%,{tag},%")
        order = {"name": "c.last_name COLLATE NOCASE, c.first_name COLLATE NOCASE",
                 "recent": "c.last_contacted DESC NULLS LAST", "created": "c.created_at DESC",
                 "org": "g.name COLLATE NOCASE"}.get(sort, "c.last_name")
        rows = db.q(f"""SELECT c.*, g.name org, g.org_type FROM contacts c LEFT JOIN orgs g ON g.id=c.org_id
                        WHERE {' AND '.join(where)} ORDER BY {order} LIMIT 2000""", args)
        return render_template("contacts.html", rows=rows, f=request.args)

    @app.route("/contacts/export.csv")
    def contacts_export():
        rows = db.q("""SELECT c.first_name, c.last_name, c.email, c.phone, c.mobile, c.title, g.name org, g.org_type,
                       c.lifecycle, c.tags, c.last_contacted, c.created_at FROM contacts c LEFT JOIN orgs g ON g.id=c.org_id
                       ORDER BY c.last_name""")
        return csv_response("contacts.csv", ["First name", "Last name", "Email", "Phone", "Mobile", "Title",
                                             "Organization", "Org type", "Status", "Tags", "Last contacted", "Created"],
                            [list(r) for r in rows])

    def contact_form_values():
        f = request.form
        org_id = f.get("org_id") or None
        new_org = f.get("new_org", "").strip()
        if new_org:
            dom = classify.base_domain(classify.split_email(f.get("email", ""))[1]) if "@" in f.get("email", "") else None
            if dom in classify.FREE_MAIL:
                dom = None
            existing = db.one("SELECT id FROM orgs WHERE domain=?", (dom,)) if dom else None
            org_id = existing["id"] if existing else db.ex(
                "INSERT INTO orgs(name, domain, org_type, created_at) VALUES(?,?,?,?)",
                (new_org, dom, f.get("new_org_type") or (classify.classify_domain(dom) if dom else "Other"), now_iso()))
        return {
            "first_name": f.get("first_name", "").strip(), "last_name": f.get("last_name", "").strip(),
            "email": f.get("email", "").strip().lower() or None, "phone": f.get("phone", "").strip(),
            "mobile": f.get("mobile", "").strip(), "title": f.get("title", "").strip(), "org_id": org_id,
            "lifecycle": f.get("lifecycle") or "Lead", "tags": ",".join(t.strip() for t in f.get("tags", "").split(",") if t.strip()),
            "notes": f.get("notes", ""),
        }

    @app.route("/contacts/new", methods=["GET", "POST"])
    def contact_new():
        if request.method == "POST":
            v = contact_form_values()
            if v["email"] and db.one("SELECT 1 FROM contacts WHERE email=?", (v["email"],)):
                flash("A contact with that email already exists.", "error")
                return render_template("contact_form.html", c=v, orgs=all_orgs())
            if not v["org_id"] and v["email"]:
                v["org_id"] = Engine(db).get_or_create_org(classify.split_email(v["email"])[1])
            cid = db.ex(f"INSERT INTO contacts({','.join(v)}, source, created_at) VALUES({','.join('?'*len(v))},?,?)",
                        (*v.values(), "manual", now_iso()))
            flash("Contact added.", "ok")
            return redirect(url_for("contact", cid=cid))
        return render_template("contact_form.html", c={"org_id": request.args.get("org_id", type=int)}, orgs=all_orgs())

    @app.route("/contacts/<int:cid>")
    def contact(cid):
        c = db.one("SELECT c.*, g.name org, g.org_type, g.id gid FROM contacts c LEFT JOIN orgs g ON g.id=c.org_id WHERE c.id=?", (cid,))
        if not c:
            abort(404)
        kind = request.args.get("kind", "")
        acts = db.q("SELECT a.*, o.name opp FROM activities a LEFT JOIN opportunities o ON o.id=a.opp_id WHERE a.contact_id=?"
                    + (" AND a.kind LIKE ?" if kind else "") + " ORDER BY a.occurred_at DESC",
                    (cid, kind + "%") if kind else (cid,))
        tasks = db.q("SELECT * FROM tasks WHERE contact_id=? ORDER BY done, due_date", (cid,))
        opps = db.q("SELECT * FROM opportunities WHERE contact_id=? OR (org_id=? AND org_id IS NOT NULL) ORDER BY "
                    "stage IN ('Awarded','Lost'), updated_at DESC", (cid, c["org_id"]))
        stats = db.one("""SELECT sum(kind='email_out') eo, sum(kind='email_in') ei, sum(kind IN ('call','meeting')) cm
                          FROM activities WHERE contact_id=?""", (cid,))
        colleagues = db.q("SELECT id, first_name, last_name, email, title FROM contacts WHERE org_id=? AND id!=? LIMIT 12",
                          (c["org_id"], cid)) if c["org_id"] else []
        return render_template("contact.html", c=c, acts=acts, tasks=tasks, opps=opps, stats=stats,
                               colleagues=colleagues, kind=kind)

    @app.route("/contacts/<int:cid>/edit", methods=["GET", "POST"])
    def contact_edit(cid):
        c = db.one("SELECT * FROM contacts WHERE id=?", (cid,))
        if not c:
            abort(404)
        if request.method == "POST":
            v = contact_form_values()
            if v["email"] and db.one("SELECT 1 FROM contacts WHERE email=? AND id!=?", (v["email"], cid)):
                flash("Another contact already uses that email.", "error")
                return render_template("contact_form.html", c={**dict(c), **v}, orgs=all_orgs())
            db.ex(f"UPDATE contacts SET {', '.join(k + '=?' for k in v)}, needs_review=0 WHERE id=?", (*v.values(), cid))
            flash("Saved.", "ok")
            return redirect(url_for("contact", cid=cid))
        return render_template("contact_form.html", c=c, orgs=all_orgs())

    @app.route("/contacts/<int:cid>/approve", methods=["POST"])
    def contact_approve(cid):
        db.ex("UPDATE contacts SET needs_review=0 WHERE id=?", (cid,))
        return back("Contact kept.")

    @app.route("/contacts/review/approve-all", methods=["POST"])
    def contact_approve_all():
        db.ex("UPDATE contacts SET needs_review=0 WHERE needs_review=1")
        return back("All new contacts kept.")

    @app.route("/contacts/<int:cid>/delete", methods=["POST"])
    def contact_delete(cid):
        db.ex("DELETE FROM contacts WHERE id=?", (cid,))
        flash("Contact deleted.", "ok")
        return redirect(request.form.get("next") or url_for("contacts"))

    @app.route("/contacts/<int:cid>/activity", methods=["POST"])
    def activity_add(cid):
        f = request.form
        kind = f.get("kind", "note")
        if kind not in ACT_KINDS:
            abort(400)
        when = f.get("occurred_at") or datetime.now().strftime("%Y-%m-%dT%H:%M")
        try:
            when_iso = iso(datetime.fromisoformat(when).astimezone())
        except ValueError:
            when_iso = now_iso()
        db.ex("INSERT INTO activities(contact_id, opp_id, kind, subject, body, occurred_at, duration_min, created_at)"
              " VALUES(?,?,?,?,?,?,?,?)",
              (cid, f.get("opp_id") or None, kind, f.get("subject", "").strip() or ACT_KINDS[kind], f.get("body", ""),
               when_iso, f.get("duration_min") or None, now_iso()))
        if kind in ("call", "meeting", "email_out"):
            db.ex("UPDATE contacts SET last_contacted=? WHERE id=? AND (last_contacted IS NULL OR last_contacted<?)",
                  (when_iso, cid, when_iso))
        if f.get("followup_days"):
            d = (date.today() + timedelta(days=int(f["followup_days"]))).isoformat()
            db.ex("INSERT INTO tasks(contact_id, opp_id, title, due_date, created_at) VALUES(?,?,?,?,?)",
                  (cid, f.get("opp_id") or None, f"Follow up: {f.get('subject') or ACT_KINDS[kind]}", d, now_iso()))
        return back(f"{ACT_KINDS[kind]} logged.")

    @app.route("/activities/<int:aid>/delete", methods=["POST"])
    def activity_delete(aid):
        db.ex("DELETE FROM activities WHERE id=?", (aid,))
        return back("Entry removed.")

    # ------------------------------------------------------------ organizations
    def all_orgs():
        return db.q("SELECT id, name, org_type FROM orgs ORDER BY name COLLATE NOCASE")

    @app.route("/orgs")
    def orgs():
        q = request.args.get("q", "").strip()
        t = request.args.get("org_type", "")
        where, args = ["1=1"], []
        if q:
            where.append("(g.name LIKE ? OR g.domain LIKE ?)"); args += [f"%{q}%"] * 2
        if t:
            where.append("g.org_type=?"); args.append(t)
        rows = db.q(f"""SELECT g.*, (SELECT count(*) FROM contacts c WHERE c.org_id=g.id) nc,
                          (SELECT coalesce(sum(value),0) FROM opportunities o WHERE o.org_id=g.id
                             AND o.stage IN ('Lead','Qualified','Proposal','Submitted')) ov,
                          (SELECT max(last_contacted) FROM contacts c WHERE c.org_id=g.id) lt
                        FROM orgs g WHERE {' AND '.join(where)} ORDER BY g.name COLLATE NOCASE""", args)
        return render_template("orgs.html", rows=rows, f=request.args)

    @app.route("/orgs/new", methods=["GET", "POST"])
    @app.route("/orgs/<int:oid>/edit", methods=["GET", "POST"])
    def org_form(oid=None):
        o = db.one("SELECT * FROM orgs WHERE id=?", (oid,)) if oid else {}
        if oid and not o:
            abort(404)
        if request.method == "POST":
            f = request.form
            dom = f.get("domain", "").strip().lower().lstrip("@") or None
            v = (f.get("name", "").strip() or dom or "Unnamed", dom, f.get("org_type") or "Other", f.get("phone", ""),
                 f.get("website", ""), f.get("address", ""), f.get("notes", ""))
            try:
                if oid:
                    db.ex("UPDATE orgs SET name=?, domain=?, org_type=?, phone=?, website=?, address=?, notes=? WHERE id=?", (*v, oid))
                else:
                    oid = db.ex("INSERT INTO orgs(name, domain, org_type, phone, website, address, notes, created_at)"
                                " VALUES(?,?,?,?,?,?,?,?)", (*v, now_iso()))
            except Exception:
                flash("Another organization already uses that email domain.", "error")
                return render_template("org_form.html", o={**dict(o or {}), **request.form})
            flash("Saved.", "ok")
            return redirect(url_for("org", oid=oid))
        return render_template("org_form.html", o=o)

    @app.route("/orgs/<int:oid>")
    def org(oid):
        o = db.one("SELECT * FROM orgs WHERE id=?", (oid,))
        if not o:
            abort(404)
        people = db.q("SELECT * FROM contacts WHERE org_id=? ORDER BY last_name", (oid,))
        opps = db.q("SELECT o.*, c.first_name, c.last_name FROM opportunities o LEFT JOIN contacts c ON c.id=o.contact_id"
                    " WHERE o.org_id=? ORDER BY o.stage IN ('Awarded','Lost'), o.close_date", (oid,))
        acts = db.q("""SELECT a.*, c.first_name, c.last_name, c.email, c.id cid FROM activities a JOIN contacts c ON c.id=a.contact_id
                       WHERE c.org_id=? ORDER BY a.occurred_at DESC LIMIT 40""", (oid,))
        return render_template("org.html", o=o, people=people, opps=opps, acts=acts)

    @app.route("/orgs/<int:oid>/delete", methods=["POST"])
    def org_delete(oid):
        db.ex("DELETE FROM orgs WHERE id=?", (oid,))
        flash("Organization deleted (its contacts were kept).", "ok")
        return redirect(url_for("orgs"))

    # ------------------------------------------------------------ pipeline
    @app.route("/pipeline")
    def pipeline():
        t = request.args.get("org_type", "")
        rows = db.q("""SELECT o.*, g.name org, g.org_type, c.first_name, c.last_name FROM opportunities o
                       LEFT JOIN orgs g ON g.id=o.org_id LEFT JOIN contacts c ON c.id=o.contact_id"""
                    + (" WHERE g.org_type=?" if t else "") + " ORDER BY o.close_date", (t,) if t else ())
        cols = {s: [r for r in rows if r["stage"] == s] for s in STAGES}
        return render_template("pipeline.html", cols=cols, f=request.args)

    @app.route("/opps/<int:oid>/stage", methods=["POST"])
    def opp_stage(oid):
        stage = request.form.get("stage") or (request.get_json(silent=True) or {}).get("stage")
        if stage not in STAGES:
            abort(400)
        closed = now_iso() if stage in ("Awarded", "Lost") else None
        db.ex("UPDATE opportunities SET stage=?, probability=?, updated_at=?, closed_at=? WHERE id=?",
              (stage, STAGE_PROB[stage], now_iso(), closed, oid))
        if request.is_json or request.headers.get("X-CSRF"):
            return jsonify(ok=True)
        return back(f"Moved to {stage}.")

    @app.route("/opps/new", methods=["GET", "POST"])
    @app.route("/opps/<int:oid>", methods=["GET", "POST"])
    def opp_form(oid=None):
        o = db.one("SELECT * FROM opportunities WHERE id=?", (oid,)) if oid else None
        if oid and not o:
            abort(404)
        if request.method == "POST":
            f = request.form
            stage = f.get("stage") if f.get("stage") in STAGES else "Lead"
            contact_id = f.get("contact_id") or None
            org_id = f.get("org_id") or None
            if contact_id and not org_id:
                org_id = db.val("SELECT org_id FROM contacts WHERE id=?", (contact_id,))
            try:
                value = float((f.get("value") or "0").replace(",", "").replace("$", ""))
            except ValueError:
                value = 0
            prob = f.get("probability", type=int)
            if prob is None:
                prob = STAGE_PROB[stage]
            v = (f.get("name", "").strip() or "Untitled opportunity", org_id, contact_id, stage, value, prob,
                 f.get("close_date") or None, f.get("solicitation", ""), f.get("vehicle", ""), f.get("notes", ""))
            if oid:
                was_closed = o["stage"] in ("Awarded", "Lost")
                closed = o["closed_at"] if was_closed and stage == o["stage"] else (now_iso() if stage in ("Awarded", "Lost") else None)
                db.ex("UPDATE opportunities SET name=?, org_id=?, contact_id=?, stage=?, value=?, probability=?, close_date=?,"
                      " solicitation=?, vehicle=?, notes=?, updated_at=?, closed_at=? WHERE id=?", (*v, now_iso(), closed, oid))
            else:
                oid = db.ex("INSERT INTO opportunities(name, org_id, contact_id, stage, value, probability, close_date,"
                            " solicitation, vehicle, notes, created_at, updated_at, closed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (*v, now_iso(), now_iso(), now_iso() if stage in ("Awarded", "Lost") else None))
            flash("Opportunity saved.", "ok")
            return redirect(f.get("next") or url_for("opp_form", oid=oid))
        acts = db.q("""SELECT a.*, c.first_name, c.last_name, c.email, c.id cid FROM activities a JOIN contacts c ON c.id=a.contact_id
                       WHERE a.opp_id=? ORDER BY a.occurred_at DESC""", (oid,)) if oid else []
        pre = {"contact_id": request.args.get("contact_id", type=int), "org_id": request.args.get("org_id", type=int)}
        if pre["contact_id"] and not pre["org_id"]:
            pre["org_id"] = db.val("SELECT org_id FROM contacts WHERE id=?", (pre["contact_id"],))
        return render_template("opp_form.html", o=o or pre, acts=acts, orgs=all_orgs(),
                               people=db.q("SELECT id, first_name, last_name, email FROM contacts ORDER BY last_name, first_name"))

    @app.route("/opps/<int:oid>/delete", methods=["POST"])
    def opp_delete(oid):
        db.ex("DELETE FROM opportunities WHERE id=?", (oid,))
        flash("Opportunity deleted.", "ok")
        return redirect(url_for("pipeline"))

    # ------------------------------------------------------------ tasks
    @app.route("/tasks")
    def tasks():
        view = request.args.get("view", "open")
        today = date.today().isoformat()
        base = """SELECT t.*, c.first_name, c.last_name, c.email, o.name opp FROM tasks t
                  LEFT JOIN contacts c ON c.id=t.contact_id LEFT JOIN opportunities o ON o.id=t.opp_id"""
        q = {"today": (" WHERE t.done=0 AND t.due_date=?", (today,)),
             "overdue": (" WHERE t.done=0 AND t.due_date<?", (today,)),
             "upcoming": (" WHERE t.done=0 AND (t.due_date>? OR t.due_date IS NULL)", (today,)),
             "auto": (" WHERE t.done=0 AND t.kind!='manual'", ()),
             "done": (" WHERE t.done=1", ()),
             "open": (" WHERE t.done=0", ())}.get(view, (" WHERE t.done=0", ()))
        order = " ORDER BY t.completed_at DESC LIMIT 300" if view == "done" else " ORDER BY t.due_date IS NULL, t.due_date, t.priority='High' DESC"
        rows = db.q(base + q[0] + order, q[1])
        return render_template("tasks.html", rows=rows, view=view,
                               people=db.q("SELECT id, first_name, last_name, email FROM contacts ORDER BY last_name"))

    @app.route("/tasks/new", methods=["POST"])
    def task_new():
        f = request.form
        if not f.get("title", "").strip():
            return back("Give the task a title.", "error")
        db.ex("INSERT INTO tasks(contact_id, opp_id, title, due_date, priority, created_at) VALUES(?,?,?,?,?,?)",
              (f.get("contact_id") or None, f.get("opp_id") or None, f["title"].strip(), f.get("due_date") or None,
               f.get("priority") or "Normal", now_iso()))
        return back("Task added.")

    @app.route("/tasks/<int:tid>/done", methods=["POST"])
    def task_done(tid):
        done = 0 if request.form.get("undo") else 1
        db.ex("UPDATE tasks SET done=?, completed_at=? WHERE id=?", (done, now_iso() if done else None, tid))
        return back("Task completed." if done else "Task reopened.")

    @app.route("/tasks/<int:tid>/snooze", methods=["POST"])
    def task_snooze(tid):
        days = request.form.get("days", type=int) or 1
        db.ex("UPDATE tasks SET due_date=? WHERE id=?", ((date.today() + timedelta(days=days)).isoformat(), tid))
        return back(f"Snoozed {days} day{'s' if days != 1 else ''}.")

    @app.route("/tasks/<int:tid>/delete", methods=["POST"])
    def task_delete(tid):
        db.ex("DELETE FROM tasks WHERE id=?", (tid,))
        return back("Task deleted.")

    # ------------------------------------------------------------ compose / send
    def fill(text, c):
        org = db.val("SELECT name FROM orgs WHERE id=?", (c["org_id"],), "") if c and c["org_id"] else ""
        vals = {"first_name": (c["first_name"] if c else "") or "there", "last_name": (c["last_name"] if c else "") or "",
                "org": org or "your organization", "my_name": db.get("my_name") or ""}
        for k, v in vals.items():
            text = text.replace("{" + k + "}", v)
        return text

    @app.route("/compose", methods=["GET", "POST"])
    def compose():
        cid = request.values.get("contact_id", type=int)
        c = db.one("SELECT * FROM contacts WHERE id=?", (cid,)) if cid else None
        if request.method == "POST":
            f = request.form
            to = [a.strip() for a in f.get("to", "").replace(";", ",").split(",") if a.strip()]
            cc = [a.strip() for a in f.get("cc", "").replace(";", ",").split(",") if a.strip()]
            subject, body = f.get("subject", "").strip(), f.get("body", "")
            if not to or not subject:
                flash("Add at least one recipient and a subject.", "error")
                return render_template("compose.html", c=c, form=f, templates=db.q("SELECT * FROM email_templates ORDER BY name"),
                                       opps=contact_opps(c))
            sig = db.get("signature")
            if sig and f.get("add_sig"):
                body = body.rstrip() + "\n\n" + sig
            is_demo = db.get("mode") == "demo"
            try:
                res = demo.DemoMailbox(db).send(to, subject, body, cc) if is_demo else m365.send(to, subject, body, cc)
            except (GraphError, NotConnected) as e:
                flash(f"Not sent: {e}", "error")
                return render_template("compose.html", c=c, form=f, templates=db.q("SELECT * FROM email_templates ORDER BY name"),
                                       opps=contact_opps(c))
            mid = res.get("internet_message_id") or f"local-{secrets.token_hex(8)}"
            eng = Engine(db)
            msg = {"id": mid, "subject": subject, "body": body, "from": (db.get("my_email"), ""),
                   "to": [(a.lower(), "") for a in to], "cc": [(a.lower(), "") for a in cc], "date": now_iso(),
                   "conversation_id": res.get("conversation_id")}
            if not db.get("my_email"):
                db.set("my_email", "me@localhost")
            eng.process_message(msg, "sent")
            if f.get("opp_id"):
                db.ex("UPDATE activities SET opp_id=? WHERE message_id=?", (f["opp_id"], mid))
            if f.get("followup_days"):
                d = (date.today() + timedelta(days=int(f["followup_days"]))).isoformat()
                for a in to:
                    r = db.one("SELECT id FROM contacts WHERE email=?", (a.lower(),))
                    if r:
                        db.ex("INSERT INTO tasks(contact_id, opp_id, title, due_date, kind, created_at) VALUES(?,?,?,?,?,?)",
                              (r["id"], f.get("opp_id") or None, f"Awaiting reply: {subject}", d, "awaiting_reply", now_iso()))
            flash(("Demo mode: email logged but not actually sent." if is_demo else "Email sent from your Outlook account and logged."), "ok")
            return redirect(url_for("contact", cid=c["id"]) if c else url_for("dashboard"))
        tpl = db.one("SELECT * FROM email_templates WHERE id=?", (request.args.get("template", type=int),)) if request.args.get("template") else None
        form = {"to": c["email"] if c else request.args.get("to", ""),
                "subject": fill(tpl["subject"], c) if tpl else request.args.get("subject", ""),
                "body": fill(tpl["body"], c) if tpl else "", "followup_days": "5", "add_sig": "1",
                "opp_id": request.args.get("opp_id", "")}
        return render_template("compose.html", c=c, form=form, templates=db.q("SELECT * FROM email_templates ORDER BY name"),
                               opps=contact_opps(c))

    def contact_opps(c):
        if not c:
            return []
        return db.q("SELECT id, name FROM opportunities WHERE (contact_id=? OR org_id=?) AND stage IN ('Lead','Qualified','Proposal','Submitted')",
                    (c["id"], c["org_id"] or -1))

    @app.route("/templates", methods=["GET", "POST"])
    def email_templates():
        if request.method == "POST":
            f = request.form
            if f.get("delete"):
                db.ex("DELETE FROM email_templates WHERE id=?", (f["delete"],))
            elif f.get("id"):
                db.ex("UPDATE email_templates SET name=?, subject=?, body=? WHERE id=?", (f["name"], f["subject"], f["body"], f["id"]))
            elif f.get("name"):
                db.ex("INSERT INTO email_templates(name, subject, body) VALUES(?,?,?)", (f["name"], f.get("subject", ""), f.get("body", "")))
            return back("Templates saved.")
        return render_template("email_templates.html", rows=db.q("SELECT * FROM email_templates ORDER BY name"))

    # ------------------------------------------------------------ import
    @app.route("/import", methods=["GET", "POST"])
    def import_page():
        if request.method == "POST" and request.files.get("file"):
            up = request.files["file"]
            try:
                headers, rows = importer.read_rows(up.filename, up.read())
            except Exception as e:
                flash(str(e), "error")
                return redirect(url_for("import_page"))
            staged = paths.data_dir() / f"import-{secrets.token_hex(6)}.json"
            staged.write_text(json.dumps({"headers": headers, "rows": rows, "name": up.filename}))
            session["import_file"] = staged.name
            return render_template("import_map.html", headers=headers, sample=rows[:5], count=len(rows),
                                   mapping=importer.guess_mapping(headers), labels=importer.LABELS, filename=up.filename)
        if request.method == "POST" and request.form.get("confirm"):
            name = session.pop("import_file", None)
            p = paths.data_dir() / (name or "")
            if not name or not p.exists():
                flash("Upload the file again, please.", "error")
                return redirect(url_for("import_page"))
            data = json.loads(p.read_text())
            p.unlink()
            mapping = [request.form.get(f"map_{i}", "") for i in range(len(data["headers"]))]
            if "email" not in mapping and "full_name" not in mapping and "last_name" not in mapping:
                flash("Map at least an Email or Name column.", "error")
                return redirect(url_for("import_page"))
            res = importer.run_import(db, data["headers"], data["rows"], mapping,
                                      update_existing=bool(request.form.get("update_existing")),
                                      tag=request.form.get("tag", "").strip())
            flash(f"Import done: {res['created']} added, {res['updated']} updated, {res['skipped']} skipped."
                  + (f" {len(res['errors'])} rows had problems." if res["errors"] else ""), "ok" if not res["errors"] else "error")
            return redirect(url_for("contacts", tag=request.form.get("tag", "").strip() or None))
        return render_template("import.html")

    @app.route("/import/sample.csv")
    def import_sample():
        return csv_response("relay_import_template.csv",
                            ["First Name", "Last Name", "Email", "Phone", "Title", "Agency", "Tags", "Notes"],
                            [["Jordan", "Reyes", "jordan.reyes@agency.gov", "202-555-0100", "Program Manager",
                              "Example Agency", "event-2025", "Met at conference"]])

    # ------------------------------------------------------------ reports
    @app.route("/reports")
    def reports_index():
        return render_template("reports.html", catalog=reports.CATALOG)

    @app.route("/reports/<key>")
    def report(key):
        if key not in {k for k, _, _ in reports.CATALOG}:
            abort(404)
        r = reports.run(db, key, request.args.get("start"), request.args.get("end"), request.args.get("org_type"))
        if request.args.get("format") == "csv":
            return csv_response(f"report_{key}_{r['start']}_{r['end']}.csv", r["columns"], r["rows"])
        return render_template("report.html", r=r, catalog=reports.CATALOG)

    # ------------------------------------------------------------ search
    @app.route("/search")
    def search():
        q = request.args.get("q", "").strip()
        if not q:
            return redirect("/")
        like = f"%{q}%"
        cs = db.q("""SELECT c.*, g.name org FROM contacts c LEFT JOIN orgs g ON g.id=c.org_id
                     WHERE c.first_name LIKE ? OR c.last_name LIKE ? OR c.email LIKE ? OR (c.first_name||' '||c.last_name) LIKE ?
                     OR c.title LIKE ? OR c.tags LIKE ? LIMIT 50""", (like,) * 6)
        os_ = db.q("SELECT * FROM orgs WHERE name LIKE ? OR domain LIKE ? LIMIT 30", (like, like))
        ops = db.q("SELECT * FROM opportunities WHERE name LIKE ? OR solicitation LIKE ? OR vehicle LIKE ? LIMIT 30", (like,) * 3)
        acts = db.q("""SELECT a.*, c.first_name, c.last_name, c.email, c.id cid FROM activities a JOIN contacts c ON c.id=a.contact_id
                       WHERE a.subject LIKE ? OR a.body LIKE ? ORDER BY a.occurred_at DESC LIMIT 40""", (like, like))
        if len(cs) == 1 and not os_ and not ops and not acts:
            return redirect(url_for("contact", cid=cs[0]["id"]))
        return render_template("search.html", q=q, cs=cs, os=os_, ops=ops, acts=acts)

    # ------------------------------------------------------------ settings
    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        if request.method == "POST":
            f = request.form
            section = f.get("section")
            if section == "general":
                for k in ("my_name", "my_email", "aliases", "crm_folder", "awaiting_reply_days", "reply_needed_days",
                          "sync_minutes", "first_sync_days", "signature"):
                    if k in f:
                        db.set(k, f.get(k, "").strip() if k != "signature" else f.get(k, ""))
                for k in ("skip_internal", "auto_create_inbound", "auto_create_outbound"):
                    db.set(k, "1" if f.get(k) else "0")
                flash("Settings saved.", "ok")
            elif section == "m365":
                old = (db.get("m365_tenant_id"), db.get("m365_client_id"), db.get("m365_cloud"))
                db.set("m365_cloud", f.get("m365_cloud") if f.get("m365_cloud") in CLOUD_LABELS else "commercial")
                db.set("m365_tenant_id", f.get("m365_tenant_id", "").strip())
                db.set("m365_client_id", f.get("m365_client_id", "").strip())
                if old != (db.get("m365_tenant_id"), db.get("m365_client_id"), db.get("m365_cloud")):
                    m365.sign_out()
                flash("Microsoft 365 details saved. Now click Sign in.", "ok")
            elif section == "password":
                if f.get("remove"):
                    db.set("password_hash", "")
                    flash("Password lock removed.", "ok")
                elif len(f.get("password", "")) < 6:
                    flash("Use at least 6 characters.", "error")
                elif f.get("password") != f.get("password2"):
                    flash("The two passwords don't match.", "error")
                else:
                    db.set("password_hash", generate_password_hash(f["password"]))
                    session["ok"] = True
                    flash("Password lock turned on.", "ok")
            return redirect(url_for("settings") + "#" + (section or ""))
        acct = None
        try:
            a = m365.account()
            acct = a.get("username") if a else None
        except Exception as e:
            log.warning("account check failed: %s", e)
        return render_template("settings.html", clouds=CLOUD_LABELS, account=acct, data_dir=str(paths.data_dir()),
                               login=m365.login_status())

    @app.route("/settings/mode", methods=["POST"])
    def set_mode():
        mode = request.form.get("mode")
        if mode == "live":
            if not (m365.configured() and m365.account()):
                flash("Sign in to Microsoft 365 first.", "error")
                return redirect(url_for("settings") + "#m365")
            if db.get("mode") == "demo":
                if request.form.get("clear_demo"):
                    db.wipe_data()
                    for name, subj, body in demo.TEMPLATES:
                        db.ex("INSERT INTO email_templates(name, subject, body) VALUES(?,?,?)", (name, subj, body))
                db.set("my_email", "")  # re-read from the signed-in account
                try:
                    me = m365.me()
                    db.set("my_email", (me.get("mail") or me.get("userPrincipalName") or "").lower())
                    if me.get("displayName"):
                        db.set("my_name", me["displayName"])
                except Exception as e:
                    flash(f"Couldn't read your profile: {e}", "error")
            db.set("mode", "live")
            db.set("last_sync_at", "")
            flash("Live mode on. Your mailbox will sync every few minutes; click Sync email to start now.", "ok")
        elif mode == "demo":
            demo.seed(db)
            flash("Demo data loaded.", "ok")
        return redirect(url_for("settings"))

    @app.route("/m365/login", methods=["POST"])
    def m365_login():
        try:
            st = m365.start_device_login()
        except Exception as e:
            return jsonify(status="error", error=str(e)), 200
        return jsonify(st)

    @app.route("/m365/status")
    def m365_status():
        return jsonify(m365.login_status())

    @app.route("/m365/test", methods=["POST"])
    def m365_test():
        try:
            me = m365.me()
            return back(f"Connected as {me.get('displayName')} ({me.get('mail') or me.get('userPrincipalName')}).")
        except Exception as e:
            return back(f"Connection test failed: {e}", "error")

    @app.route("/m365/logout", methods=["POST"])
    def m365_logout():
        m365.sign_out()
        if db.get("mode") == "live":
            db.set("mode", "demo")
        return back("Signed out of Microsoft 365.")

    @app.route("/backup")
    def backup():
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        dest = paths.data_dir() / "backups" / f"relay-backup-{stamp}.db"
        db.backup_to(dest)
        return send_file(dest, as_attachment=True, download_name=dest.name)

    @app.route("/restore", methods=["POST"])
    def restore():
        up = request.files.get("file")
        if not up:
            return back("Choose a backup file.", "error")
        tmp = paths.data_dir() / "restore-upload.db"
        up.save(tmp)
        import sqlite3
        try:
            src = sqlite3.connect(str(tmp))
            src.execute("SELECT count(*) FROM contacts").fetchone()
            safety = paths.data_dir() / "backups" / f"before-restore-{datetime.now():%Y%m%d-%H%M%S}.db"
            db.backup_to(safety)
            with db.tx() as conn:
                src.backup(conn)
            src.close()
        except Exception as e:
            return back(f"That isn't a Relay CRM backup: {e}", "error")
        finally:
            tmp.unlink(missing_ok=True)
        return back("Backup restored. (Your previous data was saved in the backups folder.)")

    # ------------------------------------------------------------ helpers
    def back(msg=None, kind="ok"):
        if msg:
            flash(msg, kind)
        nxt = request.form.get("next") or request.referrer or "/"
        if not nxt.startswith(("/", "http://127.0.0.1", "http://localhost")):
            nxt = "/"
        return redirect(nxt)

    def csv_response(name, columns, rows):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(columns)
        w.writerows(rows)
        return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.errorhandler(404)
    def nf(e):
        return render_template("error.html", code=404, msg="That page or record doesn't exist."), 404

    @app.errorhandler(400)
    def bad(e):
        return render_template("error.html", code=400, msg=getattr(e, "description", "Bad request")), 400

    @app.errorhandler(500)
    def err(e):
        log.exception("server error")
        return render_template("error.html", code=500, msg="Something went wrong. Details are in relay.log."), 500

    return app
