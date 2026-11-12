"""Core CRM screens: dashboard, contacts, organisations, pipeline, tasks,
compose, templates, reports, import, sync, search."""
import json
import logging
import os
import secrets
from datetime import date, datetime, timedelta, timezone

from flask import abort, flash, g, jsonify, redirect, render_template, request, session, url_for

from . import auth, db, importer, reports, sync
from .paths import DATA_DIR
from .web import (audit, csv_response, display_name, form_owner, my_id, owner_sql, scope, split_addresses,
                  team_members)

log = logging.getLogger("relay")


def _ajax():
    return bool(request.headers.get("X-Requested-With")) or request.is_json


def _require_delete(owner_id):
    if not auth.can_delete(owner_id):
        abort(403)


def register(app):
    # ================================================================ dashboard
    @app.route("/")
    def dashboard():
        own, sel = scope()
        won, lost = db.closed_stages()
        today = date.today().isoformat()
        c_sql, c_a = owner_sql("owner_id", own)
        p_sql, p_a = owner_sql("owner_id", own)
        a_sql, a_a = owner_sql("a.user_id", own)
        t_sql, t_a = owner_sql("t.assigned_to", own)
        kpi = {
            "contacts": db.query(f"SELECT COUNT(*) n FROM contacts WHERE 1=1 {c_sql}", c_a, one=True)["n"],
            "new30": db.query(f"SELECT COUNT(*) n FROM contacts WHERE created_at >= ? {c_sql}",
                              [(date.today() - timedelta(days=30)).isoformat(), *c_a], one=True)["n"],
            "open_value": db.query(f"SELECT COALESCE(SUM(value),0) v FROM opportunities WHERE stage NOT IN (?,?) {p_sql}",
                                   [won, lost, *p_a], one=True)["v"],
            "weighted": db.query(f"SELECT COALESCE(SUM(value*probability/100.0),0) v FROM opportunities "
                                 f"WHERE stage NOT IN (?,?) {p_sql}", [won, lost, *p_a], one=True)["v"],
            "emails7": db.query(f"SELECT COUNT(*) n FROM activities a WHERE kind IN ('email_in','email_out') "
                                f"AND occurred_at >= ? {a_sql}",
                                [(datetime.now() - timedelta(days=7)).isoformat(sep=" "), *a_a], one=True)["n"],
            "overdue": db.query(f"SELECT COUNT(*) n FROM tasks t WHERE status='Open' AND due_date < ? {t_sql}",
                                [today, *t_a], one=True)["n"],
        }
        tasks = db.query(f"""SELECT t.*, c.first_name, c.last_name, c.email FROM tasks t
                             LEFT JOIN contacts c ON c.id=t.contact_id WHERE t.status='Open' {t_sql}
                             ORDER BY CASE WHEN t.due_date IS NULL THEN 1 ELSE 0 END, t.due_date,
                             CASE t.priority WHEN 'High' THEN 0 WHEN 'Normal' THEN 1 ELSE 2 END LIMIT 8""", t_a)
        feed = db.query(f"""SELECT a.*, c.first_name, c.last_name, c.email FROM activities a
                            LEFT JOIN contacts c ON c.id=a.contact_id WHERE 1=1 {a_sql}
                            ORDER BY a.occurred_at DESC, a.id DESC LIMIT 12""", a_a)
        review = db.query(f"""SELECT c.*, o.name org_name FROM contacts c LEFT JOIN organizations o ON o.id=c.org_id
                              WHERE c.needs_review=1 {owner_sql('c.owner_id', own)[0]}
                              ORDER BY c.created_at DESC LIMIT 6""", owner_sql("c.owner_id", own)[1])
        pipe = db.query(f"SELECT stage, COUNT(*) n, COALESCE(SUM(value),0) v FROM opportunities WHERE 1=1 {p_sql} "
                        "GROUP BY stage", p_a)
        pmap = {r["stage"]: r for r in pipe}
        closing = db.query(f"""SELECT p.*, o.name org_name FROM opportunities p LEFT JOIN organizations o ON o.id=p.org_id
                               WHERE p.stage NOT IN (?,?) {owner_sql('p.owner_id', own)[0]}
                               ORDER BY p.close_date LIMIT 5""", [won, lost, *owner_sql("p.owner_id", own)[1]])
        leaderboard = None
        if own is None:
            leaderboard = db.query("""SELECT u.id, u.name, u.email,
                (SELECT COUNT(*) FROM activities a WHERE a.user_id=u.id AND a.kind IN ('email_in','email_out','call','meeting')
                   AND a.occurred_at >= ?) acts,
                (SELECT COALESCE(SUM(value),0) FROM opportunities p WHERE p.owner_id=u.id AND p.stage NOT IN (?,?)) pipe,
                (SELECT COUNT(*) FROM tasks t WHERE t.assigned_to=u.id AND t.status='Open' AND t.due_date < ?) overdue
                FROM users u WHERE u.active=1 AND u.role!='viewer' ORDER BY acts DESC""",
                                   ((datetime.now() - timedelta(days=30)).isoformat(sep=" "), won, lost, today))
        return render_template("dashboard.html", kpi=kpi, tasks=tasks, feed=feed, review=review, pmap=pmap,
                               closing=closing, sel=sel, leaderboard=leaderboard)

    # ================================================================ contacts
    @app.route("/contacts")
    def contacts():
        q = request.args.get("q", "").strip()
        status = request.args.get("status", "")
        org = request.args.get("org", "")
        review = request.args.get("review", "")
        source = request.args.get("source", "")
        sort = request.args.get("sort", "last")
        own, sel = scope(default="team" if q else None)
        where, args = ["1=1"], []
        if q:
            where.append("(c.first_name || ' ' || c.last_name LIKE ? OR c.email LIKE ? OR o.name LIKE ? "
                         "OR c.title LIKE ? OR c.tags LIKE ?)")
            args += [f"%{q}%"] * 5
        if status:
            where.append("c.status=?"); args.append(status)
        if org:
            where.append("c.org_id=?"); args.append(org)
        if review:
            where.append("c.needs_review=1")
        if source == "email":
            where.append("c.source LIKE 'Email%'")
        osql, oargs = owner_sql("c.owner_id", own)
        order = {"last": "c.last_contacted IS NULL, c.last_contacted DESC", "name": "c.last_name, c.first_name",
                 "created": "c.created_at DESC", "org": "o.name"}.get(sort, "c.last_contacted DESC")
        rows = db.query(f"""SELECT c.*, o.name org_name FROM contacts c LEFT JOIN organizations o ON o.id=c.org_id
                            WHERE {' AND '.join(where)} {osql} ORDER BY {order} LIMIT 1000""", args + oargs)
        orgs = db.query("SELECT id, name FROM organizations ORDER BY name")
        return render_template("contacts.html", rows=rows, orgs=orgs, q=q, status=status, org=org, review=review,
                               sort=sort, source=source, sel=sel)

    @app.route("/contacts/export.csv")
    def contacts_export():
        if not auth.can_export():
            abort(403)
        own, _sel = scope(default="team")
        o_sql, o_a = owner_sql("c.owner_id", own)
        rows = db.query(f"""SELECT c.first_name, c.last_name, c.email, c.title, c.phone, c.mobile, o.name organization,
                           o.org_type, c.status, c.source, c.tags, u.name owner, c.last_contacted, c.emails_out,
                           c.emails_in, c.created_at, c.notes FROM contacts c LEFT JOIN organizations o ON o.id=c.org_id
                           LEFT JOIN users u ON u.id=c.owner_id WHERE 1=1 {o_sql} ORDER BY c.last_name""", o_a)
        audit("export", "contact", None, f"{len(rows)} contacts to CSV")
        return csv_response("contacts.csv", rows[0].keys() if rows else ["first_name"], [tuple(r) for r in rows])

    def _contact_form_values(f, existing=None):
        org_id = f.get("org_id") or None
        new_org = f.get("new_org", "").strip()
        if new_org:
            dom = sync.domain_of(f.get("email", ""))
            if dom and (dom in sync.PUBLIC_DOMAINS or db.query("SELECT 1 FROM organizations WHERE domain=?", (dom,), one=True)):
                dom = None
            org_id = db.execute("INSERT INTO organizations(name, org_type, domain, owner_id, created_at) VALUES (?,?,?,?,?)",
                                (new_org, f.get("new_org_type", ""), dom or None, my_id(), db.now_iso()))
        v = dict(first_name=f.get("first_name", "").strip(), last_name=f.get("last_name", "").strip(),
                 email=(f.get("email", "").strip().lower() or None), title=f.get("title", "").strip(),
                 phone=f.get("phone", "").strip(), mobile=f.get("mobile", "").strip(), org_id=org_id,
                 status=f.get("status", "Lead"), tags=f.get("tags", "").strip(), notes=f.get("notes", ""))
        v["owner_id"] = form_owner(f, default=existing["owner_id"] if existing else my_id())
        return v

    @app.route("/contacts/new", methods=["GET", "POST"])
    def contact_new():
        if request.method == "POST":
            v = _contact_form_values(request.form)
            if v["email"] and sync.find_contact(v["email"]):
                flash("A contact with that email already exists.", "error")
                return redirect(url_for("contact_detail", cid=sync.find_contact(v["email"])["id"]))
            if v["email"] and v["email"] in sync.team_directory():
                flash("That address belongs to a team member, so it can't be a contact.", "error")
                return redirect(url_for("contact_new"))
            if v["email"] and not v["org_id"]:
                v["org_id"] = sync.get_or_create_org(sync.domain_of(v["email"]), v["owner_id"])
            now = db.now_iso()
            cid = db.execute(f"""INSERT INTO contacts({', '.join(v)}, source, created_by, created_at, updated_at)
                                 VALUES ({', '.join('?' * len(v))}, 'Manual', ?, ?, ?)""",
                             (*v.values(), my_id(), now, now))
            audit("create", "contact", cid, v["email"] or f"{v['first_name']} {v['last_name']}")
            flash("Contact created.", "ok")
            return redirect(url_for("contact_detail", cid=cid))
        orgs = db.query("SELECT id, name FROM organizations ORDER BY name")
        return render_template("contact_form.html", c=None, orgs=orgs, members=team_members(),
                               preset_org=request.args.get("org", type=int))

    @app.route("/contacts/<int:cid>")
    def contact_detail(cid):
        c = db.query("SELECT c.*, o.name org_name, o.org_type FROM contacts c "
                     "LEFT JOIN organizations o ON o.id=c.org_id WHERE c.id=?", (cid,), one=True)
        if not c:
            abort(404)
        kind = request.args.get("kind", "")
        acts = db.query(f"""SELECT a.*, p.name opp_name FROM activities a LEFT JOIN opportunities p ON p.id=a.opp_id
                            WHERE a.contact_id=? {"AND a.kind=?" if kind else ""}
                            ORDER BY a.occurred_at DESC, a.id DESC""", (cid, kind) if kind else (cid,))
        tasks = db.query("SELECT * FROM tasks WHERE contact_id=? ORDER BY status DESC, due_date", (cid,))
        opps = db.query("SELECT * FROM opportunities WHERE contact_id=? OR (org_id=? AND org_id IS NOT NULL) "
                        "ORDER BY updated_at DESC", (cid, c["org_id"]))
        colleagues = db.query("SELECT * FROM contacts WHERE org_id=? AND id!=? ORDER BY last_contacted DESC LIMIT 8",
                              (c["org_id"], cid)) if c["org_id"] else []
        team_touch = db.query("""SELECT a.user_id, COUNT(*) n, MAX(a.occurred_at) last FROM activities a
                                 WHERE a.contact_id=? AND a.user_id IS NOT NULL AND a.kind != 'system'
                                 GROUP BY a.user_id ORDER BY last DESC""", (cid,))
        return render_template("contact_detail.html", c=c, acts=acts, tasks=tasks, opps=opps, colleagues=colleagues,
                               kind=kind, members=team_members(), team_touch=team_touch)

    @app.route("/contacts/<int:cid>/edit", methods=["GET", "POST"])
    def contact_edit(cid):
        c = db.query("SELECT * FROM contacts WHERE id=?", (cid,), one=True)
        if not c:
            abort(404)
        if request.method == "POST":
            v = _contact_form_values(request.form, existing=c)
            other = sync.find_contact(v["email"]) if v["email"] else None
            if other and other["id"] != cid:
                flash("Another contact already uses that email.", "error")
                return redirect(url_for("contact_edit", cid=cid))
            v["needs_review"] = 0
            v["updated_at"] = db.now_iso()
            changed = [k for k in v if k not in ("updated_at", "needs_review") and (c[k] or "") != (v[k] or "")]
            db.execute(f"UPDATE contacts SET {', '.join(k + '=?' for k in v)} WHERE id=?", (*v.values(), cid))
            audit("update", "contact", cid, "changed: " + ", ".join(changed) if changed else "no changes")
            flash("Contact saved.", "ok")
            return redirect(url_for("contact_detail", cid=cid))
        orgs = db.query("SELECT id, name FROM organizations ORDER BY name")
        return render_template("contact_form.html", c=c, orgs=orgs, members=team_members(), preset_org=None)

    @app.route("/contacts/<int:cid>/approve", methods=["POST"])
    def contact_approve(cid):
        db.execute("UPDATE contacts SET needs_review=0, owner_id=COALESCE(owner_id, ?) WHERE id=?", (my_id(), cid))
        if _ajax():
            return jsonify(ok=True)
        flash("Contact confirmed.", "ok")
        return redirect(request.referrer or url_for("contacts"))

    @app.route("/contacts/<int:cid>/assign", methods=["POST"])
    def contact_assign(cid):
        c = db.query("SELECT id, owner_id FROM contacts WHERE id=?", (cid,), one=True)
        if not c:
            abort(404)
        new = form_owner(request.form, default=c["owner_id"])
        db.execute("UPDATE contacts SET owner_id=?, updated_at=? WHERE id=?", (new, db.now_iso(), cid))
        audit("assign", "contact", cid, f"owner -> {new}")
        flash("Owner updated.", "ok")
        return redirect(request.referrer or url_for("contact_detail", cid=cid))

    @app.route("/contacts/<int:cid>/delete", methods=["POST"])
    def contact_delete(cid):
        c = db.query("SELECT * FROM contacts WHERE id=?", (cid,), one=True)
        if not c:
            abort(404)
        _require_delete(c["owner_id"])
        ignore = request.form.get("ignore")
        db.execute("DELETE FROM contacts WHERE id=?", (cid,))
        audit("delete", "contact", cid, f"{display_name(c)} <{c['email'] or ''}>")
        if ignore and c["email"]:
            pats = db.setting_list("ignore_patterns")
            if c["email"] not in pats:
                pats.append(c["email"])
                db.set_setting("ignore_patterns", ",".join(pats))
            flash(f"Contact deleted and {c['email']} will be ignored by future syncs.", "ok")
        else:
            flash("Contact deleted.", "ok")
        return redirect(url_for("contacts"))

    @app.route("/contacts/bulk", methods=["POST"])
    def contacts_bulk():
        ids = [int(i) for i in request.form.getlist("ids") if i.isdigit()]
        action = request.form.get("action") or ""
        ph = ",".join("?" * len(ids))
        if not ids:
            flash("No contacts selected.", "error")
        elif action == "approve":
            db.execute(f"UPDATE contacts SET needs_review=0 WHERE id IN ({ph})", ids)
            flash(f"{len(ids)} contacts confirmed.", "ok")
        elif action == "delete":
            if auth.is_admin():
                allowed = ids
            else:
                rows = db.query(f"SELECT id, owner_id FROM contacts WHERE id IN ({ph})", ids)
                allowed = [r["id"] for r in rows if auth.can_delete(r["owner_id"])]
            if allowed:
                db.execute(f"DELETE FROM contacts WHERE id IN ({','.join('?' * len(allowed))})", allowed)
                audit("delete", "contact", None, f"bulk delete of {len(allowed)}: ids {allowed[:50]}")
            skipped = len(ids) - len(allowed)
            flash(f"{len(allowed)} contacts deleted." + (f" {skipped} skipped (you can only delete contacts you own)."
                                                         if skipped else ""), "ok" if allowed else "error")
        elif action.startswith("status:"):
            db.execute(f"UPDATE contacts SET status=? WHERE id IN ({ph})", [action.split(":", 1)[1], *ids])
            audit("update", "contact", None, f"bulk status {action.split(':', 1)[1]} for {len(ids)}")
            flash(f"{len(ids)} contacts updated.", "ok")
        elif action.startswith("owner:"):
            new = form_owner({"owner_id": action.split(":", 1)[1]}, default=None)
            db.execute(f"UPDATE contacts SET owner_id=? WHERE id IN ({ph})", [new, *ids])
            audit("assign", "contact", None, f"bulk owner -> {new} for {len(ids)}")
            flash(f"{len(ids)} contacts reassigned.", "ok")
        return redirect(request.referrer or url_for("contacts"))

    @app.route("/contacts/<int:cid>/log", methods=["POST"])
    def contact_log(cid):
        c = db.query("SELECT * FROM contacts WHERE id=?", (cid,), one=True)
        if not c:
            abort(404)
        f = request.form
        kind = f.get("kind", "note")
        if kind not in ("call", "meeting", "note"):
            kind = "note"
        when = f.get("occurred_at") or datetime.now().strftime("%Y-%m-%dT%H:%M")
        when = when.replace("T", " ") + (":00" if len(when) == 16 else "")
        dur = f.get("duration_min", type=int)
        opp_id = f.get("opp_id", type=int) or None
        db.execute("""INSERT INTO activities(contact_id, opp_id, org_id, user_id, kind, subject, body, outcome,
                      duration_min, occurred_at, created_at, via) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (cid, opp_id, c["org_id"], my_id(), kind, f.get("subject", "").strip() or kind.title(),
                    f.get("body", ""), f.get("outcome", ""), dur, when, db.now_iso(), "manual"))
        if kind in ("call", "meeting"):
            db.execute("""UPDATE contacts SET last_contacted = CASE WHEN last_contacted IS NULL OR last_contacted < ?
                          THEN ? ELSE last_contacted END, updated_at=? WHERE id=?""", (when, when, db.now_iso(), cid))
        if f.get("followup_date"):
            db.execute("INSERT INTO tasks(title, contact_id, opp_id, assigned_to, created_by, due_date, priority, "
                       "created_at) VALUES (?,?,?,?,?,?,?,?)",
                       (f.get("followup_title") or f"Follow up with {display_name(c)}", cid, opp_id, my_id(), my_id(),
                        f.get("followup_date"), "Normal", db.now_iso()))
        if c["owner_id"] is None:
            db.execute("UPDATE contacts SET owner_id=? WHERE id=?", (my_id(), cid))
        flash(f"{ {'call': 'Call', 'meeting': 'Meeting', 'note': 'Note'}.get(kind, 'Activity') } logged.", "ok")
        return redirect(url_for("contact_detail", cid=cid))

    @app.route("/activities/<int:aid>/delete", methods=["POST"])
    def activity_delete(aid):
        a = db.query("SELECT * FROM activities WHERE id=?", (aid,), one=True)
        if not a:
            abort(404)
        if not (auth.is_admin() or (a["user_id"] == my_id() and a["kind"] in ("call", "meeting", "note"))):
            abort(403)
        db.execute("DELETE FROM activities WHERE id=?", (aid,))
        audit("delete", "activity", aid, f"{a['kind']}: {a['subject']}")
        flash("Activity removed.", "ok")
        return redirect(url_for("contact_detail", cid=a["contact_id"]) if a["contact_id"] else url_for("dashboard"))

    @app.route("/activities/<int:aid>/link", methods=["POST"])
    def activity_link(aid):
        opp_id = request.form.get("opp_id", type=int) or None
        db.execute("UPDATE activities SET opp_id=? WHERE id=?", (opp_id, aid))
        a = db.query("SELECT contact_id FROM activities WHERE id=?", (aid,), one=True)
        return redirect(url_for("contact_detail", cid=a["contact_id"]) if a else url_for("dashboard"))

    # ================================================================ organisations
    @app.route("/orgs")
    def orgs():
        q = request.args.get("q", "").strip()
        t = request.args.get("type", "")
        won, lost = db.closed_stages()
        where, args = ["1=1"], []
        if q:
            where.append("(o.name LIKE ? OR o.domain LIKE ?)"); args += [f"%{q}%"] * 2
        if t:
            where.append("o.org_type=?"); args.append(t)
        rows = db.query(f"""SELECT o.*, (SELECT COUNT(*) FROM contacts c WHERE c.org_id=o.id) nc,
                            (SELECT COALESCE(SUM(value),0) FROM opportunities p WHERE p.org_id=o.id AND p.stage NOT IN (?,?)) ov,
                            (SELECT MAX(last_contacted) FROM contacts c WHERE c.org_id=o.id) last
                            FROM organizations o WHERE {' AND '.join(where)} ORDER BY o.name""", (won, lost, *args))
        return render_template("orgs.html", rows=rows, q=q, t=t, members=team_members())

    @app.route("/orgs/new", methods=["POST"])
    def org_new():
        f = request.form
        dom = f.get("domain", "").strip().lower() or None
        if dom and db.query("SELECT 1 FROM organizations WHERE domain=?", (dom,), one=True):
            flash("An organisation already uses that email domain.", "error")
            return redirect(url_for("orgs"))
        oid = db.execute("INSERT INTO organizations(name, domain, org_type, phone, website, address, notes, owner_id, "
                         "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                         (f["name"], dom, f.get("org_type", ""), f.get("phone", ""), f.get("website", ""),
                          f.get("address", ""), f.get("notes", ""), form_owner(f, default=my_id()), db.now_iso()))
        audit("create", "organization", oid, f["name"])
        return redirect(url_for("org_detail", oid=oid))

    @app.route("/orgs/<int:oid>", methods=["GET", "POST"])
    def org_detail(oid):
        o = db.query("SELECT * FROM organizations WHERE id=?", (oid,), one=True)
        if not o:
            abort(404)
        if request.method == "POST":
            f = request.form
            try:
                db.execute("UPDATE organizations SET name=?, domain=?, org_type=?, phone=?, website=?, address=?, "
                           "notes=?, owner_id=? WHERE id=?",
                           (f["name"], f.get("domain", "").strip().lower() or None, f.get("org_type", ""),
                            f.get("phone", ""), f.get("website", ""), f.get("address", ""), f.get("notes", ""),
                            form_owner(f, default=o["owner_id"]), oid))
                audit("update", "organization", oid, f["name"])
                flash("Organisation saved.", "ok")
            except Exception:
                flash("That email domain is already assigned to another organisation.", "error")
            return redirect(url_for("org_detail", oid=oid))
        people = db.query("SELECT * FROM contacts WHERE org_id=? ORDER BY last_contacted DESC", (oid,))
        opps = db.query("SELECT * FROM opportunities WHERE org_id=? ORDER BY updated_at DESC", (oid,))
        acts = db.query("""SELECT a.*, c.first_name, c.last_name, c.email FROM activities a
                           LEFT JOIN contacts c ON c.id=a.contact_id WHERE a.org_id=?
                           ORDER BY a.occurred_at DESC LIMIT 40""", (oid,))
        all_orgs = db.query("SELECT id, name FROM organizations WHERE id!=? ORDER BY name", (oid,))
        return render_template("org_detail.html", o=o, people=people, opps=opps, acts=acts, all_orgs=all_orgs,
                               members=team_members())

    @app.route("/orgs/<int:oid>/merge", methods=["POST"])
    def org_merge(oid):
        if not auth.is_admin():
            abort(403)
        target = request.form.get("target", type=int)
        if not target or target == oid:
            return redirect(url_for("org_detail", oid=oid))
        for t in ("contacts", "opportunities", "activities"):
            db.execute(f"UPDATE {t} SET org_id=? WHERE org_id=?", (target, oid))
        src = db.query("SELECT name, domain FROM organizations WHERE id=?", (oid,), one=True)
        db.execute("DELETE FROM organizations WHERE id=?", (oid,))
        audit("merge", "organization", target, f"merged '{src['name'] if src else oid}' into this organisation")
        flash(f"Merged. Note: domain '{src['domain']}' was removed; new emails from it will match the parent "
              "domain if set." if src and src["domain"] else "Merged.", "ok")
        return redirect(url_for("org_detail", oid=target))

    @app.route("/orgs/<int:oid>/delete", methods=["POST"])
    def org_delete(oid):
        o = db.query("SELECT * FROM organizations WHERE id=?", (oid,), one=True)
        if not o:
            abort(404)
        _require_delete(o["owner_id"])
        db.execute("DELETE FROM organizations WHERE id=?", (oid,))
        audit("delete", "organization", oid, o["name"])
        flash("Organisation deleted (contacts kept).", "ok")
        return redirect(url_for("orgs"))

    # ================================================================ pipeline
    @app.route("/pipeline")
    def pipeline():
        t = request.args.get("type", "")
        show_closed = request.args.get("closed", "1")
        own, sel = scope(default="team")
        args, extra = [], ""
        if t:
            extra = " AND o.org_type=?"; args.append(t)
        osql, oargs = owner_sql("p.owner_id", own)
        rows = db.query(f"""SELECT p.*, o.name org_name, o.org_type, c.first_name, c.last_name, c.email,
                            (SELECT MAX(occurred_at) FROM activities a WHERE a.opp_id=p.id) last_act
                            FROM opportunities p LEFT JOIN organizations o ON o.id=p.org_id
                            LEFT JOIN contacts c ON c.id=p.contact_id WHERE 1=1 {extra} {osql}
                            ORDER BY p.close_date""", args + oargs)
        cols = {s: [] for s in db.setting_list("stages")}
        for r in rows:
            cols.setdefault(r["stage"], []).append(r)
        won, lost = db.closed_stages()
        if show_closed == "0":
            cols.pop(won, None); cols.pop(lost, None)
        return render_template("pipeline.html", cols=cols, t=t, show_closed=show_closed, sel=sel)

    def _opp_values(f, existing=None):
        stage = f.get("stage", "Lead")
        if stage not in db.setting_list("stages"):
            stage = db.setting_list("stages")[0]
        prob = f.get("probability", type=int)
        if prob is None:
            prob = db.stage_probabilities().get(stage, 10)
        prob = max(0, min(100, prob))
        contact_id = f.get("contact_id", type=int) or None
        org_id = f.get("org_id", type=int) or None
        if contact_id and not org_id:
            c = db.query("SELECT org_id FROM contacts WHERE id=?", (contact_id,), one=True)
            org_id = c["org_id"] if c else None
        try:
            value = float((f.get("value") or "0").replace(",", "").replace("$", ""))
        except ValueError:
            value = 0
        return dict(name=f.get("name", "").strip() or "Untitled opportunity", org_id=org_id, contact_id=contact_id,
                    stage=stage, value=value, probability=prob, close_date=f.get("close_date") or None,
                    solicitation_no=f.get("solicitation_no", ""), contract_vehicle=f.get("contract_vehicle", ""),
                    notes=f.get("notes", ""),
                    owner_id=form_owner(f, default=existing["owner_id"] if existing else my_id()))

    @app.route("/opps/new", methods=["GET", "POST"])
    def opp_new():
        if request.method == "POST":
            v = _opp_values(request.form)
            now = db.now_iso()
            oid = db.execute(f"""INSERT INTO opportunities({', '.join(v)}, created_at, updated_at, stage_changed_at)
                                 VALUES ({', '.join('?' * len(v))}, ?, ?, ?)""", (*v.values(), now, now, now))
            audit("create", "opportunity", oid, f"{v['name']} ({v['stage']}, ${v['value']:,.0f})")
            flash("Opportunity created.", "ok")
            return redirect(url_for("opp_detail", oid=oid))
        contacts_ = db.query("SELECT id, first_name, last_name, email FROM contacts ORDER BY last_name")
        orgs_ = db.query("SELECT id, name FROM organizations ORDER BY name")
        return render_template("opp_form.html", p=None, contacts=contacts_, orgs=orgs_, members=team_members(),
                               preset_contact=request.args.get("contact", type=int),
                               preset_org=request.args.get("org", type=int),
                               preset_stage=request.args.get("stage", "Lead"))

    @app.route("/opps/<int:oid>", methods=["GET", "POST"])
    def opp_detail(oid):
        p = db.query("""SELECT p.*, o.name org_name, c.first_name, c.last_name, c.email FROM opportunities p
                        LEFT JOIN organizations o ON o.id=p.org_id LEFT JOIN contacts c ON c.id=p.contact_id
                        WHERE p.id=?""", (oid,), one=True)
        if not p:
            abort(404)
        if request.method == "POST":
            v = _opp_values(request.form, existing=p)
            old_stage = p["stage"]
            v["updated_at"] = db.now_iso()
            db.execute(f"UPDATE opportunities SET {', '.join(k + '=?' for k in v)} WHERE id=?", (*v.values(), oid))
            if v["stage"] != old_stage:
                _stage_changed(oid, old_stage, v["stage"], keep_prob=True)
            audit("update", "opportunity", oid, f"{v['name']} ({v['stage']}, ${v['value']:,.0f})")
            flash("Opportunity saved.", "ok")
            return redirect(url_for("opp_detail", oid=oid))
        acts = db.query("""SELECT a.*, c.first_name, c.last_name, c.email FROM activities a
                           LEFT JOIN contacts c ON c.id=a.contact_id WHERE a.opp_id=?
                           ORDER BY a.occurred_at DESC""", (oid,))
        tasks = db.query("SELECT * FROM tasks WHERE opp_id=? ORDER BY status DESC, due_date", (oid,))
        contacts_ = db.query("SELECT id, first_name, last_name, email FROM contacts ORDER BY last_name")
        orgs_ = db.query("SELECT id, name FROM organizations ORDER BY name")
        return render_template("opp_detail.html", p=p, acts=acts, tasks=tasks, contacts=contacts_, orgs=orgs_,
                               members=team_members())

    def _stage_changed(oid, old, new, keep_prob=False):
        won, lost = db.closed_stages()
        now = db.now_iso()
        sets = {"stage_changed_at": now, "updated_at": now}
        if not keep_prob:
            sets["probability"] = db.stage_probabilities().get(new, 10)
        sets["closed_at"] = date.today().isoformat() if new in (won, lost) else None
        db.execute(f"UPDATE opportunities SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), oid))
        p = db.query("SELECT * FROM opportunities WHERE id=?", (oid,), one=True)
        db.execute("INSERT INTO activities(contact_id, opp_id, org_id, user_id, kind, subject, body, occurred_at, "
                   "created_at, via) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (p["contact_id"], oid, p["org_id"], my_id(), "stage", f"Stage: {old} \u2192 {new}", p["name"],
                    now, now, "manual"))
        if new == won and p["contact_id"]:
            db.execute("UPDATE contacts SET status='Client' WHERE id=? AND status IN ('Lead','Prospect')",
                       (p["contact_id"],))

    @app.route("/opps/<int:oid>/stage", methods=["POST"])
    def opp_stage(oid):
        new = (request.get_json(silent=True) or {}).get("stage") or request.form.get("stage")
        p = db.query("SELECT stage, name FROM opportunities WHERE id=?", (oid,), one=True)
        if not p or new not in db.setting_list("stages"):
            return jsonify(ok=False), 400
        db.execute("UPDATE opportunities SET stage=? WHERE id=?", (new, oid))
        if p["stage"] != new:
            _stage_changed(oid, p["stage"], new)
            audit("stage", "opportunity", oid, f"{p['name']}: {p['stage']} -> {new}")
        if request.is_json:
            return jsonify(ok=True)
        return redirect(request.referrer or url_for("pipeline"))

    @app.route("/opps/<int:oid>/delete", methods=["POST"])
    def opp_delete(oid):
        p = db.query("SELECT * FROM opportunities WHERE id=?", (oid,), one=True)
        if not p:
            abort(404)
        _require_delete(p["owner_id"])
        db.execute("DELETE FROM opportunities WHERE id=?", (oid,))
        audit("delete", "opportunity", oid, f"{p['name']} ({p['stage']}, ${p['value'] or 0:,.0f})")
        flash("Opportunity deleted.", "ok")
        return redirect(url_for("pipeline"))

    # ================================================================ tasks
    @app.route("/tasks")
    def tasks():
        view = request.args.get("view", "open")
        own, sel = scope()
        today = date.today().isoformat()
        where = {"open": "t.status='Open'", "today": "t.status='Open' AND t.due_date <= ?",
                 "auto": "t.status='Open' AND t.auto_rule != ''", "done": "t.status='Done'"}.get(view, "t.status='Open'")
        args = [today] if view == "today" else []
        osql, oargs = owner_sql("t.assigned_to", own)
        rows = db.query(f"""SELECT t.*, c.first_name, c.last_name, c.email, p.name opp_name FROM tasks t
                            LEFT JOIN contacts c ON c.id=t.contact_id LEFT JOIN opportunities p ON p.id=t.opp_id
                            WHERE {where} {osql} ORDER BY {"t.completed_at DESC" if view == "done" else
                            "CASE WHEN t.due_date IS NULL THEN 1 ELSE 0 END, t.due_date"} LIMIT 500""", args + oargs)
        contacts_ = db.query("SELECT id, first_name, last_name, email FROM contacts ORDER BY last_name")
        return render_template("tasks.html", rows=rows, view=view, contacts=contacts_, sel=sel, members=team_members())

    @app.route("/tasks/new", methods=["POST"])
    def task_new():
        f = request.form
        assignee = form_owner(f, key="assigned_to", default=my_id())
        title = f.get("title", "").strip() or "Follow up"
        tid = db.execute("INSERT INTO tasks(title, contact_id, opp_id, assigned_to, created_by, due_date, priority, notes, "
                         "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                         (title, f.get("contact_id", type=int) or None, f.get("opp_id", type=int) or None, assignee,
                          my_id(), f.get("due_date") or None, f.get("priority", "Normal"), f.get("notes", ""),
                          db.now_iso()))
        if assignee and assignee != my_id():
            audit("assign", "task", tid, f"'{title}' assigned to user {assignee}")
        flash("Task added." if assignee == my_id() else "Task added and assigned.", "ok")
        return redirect(request.referrer or url_for("tasks"))

    def _task_or_404(tid):
        t = db.query("SELECT * FROM tasks WHERE id=?", (tid,), one=True)
        if not t:
            abort(404)
        return t

    @app.route("/tasks/<int:tid>/toggle", methods=["POST"])
    def task_toggle(tid):
        t = _task_or_404(tid)
        if t["status"] == "Open":
            db.execute("UPDATE tasks SET status='Done', completed_at=? WHERE id=?", (db.now_iso(), tid))
        else:
            db.execute("UPDATE tasks SET status='Open', completed_at=NULL WHERE id=?", (tid,))
        if _ajax():
            return jsonify(ok=True)
        return redirect(request.referrer or url_for("tasks"))

    @app.route("/tasks/<int:tid>/snooze", methods=["POST"])
    def task_snooze(tid):
        _task_or_404(tid)
        days = request.form.get("days", type=int) or 3
        db.execute("UPDATE tasks SET due_date=? WHERE id=?", ((date.today() + timedelta(days=days)).isoformat(), tid))
        return redirect(request.referrer or url_for("tasks"))

    @app.route("/tasks/<int:tid>/assign", methods=["POST"])
    def task_assign(tid):
        t = _task_or_404(tid)
        new = form_owner(request.form, key="assigned_to", default=t["assigned_to"])
        db.execute("UPDATE tasks SET assigned_to=? WHERE id=?", (new, tid))
        audit("assign", "task", tid, f"'{t['title']}' -> user {new}")
        return redirect(request.referrer or url_for("tasks"))

    @app.route("/tasks/<int:tid>/delete", methods=["POST"])
    def task_delete(tid):
        t = _task_or_404(tid)
        if not (auth.is_admin() or t["assigned_to"] == my_id() or t["created_by"] == my_id()):
            abort(403)
        db.execute("DELETE FROM tasks WHERE id=?", (tid,))
        audit("delete", "task", tid, t["title"])
        return redirect(request.referrer or url_for("tasks"))

    # ================================================================ compose / send
    def _fill(text, c):
        org = ""
        if c and c["org_id"]:
            r = db.query("SELECT name FROM organizations WHERE id=?", (c["org_id"],), one=True)
            org = r["name"] if r else ""
        vals = {"first_name": (c["first_name"] if c else "") or "there", "last_name": (c["last_name"] if c else "") or "",
                "organization": org or "your organization", "my_name": g.user["name"] or "",
                "my_title": g.user["title"] or "", "firm_name": db.get_setting("firm_name") or ""}
        for k, v in vals.items():
            text = text.replace("{" + k + "}", v)
        return text

    def _mailbox_ready():
        if db.get_setting("mode") != "live":
            return True
        return bool(g.user["ms_token_cache"])

    @app.route("/compose", methods=["GET", "POST"])
    def compose():
        if request.method == "POST":
            return _send()
        c = None
        cid = request.args.get("contact", type=int)
        if cid:
            c = db.query("SELECT * FROM contacts WHERE id=?", (cid,), one=True)
        tpl = None
        if request.args.get("template", type=int):
            tpl = db.query("SELECT * FROM templates WHERE id=?", (request.args.get("template", type=int),), one=True)
        subject = request.args.get("subject", "")
        body = ""
        if tpl:
            subject, body = _fill(tpl["subject"], c), _fill(tpl["body"], c)
        sig = g.user["signature"]
        if sig and sig not in body:
            body = (body + "\n\n" if body else "\n\n") + sig
        opps = db.query("SELECT id, name FROM opportunities WHERE stage NOT IN (?,?) ORDER BY name", db.closed_stages())
        templates_ = db.query("SELECT * FROM templates ORDER BY name")
        contacts_ = db.query("SELECT id, first_name, last_name, email FROM contacts WHERE email IS NOT NULL ORDER BY last_name")
        return render_template("compose.html", c=c, subject=subject, body=body, opps=opps, templates=templates_,
                               contacts=contacts_, opp_id=request.args.get("opp", type=int),
                               mailbox_ready=_mailbox_ready(),
                               followup_default=(date.today() + timedelta(days=int(db.get_setting("followup_days") or 5))).isoformat())

    def _send():
        f = request.form
        to, cc, bcc = split_addresses(f.get("to")), split_addresses(f.get("cc")), split_addresses(f.get("bcc"))
        subject, body = f.get("subject", "").strip(), f.get("body", "")
        if not to:
            flash("Add at least one recipient.", "error")
            return redirect(request.referrer or url_for("compose"))
        mode = db.get_setting("mode")
        u = g.user
        sender = (u["ms_email"] or u["email"]).lower()
        try:
            if mode == "live":
                from .graph import GraphClient
                res = GraphClient(u).send_mail(to, cc, bcc, subject, body)
                mid, conv = res.get("internet_message_id"), res.get("conversation_id")
            else:
                mid, conv = f"<compose-{secrets.token_hex(6)}@relay.local>", f"conv-{secrets.token_hex(4)}"
        except Exception as e:
            log.exception("send failed")
            flash(f"Send failed: {e}", "error")
            return redirect(request.referrer or url_for("compose"))
        now = datetime.now().replace(microsecond=0)
        msg = {"internet_message_id": mid, "conversation_id": conv, "subject": subject, "body": body,
               "from": {"name": u["name"], "email": sender},
               "to": [{"name": "", "email": a} for a in to], "cc": [{"name": "", "email": a} for a in cc],
               "date": now.isoformat(sep=" "),
               "date_utc": now.astimezone(timezone.utc).replace(tzinfo=None).isoformat() + "Z", "headers": {}}
        st = sync.Stats()
        touched = sync.process_message(msg, via="compose", stats=st, mailbox_user_id=u["id"])
        opp_id = f.get("opp_id", type=int)
        if opp_id and mid:
            db.execute("UPDATE activities SET opp_id=? WHERE message_id=?", (opp_id, mid))
        if f.get("followup") and touched:
            due = f.get("followup_date") or (date.today() + timedelta(days=5)).isoformat()
            for cid in touched:
                c = db.query("SELECT * FROM contacts WHERE id=?", (cid,), one=True)
                db.execute("""INSERT INTO tasks(title, contact_id, opp_id, assigned_to, created_by, due_date, priority,
                              auto_rule, conversation_id, notes, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                           (f"Follow up with {display_name(c)} - \"{subject[:60]}\"", cid, opp_id, u["id"], u["id"],
                            due, "Normal", "awaiting_reply", conv,
                            "Created when sending from Relay. Closes automatically on reply.", db.now_iso()))
        msg_txt = f"Email sent to {', '.join(to)} and logged."
        if st.new_contacts:
            msg_txt += f" {st.new_contacts} new contact(s) created."
        if mode != "live":
            msg_txt += " (Demo mode - nothing was actually sent.)"
        flash(msg_txt, "ok")
        if len(touched) == 1:
            return redirect(url_for("contact_detail", cid=touched[0]))
        return redirect(url_for("dashboard"))

    @app.route("/templates", methods=["GET", "POST"])
    def templates_view():
        if request.method == "POST":
            f = request.form
            if f.get("delete"):
                if not auth.is_admin():
                    abort(403)
                db.execute("DELETE FROM templates WHERE id=?", (f.get("id", type=int),))
                audit("delete", "template", f.get("id", type=int), "")
            elif f.get("id"):
                db.execute("UPDATE templates SET name=?, subject=?, body=? WHERE id=?",
                           (f["name"], f["subject"], f["body"], f.get("id", type=int)))
            else:
                db.execute("INSERT INTO templates(name, subject, body) VALUES (?,?,?)",
                           (f["name"], f["subject"], f["body"]))
            flash("Templates updated.", "ok")
            return redirect(url_for("templates_view"))
        return render_template("templates.html", rows=db.query("SELECT * FROM templates ORDER BY name"))

    # ================================================================ reports
    def _report_user():
        v = request.args.get("user", "")
        if v == "me":
            return my_id(), "me"
        return (int(v), v) if v.isdigit() else (None, "")

    @app.route("/reports")
    def reports_home():
        return render_template("reports.html", reports=reports.REPORTS, r=None, periods=reports.PERIODS,
                               members=team_members(), usel="", period="90", org_type="")

    @app.route("/reports/<key>")
    def report_view(key):
        if key not in reports.REPORTS:
            abort(404)
        period = request.args.get("period", "90")
        org_type = request.args.get("type", "")
        uid, usel = _report_user()
        r = reports.run(key, period, org_type, request.args.get("days"), user_id=uid)
        return render_template("reports.html", reports=reports.REPORTS, r=r, periods=reports.PERIODS,
                               period=period, org_type=org_type, key=key, usel=usel, members=team_members())

    @app.route("/reports/<key>.csv")
    def report_csv(key):
        if key not in reports.REPORTS:
            abort(404)
        if not auth.can_export():
            abort(403)
        uid, _ = _report_user()
        r = reports.run(key, request.args.get("period", "90"), request.args.get("type", ""), request.args.get("days"),
                        user_id=uid)
        audit("export", "report", None, f"{key} CSV")
        return csv_response(f"relay-{key}-{date.today().isoformat()}.csv", r["columns"], r["rows"])

    # ================================================================ import
    @app.route("/import", methods=["GET", "POST"])
    def import_view():
        if not auth.can_import():
            abort(403)
        if request.method == "POST" and "file" in request.files:
            up = request.files["file"]
            if not up.filename:
                flash("Choose a file.", "error")
                return redirect(url_for("import_view"))
            ext = os.path.splitext(up.filename.lower())[1]
            if ext not in (".csv", ".txt", ".xlsx", ".xlsm"):
                flash("Please upload a .csv or .xlsx file (save older .xls files as .xlsx first).", "error")
                return redirect(url_for("import_view"))
            tmpdir = os.path.join(DATA_DIR, "imports")
            os.makedirs(tmpdir, exist_ok=True)
            token = secrets.token_hex(8)
            path = os.path.join(tmpdir, token + ext)
            up.save(path)
            try:
                headers, rows = importer.read_file(path, up.filename)
            except Exception as e:
                flash(f"Could not read file: {e}", "error")
                return redirect(url_for("import_view"))
            session["import"] = {"token": token, "ext": ext, "name": up.filename}
            return render_template("import.html", step="map", headers=headers, sample=rows[:5], total=len(rows),
                                   mapping=importer.auto_map(headers), fields=importer.FIELDS, filename=up.filename,
                                   members=team_members())
        return render_template("import.html", step="upload")

    @app.route("/import/run", methods=["POST"])
    def import_run():
        if not auth.can_import():
            abort(403)
        info = session.get("import")
        path = os.path.join(DATA_DIR, "imports", info["token"] + info["ext"]) if info else None
        if not info or not os.path.exists(path):
            flash("Upload expired - please upload the file again.", "error")
            return redirect(url_for("import_view"))
        _, rows = importer.read_file(path, info["name"])
        mapping = {k: request.form.get(f"map_{k}") for k in importer.FIELDS if request.form.get(f"map_{k}")}
        if not (mapping.get("email") or mapping.get("first_name") or mapping.get("full_name")):
            flash("Map at least an Email or Name column.", "error")
            return redirect(url_for("import_view"))
        res = importer.run_import(rows, mapping, update_existing=bool(request.form.get("update")),
                                  default_status=request.form.get("default_status", "Lead"),
                                  tag=request.form.get("tag", "").strip(),
                                  owner_id=form_owner(request.form, default=my_id()), created_by=my_id())
        try:
            os.remove(path)
        except OSError:
            pass
        session.pop("import", None)
        audit("import", "contact", None, f"{info['name']}: {res['created']} created, {res['updated']} updated, "
                                         f"{res['skipped']} skipped")
        return render_template("import.html", step="done", res=res)

    @app.route("/import/sample.csv")
    def import_sample():
        return csv_response("relay-import-template.csv",
                            ["First Name", "Last Name", "Email", "Title", "Phone", "Company", "Organization Type",
                             "Status", "Tags", "Notes"],
                            [["Jane", "Doe", "jane.doe@agency.gov", "Program Manager", "(555) 123-4567",
                              "Example Agency", "Federal", "Prospect", "conference", "Met at industry day"]])

    # ================================================================ sync
    @app.route("/sync/run", methods=["POST"])
    def sync_run():
        everyone = request.args.get("all") == "1" and auth.is_admin()
        if db.get_setting("mode") == "live" and not everyone:
            res = sync.run_sync("manual", user_ids=[my_id()], include_shared=True)
        else:
            res = sync.run_sync("manual")
        if _ajax():
            return jsonify(res)
        flash(res.get("message", ""), "ok" if res.get("status") == "ok" else "error")
        return redirect(request.referrer or url_for("dashboard"))

    @app.route("/sync/log")
    def sync_log():
        if auth.is_admin():
            rows = db.query("SELECT * FROM sync_log ORDER BY id DESC LIMIT 200")
        else:
            rows = db.query("SELECT * FROM sync_log WHERE user_id=? OR user_id IS NULL ORDER BY id DESC LIMIT 100",
                            (my_id(),))
        return render_template("sync_log.html", rows=rows)

    # ================================================================ search
    @app.route("/search")
    def search():
        q = request.args.get("q", "").strip()
        if not q:
            return jsonify([])
        like = f"%{q}%"
        out = []
        for r in db.query("SELECT id, first_name, last_name, email FROM contacts WHERE first_name || ' ' || last_name "
                          "LIKE ? OR email LIKE ? LIMIT 6", (like, like)):
            out.append({"type": "Contact", "label": display_name(r), "sub": r["email"] or "",
                        "url": url_for("contact_detail", cid=r["id"])})
        for r in db.query("SELECT id, name, domain FROM organizations WHERE name LIKE ? OR domain LIKE ? LIMIT 4",
                          (like, like)):
            out.append({"type": "Organisation", "label": r["name"], "sub": r["domain"] or "",
                        "url": url_for("org_detail", oid=r["id"])})
        for r in db.query("SELECT id, name, stage FROM opportunities WHERE name LIKE ? OR solicitation_no LIKE ? LIMIT 4",
                          (like, like)):
            out.append({"type": "Opportunity", "label": r["name"], "sub": r["stage"],
                        "url": url_for("opp_detail", oid=r["id"])})
        return jsonify(out)
