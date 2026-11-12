"""Report library. Each report returns a dict with columns/rows (for tables and
CSV export) plus optional chart data and summary KPIs."""
from datetime import date, datetime, timedelta

from . import db

REPORTS = {
    "pipeline": ("Pipeline summary", "Open value, weighted forecast and count by stage."),
    "forecast": ("Forecast by close month", "Weighted and unweighted open pipeline by expected close month."),
    "win_loss": ("Win / loss analysis", "Awarded vs lost, win rate and value, by organisation type."),
    "activity": ("Correspondence & activity", "Emails sent/received, calls, meetings and notes per week."),
    "contact_engagement": ("Contact engagement", "Who you talk to most, two-way ratio and last touch."),
    "stale": ("Stale relationships", "Active contacts with no correspondence in N days."),
    "new_contacts": ("New contacts by source", "Contacts added in the period and where they came from."),
    "org_summary": ("Organisation / agency summary", "Contacts, activity and pipeline per organisation."),
    "tasks": ("Task & follow-up report", "Open, overdue and completed follow-ups, auto vs manual."),
    "team": ("Team activity by person", "Emails, calls, meetings, new contacts, pipeline and tasks per team member."),
}
# reports that make sense filtered to one person
USER_FILTERABLE = set(REPORTS) - {"team"}

PERIODS = {"30": "Last 30 days", "90": "Last 90 days", "180": "Last 6 months", "365": "Last 12 months",
           "ytd": "Year to date", "all": "All time"}


def period_start(period):
    if period == "all":
        return "0000-01-01"
    if period == "ytd":
        return date(date.today().year, 1, 1).isoformat()
    try:
        days = int(period)
    except (TypeError, ValueError):
        days = 90
    return (date.today() - timedelta(days=days)).isoformat()


def money(v):
    return f"${(v or 0):,.0f}"


def run(key, period="90", org_type="", stale_days=None, user_id=None):
    """Run a report. ``user_id`` limits it to one team member's records:
    opportunities/contacts/organisations they own, activity they took part in, tasks assigned to them."""
    since = period_start(period)

    def us(col):
        return (f" AND {col} = ?", (user_id,)) if user_id else ("", ())
    won, lost = db.closed_stages()
    stages = db.setting_list("stages")
    type_sql, type_args = ("", ())
    if org_type:
        type_sql, type_args = (" AND o.org_type = ?", (org_type,))

    if key == "pipeline":
        rows = db.query(f"""SELECT p.stage, COUNT(*) n, SUM(p.value) v, SUM(p.value * p.probability / 100.0) w
                            FROM opportunities p LEFT JOIN organizations o ON o.id=p.org_id
                            WHERE 1=1 {type_sql} {us("p.owner_id")[0]} GROUP BY p.stage""",
                        (*type_args, *us("p.owner_id")[1]))
        by = {r["stage"]: r for r in rows}
        out = []
        for s in stages:
            r = by.get(s)
            out.append([s, r["n"] if r else 0, money(r["v"] if r else 0), money(r["w"] if r else 0)])
        open_rows = [by[s] for s in stages if s in by and s not in (won, lost)]
        kpis = [("Open opportunities", sum(r["n"] for r in open_rows)),
                ("Open pipeline", money(sum(r["v"] or 0 for r in open_rows))),
                ("Weighted forecast", money(sum(r["w"] or 0 for r in open_rows))),
                ("Awarded (all time)", money(by[won]["v"] if won in by else 0))]
        chart = {"type": "bar", "labels": stages,
                 "series": [{"name": "Value", "data": [float(by[s]["v"] or 0) if s in by else 0 for s in stages]}],
                 "money": True}
        return _r(key, ["Stage", "Count", "Total value", "Weighted value"], out, kpis, chart)

    if key == "forecast":
        rows = db.query(f"""SELECT substr(p.close_date,1,7) m, COUNT(*) n, SUM(p.value) v,
                            SUM(p.value * p.probability / 100.0) w
                            FROM opportunities p LEFT JOIN organizations o ON o.id=p.org_id
                            WHERE p.stage NOT IN (?,?) AND p.close_date IS NOT NULL {type_sql} {us("p.owner_id")[0]}
                            GROUP BY m ORDER BY m""", (won, lost, *type_args, *us("p.owner_id")[1]))
        out = [[r["m"], r["n"], money(r["v"]), money(r["w"])] for r in rows]
        chart = {"type": "bar", "labels": [r["m"] for r in rows], "money": True,
                 "series": [{"name": "Weighted", "data": [float(r["w"] or 0) for r in rows]},
                            {"name": "Total", "data": [float(r["v"] or 0) for r in rows]}]}
        kpis = [("Months with expected awards", len(rows)),
                ("Weighted total", money(sum(r["w"] or 0 for r in rows)))]
        return _r(key, ["Close month", "Count", "Total value", "Weighted value"], out, kpis, chart)

    if key == "win_loss":
        rows = db.query(f"""SELECT COALESCE(NULLIF(o.org_type,''),'Unspecified') t,
                            SUM(p.stage=?) w, SUM(p.stage=?) l,
                            SUM(CASE WHEN p.stage=? THEN p.value ELSE 0 END) wv,
                            SUM(CASE WHEN p.stage=? THEN p.value ELSE 0 END) lv
                            FROM opportunities p LEFT JOIN organizations o ON o.id=p.org_id
                            WHERE p.stage IN (?,?) AND COALESCE(p.closed_at, p.updated_at) >= ? {type_sql}
                            {us("p.owner_id")[0]} GROUP BY t ORDER BY wv DESC""",
                        (won, lost, won, lost, won, lost, since, *type_args, *us("p.owner_id")[1]))
        out = []
        tw = tl = twv = 0
        for r in rows:
            total = (r["w"] or 0) + (r["l"] or 0)
            rate = f"{(r['w'] or 0) / total * 100:.0f}%" if total else "-"
            out.append([r["t"], r["w"], r["l"], rate, money(r["wv"]), money(r["lv"])])
            tw += r["w"] or 0; tl += r["l"] or 0; twv += r["wv"] or 0
        kpis = [("Awarded", tw), ("Lost", tl),
                ("Win rate", f"{tw / (tw + tl) * 100:.0f}%" if (tw + tl) else "-"), ("Awarded value", money(twv))]
        chart = {"type": "bar", "labels": [r[0] for r in out],
                 "series": [{"name": "Awarded", "data": [r[1] for r in out]},
                            {"name": "Lost", "data": [r[2] for r in out]}]}
        return _r(key, ["Org type", "Awarded", "Lost", "Win rate", "Awarded value", "Lost value"], out, kpis, chart)

    if key == "activity":
        rows = db.query(f"""SELECT strftime('%Y-W%W', a.occurred_at) wk,
                            SUM(a.kind='email_out') eo, SUM(a.kind='email_in') ei, SUM(a.kind='call') c,
                            SUM(a.kind='meeting') m, SUM(a.kind='note') n
                            FROM activities a LEFT JOIN organizations o ON o.id=a.org_id
                            WHERE a.occurred_at >= ? AND a.kind != 'system' {type_sql} {us("a.user_id")[0]}
                            GROUP BY wk ORDER BY wk""", (since, *type_args, *us("a.user_id")[1]))
        out = [[r["wk"], r["eo"], r["ei"], r["c"], r["m"], r["n"]] for r in rows]
        kpis = [("Emails sent", sum(r[1] for r in out)), ("Emails received", sum(r[2] for r in out)),
                ("Calls", sum(r[3] for r in out)), ("Meetings", sum(r[4] for r in out))]
        chart = {"type": "bar", "labels": [r[0] for r in out], "stacked": True,
                 "series": [{"name": "Sent", "data": [r[1] for r in out]},
                            {"name": "Received", "data": [r[2] for r in out]},
                            {"name": "Calls", "data": [r[3] for r in out]},
                            {"name": "Meetings", "data": [r[4] for r in out]}]}
        return _r(key, ["Week", "Emails sent", "Emails received", "Calls", "Meetings", "Notes"], out, kpis, chart)

    if key == "contact_engagement":
        rows = db.query(f"""SELECT c.id, c.first_name || ' ' || c.last_name nm, c.email, o.name org, c.status,
                            SUM(a.kind='email_out') eo, SUM(a.kind='email_in') ei,
                            SUM(a.kind IN ('call','meeting')) cm, MAX(a.occurred_at) last
                            FROM contacts c LEFT JOIN organizations o ON o.id=c.org_id
                            JOIN activities a ON a.contact_id=c.id AND a.occurred_at >= ? AND a.kind != 'system'
                                 {us("a.user_id")[0]}
                            WHERE 1=1 {type_sql}
                            GROUP BY c.id ORDER BY (eo+ei+cm) DESC LIMIT 200""",
                        (since, *us("a.user_id")[1], *type_args))
        out = []
        for r in rows:
            two_way = "Yes" if (r["eo"] and r["ei"]) else ("Outbound only" if r["eo"] else "Inbound only")
            out.append([r["nm"].strip() or r["email"], r["org"] or "", r["status"], r["eo"], r["ei"], r["cm"],
                        two_way, (r["last"] or "")[:10]])
        kpis = [("Contacts engaged", len(out)),
                ("Two-way conversations", sum(1 for r in out if r[6] == "Yes")),
                ("Awaiting their reply", sum(1 for r in out if r[6] == "Outbound only"))]
        return _r(key, ["Contact", "Organisation", "Status", "Sent", "Received", "Calls/Mtgs", "Two-way",
                        "Last touch"], out, kpis, None, links=[("contact", r["id"]) for r in rows])

    if key == "stale":
        n = int(stale_days or db.get_setting("stale_days") or 45)
        cutoff = (datetime.now() - timedelta(days=n)).isoformat(sep=" ")
        rows = db.query(f"""SELECT c.id, c.first_name || ' ' || c.last_name nm, c.email, o.name org, c.status,
                            c.last_contacted FROM contacts c LEFT JOIN organizations o ON o.id=c.org_id
                            WHERE c.status IN ('Client','Prospect','Partner')
                            AND (c.last_contacted IS NULL OR c.last_contacted < ?) {type_sql} {us("c.owner_id")[0]}
                            ORDER BY c.last_contacted""", (cutoff, *type_args, *us("c.owner_id")[1]))
        out = []
        for r in rows:
            days = (datetime.now() - datetime.fromisoformat(r["last_contacted"])).days if r["last_contacted"] else None
            out.append([r["nm"].strip() or r["email"], r["org"] or "", r["status"],
                        (r["last_contacted"] or "Never")[:10], days if days is not None else "-"])
        return _r(key, ["Contact", "Organisation", "Status", "Last contacted", "Days silent"], out,
                  [("Stale relationships", len(out)), ("Threshold (days)", n)], None,
                  links=[("contact", r["id"]) for r in rows])

    if key == "new_contacts":
        rows = db.query(f"""SELECT c.source, COUNT(*) n, SUM(c.needs_review) rv FROM contacts c
                            LEFT JOIN organizations o ON o.id=c.org_id
                            WHERE c.created_at >= ? {type_sql} {us("c.owner_id")[0]} GROUP BY c.source ORDER BY n DESC""",
                        (since, *type_args, *us("c.owner_id")[1]))
        out = [[r["source"], r["n"], r["rv"]] for r in rows]
        auto = sum(r[1] for r in out if str(r[0]).startswith("Email"))
        kpis = [("New contacts", sum(r[1] for r in out)), ("Auto-created from email", auto),
                ("Awaiting review", sum(r[2] or 0 for r in out))]
        chart = {"type": "doughnut", "labels": [r[0] for r in out], "series": [{"name": "Contacts", "data": [r[1] for r in out]}]}
        return _r(key, ["Source", "Contacts", "Needs review"], out, kpis, chart)

    if key == "org_summary":
        org_user_sql, org_user_args = "", ()
        if user_id:
            org_user_sql = (" AND (o.owner_id = ? OR EXISTS (SELECT 1 FROM opportunities p2 WHERE p2.org_id=o.id AND "
                            "p2.owner_id = ?) OR EXISTS (SELECT 1 FROM contacts c2 WHERE c2.org_id=o.id AND "
                            "c2.owner_id = ?))")
            org_user_args = (user_id, user_id, user_id)
        rows = db.query(f"""SELECT o.id, o.name, o.org_type,
                            (SELECT COUNT(*) FROM contacts c WHERE c.org_id=o.id) nc,
                            (SELECT COUNT(*) FROM activities a WHERE a.org_id=o.id AND a.occurred_at >= ? AND a.kind!='system') na,
                            (SELECT COUNT(*) FROM opportunities p WHERE p.org_id=o.id AND p.stage NOT IN (?,?)) no,
                            (SELECT COALESCE(SUM(value),0) FROM opportunities p WHERE p.org_id=o.id AND p.stage NOT IN (?,?)) ov,
                            (SELECT COALESCE(SUM(value),0) FROM opportunities p WHERE p.org_id=o.id AND p.stage=?) wv,
                            (SELECT MAX(occurred_at) FROM activities a WHERE a.org_id=o.id AND a.kind!='system') last
                            FROM organizations o WHERE 1=1 {type_sql} {org_user_sql} ORDER BY ov DESC, na DESC""",
                        (since, won, lost, won, lost, won, *type_args, *org_user_args))
        out = [[r["name"], r["org_type"] or "", r["nc"], r["na"], r["no"], money(r["ov"]), money(r["wv"]),
                (r["last"] or "")[:10]] for r in rows]
        return _r(key, ["Organisation", "Type", "Contacts", "Activities", "Open opps", "Open value",
                        "Awarded value", "Last activity"], out, [("Organisations", len(out))], None,
                  links=[("org", r["id"]) for r in rows])

    if key == "tasks":
        today = date.today().isoformat()
        rows = db.query(f"""SELECT t.*, c.first_name || ' ' || c.last_name nm, u.name assignee FROM tasks t
                           LEFT JOIN contacts c ON c.id=t.contact_id LEFT JOIN users u ON u.id=t.assigned_to
                           WHERE (t.status='Open' OR t.completed_at >= ?) {us("t.assigned_to")[0]}
                           ORDER BY t.status DESC, t.due_date""",
                        (since, *us("t.assigned_to")[1]))
        out = []
        for r in rows:
            state = r["status"]
            if state == "Open" and r["due_date"] and r["due_date"] < today:
                state = "Overdue"
            out.append([r["title"], (r["nm"] or "").strip(), r["assignee"] or "Unassigned", r["due_date"] or "",
                        r["priority"], state,
                        {"awaiting_reply": "Auto: awaiting reply", "reply_needed": "Auto: reply needed"}.get(r["auto_rule"], "Manual"),
                        (r["completed_at"] or "")[:10]])
        kpis = [("Open", sum(1 for r in out if r[5] in ("Open", "Overdue"))),
                ("Overdue", sum(1 for r in out if r[5] == "Overdue")),
                ("Completed in period", sum(1 for r in out if r[5] == "Done")),
                ("Auto-closed by email", sum(1 for r in rows if r["status"] == "Done" and "Auto-closed" in (r["notes"] or "")))]
        return _r(key, ["Task", "Contact", "Assigned to", "Due", "Priority", "State", "Type", "Completed"],
                  out, kpis, None)

    if key == "team":
        today = date.today().isoformat()
        sql_u = "SELECT * FROM users WHERE active=1 AND role<>'viewer'"
        members = db.query(sql_u + (" AND id=?" if user_id else "") + " ORDER BY name COLLATE NOCASE",
                           (user_id,) if user_id else ())
        out = []
        for u in members:
            a = db.query(f"""SELECT SUM(a.kind='email_out') eo, SUM(a.kind='email_in') ei, SUM(a.kind='call') c,
                             SUM(a.kind='meeting') m, COUNT(DISTINCT a.contact_id) people
                             FROM activities a LEFT JOIN organizations o ON o.id=a.org_id
                             WHERE a.user_id=? AND a.occurred_at >= ? AND a.kind!='system' {type_sql}""",
                         (u["id"], since, *type_args), one=True)
            nc = db.query("SELECT COUNT(*) n FROM contacts WHERE owner_id=? AND created_at >= ?",
                          (u["id"], since), one=True)["n"]
            p = db.query(f"""SELECT COALESCE(SUM(CASE WHEN p.stage NOT IN (?,?) THEN p.value END),0) ov,
                             COALESCE(SUM(CASE WHEN p.stage NOT IN (?,?) THEN p.value*p.probability/100.0 END),0) w,
                             COALESCE(SUM(CASE WHEN p.stage=? AND COALESCE(p.closed_at,p.updated_at)>=? THEN p.value END),0) wv
                             FROM opportunities p LEFT JOIN organizations o ON o.id=p.org_id
                             WHERE p.owner_id=? {type_sql}""",
                         (won, lost, won, lost, won, since, u["id"], *type_args), one=True)
            t = db.query("SELECT SUM(status='Open') op, SUM(status='Open' AND due_date < ?) od, "
                         "SUM(status='Done' AND completed_at >= ?) dn FROM tasks WHERE assigned_to=?",
                         (today, since, u["id"]), one=True)
            out.append([u["name"] or u["email"], a["eo"] or 0, a["ei"] or 0, a["c"] or 0, a["m"] or 0,
                        a["people"] or 0, nc, money(p["ov"]), money(p["w"]), money(p["wv"]), t["op"] or 0,
                        t["od"] or 0, t["dn"] or 0])
        kpis = [("Team members", len(out)), ("Emails sent", sum(r[1] for r in out)),
                ("Calls & meetings", sum(r[3] + r[4] for r in out)), ("Overdue tasks", sum(r[11] for r in out))]
        chart = {"type": "bar", "labels": [r[0] for r in out], "stacked": True,
                 "series": [{"name": "Emails sent", "data": [r[1] for r in out]},
                            {"name": "Emails received", "data": [r[2] for r in out]},
                            {"name": "Calls", "data": [r[3] for r in out]},
                            {"name": "Meetings", "data": [r[4] for r in out]}]}
        return _r(key, ["Team member", "Emails sent", "Emails received", "Calls", "Meetings", "People engaged",
                        "New contacts", "Open pipeline", "Weighted", "Awarded in period", "Open tasks", "Overdue",
                        "Tasks done"], out, kpis, chart, links=[("user", u["id"]) for u in members])

    raise KeyError(key)


def _r(key, columns, rows, kpis, chart, links=None):
    title, desc = REPORTS[key]
    return {"key": key, "title": title, "description": desc, "columns": columns, "rows": rows,
            "kpis": kpis or [], "chart": chart, "links": links}
