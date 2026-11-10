"""Reports. Each returns {title, description, columns, rows, chart, totals}."""
from datetime import date, datetime, timedelta, timezone

STAGES = ["Lead", "Qualified", "Proposal", "Submitted", "Awarded", "Lost"]
OPEN = STAGES[:4]
ORG_TYPES = ["Federal", "State", "Local", "Education", "Commercial", "Nonprofit", "Individual", "Other"]

CATALOG = [
    ("pipeline", "Pipeline by stage", "Open and closed opportunities, count and value by stage."),
    ("forecast", "Forecast", "Weighted value (value x probability) of open opportunities by expected close month."),
    ("winloss", "Win / loss", "Awarded vs. lost opportunities closed in the period, by organization type."),
    ("activity", "Activity summary", "Emails, calls, meetings and notes logged per week."),
    ("engagement", "Contact engagement", "Who you corresponded with most in the period."),
    ("stale", "Stale contacts", "Contacts you haven't been in touch with for 60+ days."),
    ("newcontacts", "New contacts", "Contacts added in the period and where they came from."),
    ("orgs", "Organization summary", "Contacts, open pipeline and last touch per organization."),
    ("tasks", "Tasks & follow-ups", "Open, overdue and completed tasks."),
]


def money(v):
    return f"${(v or 0):,.0f}"


def _range(start, end):
    today = date.today()
    s = start or (today - timedelta(days=90)).isoformat()
    e = end or today.isoformat()
    return s, e, e + "T23:59:59+00:00"


def _type_clause(org_type, alias="g"):
    if org_type:
        return f" AND {alias}.org_type = ? ", [org_type]
    return "", []


def run(db, key, start=None, end=None, org_type=None):
    s, e, e_end = _range(start, end)
    tc, ta = _type_clause(org_type)
    out = {"key": key, "start": s, "end": e, "org_type": org_type or "", "chart": None, "totals": None}

    if key == "pipeline":
        rows = db.q(f"""SELECT o.stage, count(*) n, sum(o.value) v, sum(o.value*o.probability/100.0) w
                        FROM opportunities o LEFT JOIN orgs g ON g.id=o.org_id WHERE 1=1 {tc} GROUP BY o.stage""", ta)
        by = {r["stage"]: r for r in rows}
        data = [[st, by[st]["n"] if st in by else 0, money(by[st]["v"] if st in by else 0),
                 money(by[st]["w"] if st in by else 0)] for st in STAGES]
        out.update(columns=["Stage", "Opportunities", "Total value", "Weighted value"], rows=data,
                   chart=[(st, (by[st]["v"] or 0) if st in by else 0, money(by[st]["v"]) if st in by else "$0") for st in STAGES])
        tot_open = sum((by[st]["v"] or 0) for st in OPEN if st in by)
        tot_w = sum((by[st]["w"] or 0) for st in OPEN if st in by)
        out["totals"] = [("Open pipeline", money(tot_open)), ("Weighted pipeline", money(tot_w))]

    elif key == "forecast":
        rows = db.q(f"""SELECT substr(o.close_date,1,7) m, count(*) n, sum(o.value) v, sum(o.value*o.probability/100.0) w
                        FROM opportunities o LEFT JOIN orgs g ON g.id=o.org_id
                        WHERE o.stage IN ('Lead','Qualified','Proposal','Submitted') AND o.close_date IS NOT NULL {tc}
                        GROUP BY m ORDER BY m""", ta)
        out.update(columns=["Close month", "Opportunities", "Total value", "Weighted forecast"],
                   rows=[[r["m"], r["n"], money(r["v"]), money(r["w"])] for r in rows],
                   chart=[(r["m"], r["w"] or 0, money(r["w"])) for r in rows])
        out["totals"] = [("Weighted forecast", money(sum(r["w"] or 0 for r in rows)))]

    elif key == "winloss":
        rows = db.q(f"""SELECT coalesce(g.org_type,'Other') t,
                          sum(o.stage='Awarded') won, sum(o.stage='Lost') lost,
                          sum(CASE WHEN o.stage='Awarded' THEN o.value ELSE 0 END) wv,
                          sum(CASE WHEN o.stage='Lost' THEN o.value ELSE 0 END) lv
                        FROM opportunities o LEFT JOIN orgs g ON g.id=o.org_id
                        WHERE o.stage IN ('Awarded','Lost') AND coalesce(o.closed_at, o.updated_at) BETWEEN ? AND ? {tc}
                        GROUP BY t ORDER BY wv DESC""", [s, e_end] + ta)
        data = []
        for r in rows:
            n = (r["won"] or 0) + (r["lost"] or 0)
            data.append([r["t"], r["won"], r["lost"], f"{(100 * (r['won'] or 0) / n):.0f}%" if n else "-",
                         money(r["wv"]), money(r["lv"])])
        won = sum(r["won"] or 0 for r in rows)
        lost = sum(r["lost"] or 0 for r in rows)
        out.update(columns=["Org type", "Won", "Lost", "Win rate", "Value won", "Value lost"], rows=data,
                   chart=[(r["t"], r["wv"] or 0, money(r["wv"])) for r in rows])
        out["totals"] = [("Win rate", f"{100 * won / (won + lost):.0f}%" if won + lost else "-"),
                         ("Value won", money(sum(r["wv"] or 0 for r in rows)))]

    elif key == "activity":
        rows = db.q(f"""SELECT strftime('%Y-W%W', a.occurred_at) wk,
                          sum(a.kind='email_out') eo, sum(a.kind='email_in') ei, sum(a.kind='call') c,
                          sum(a.kind='meeting') m, sum(a.kind='note') n, count(*) total
                        FROM activities a JOIN contacts ct ON ct.id=a.contact_id LEFT JOIN orgs g ON g.id=ct.org_id
                        WHERE a.occurred_at BETWEEN ? AND ? {tc} GROUP BY wk ORDER BY wk""", [s, e_end] + ta)
        out.update(columns=["Week", "Emails sent", "Emails received", "Calls", "Meetings", "Notes", "Total"],
                   rows=[[r["wk"], r["eo"], r["ei"], r["c"], r["m"], r["n"], r["total"]] for r in rows],
                   chart=[(r["wk"], r["total"], str(r["total"])) for r in rows])
        out["totals"] = [("Total activities", str(sum(r["total"] for r in rows)))]

    elif key == "engagement":
        rows = db.q(f"""SELECT ct.id, ct.first_name || ' ' || ct.last_name nm, ct.email, g.name org,
                          sum(a.kind='email_out') eo, sum(a.kind='email_in') ei,
                          sum(a.kind IN ('call','meeting')) cm, count(*) total, max(a.occurred_at) last
                        FROM activities a JOIN contacts ct ON ct.id=a.contact_id LEFT JOIN orgs g ON g.id=ct.org_id
                        WHERE a.occurred_at BETWEEN ? AND ? {tc}
                        GROUP BY ct.id ORDER BY total DESC LIMIT 50""", [s, e_end] + ta)
        out.update(columns=["Contact", "Organization", "Sent", "Received", "Calls/meetings", "Total", "Last touch"],
                   rows=[[r["nm"].strip() or r["email"], r["org"] or "", r["eo"], r["ei"], r["cm"], r["total"],
                          (r["last"] or "")[:10]] for r in rows],
                   chart=[((r["nm"].strip() or r["email"]), r["total"], str(r["total"])) for r in rows[:12]],
                   links=[f"/contacts/{r['id']}" for r in rows])

    elif key == "stale":
        cutoff = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        rows = db.q(f"""SELECT ct.id, ct.first_name || ' ' || ct.last_name nm, ct.email, ct.lifecycle, g.name org,
                          ct.last_contacted, (SELECT count(*) FROM opportunities o WHERE o.contact_id=ct.id
                            AND o.stage IN ('Lead','Qualified','Proposal','Submitted')) open_opps
                        FROM contacts ct LEFT JOIN orgs g ON g.id=ct.org_id
                        WHERE (ct.last_contacted IS NULL OR ct.last_contacted < ?) AND ct.lifecycle != 'Inactive' {tc}
                        ORDER BY open_opps DESC, ct.last_contacted""", [cutoff] + ta)
        out.update(columns=["Contact", "Organization", "Status", "Open opps", "Last contacted"],
                   rows=[[r["nm"].strip() or r["email"], r["org"] or "", r["lifecycle"], r["open_opps"],
                          (r["last_contacted"] or "never")[:10]] for r in rows],
                   links=[f"/contacts/{r['id']}" for r in rows])
        out["totals"] = [("Stale contacts", str(len(rows)))]

    elif key == "newcontacts":
        rows = db.q(f"""SELECT ct.id, ct.first_name || ' ' || ct.last_name nm, ct.email, g.name org, g.org_type,
                          ct.source, ct.needs_review, ct.created_at
                        FROM contacts ct LEFT JOIN orgs g ON g.id=ct.org_id
                        WHERE ct.created_at BETWEEN ? AND ? {tc} ORDER BY ct.created_at DESC""", [s, e_end] + ta)
        src = {}
        for r in rows:
            src[r["source"]] = src.get(r["source"], 0) + 1
        label = {"email-out": "From emails you sent", "email-in": "From emails you received", "import": "Imported",
                 "manual": "Added by hand", "demo": "Demo data"}
        out.update(columns=["Contact", "Email", "Organization", "Type", "Source", "Added"],
                   rows=[[r["nm"].strip() or r["email"], r["email"] or "", r["org"] or "", r["org_type"] or "",
                          label.get(r["source"], r["source"]) + (" (review)" if r["needs_review"] else ""),
                          (r["created_at"] or "")[:10]] for r in rows],
                   chart=[(label.get(k, k), v, str(v)) for k, v in sorted(src.items(), key=lambda x: -x[1])],
                   links=[f"/contacts/{r['id']}" for r in rows])
        out["totals"] = [("New contacts", str(len(rows)))]

    elif key == "orgs":
        rows = db.q(f"""SELECT g.id, g.name, g.org_type,
                          (SELECT count(*) FROM contacts c WHERE c.org_id=g.id) nc,
                          (SELECT count(*) FROM opportunities o WHERE o.org_id=g.id AND o.stage IN ('Lead','Qualified','Proposal','Submitted')) no,
                          (SELECT coalesce(sum(value),0) FROM opportunities o WHERE o.org_id=g.id AND o.stage IN ('Lead','Qualified','Proposal','Submitted')) ov,
                          (SELECT coalesce(sum(value),0) FROM opportunities o WHERE o.org_id=g.id AND o.stage='Awarded') aw,
                          (SELECT max(last_contacted) FROM contacts c WHERE c.org_id=g.id) lt
                        FROM orgs g WHERE 1=1 {tc} ORDER BY ov DESC, g.name""", ta)
        out.update(columns=["Organization", "Type", "Contacts", "Open opps", "Open value", "Awarded value", "Last touch"],
                   rows=[[r["name"], r["org_type"], r["nc"], r["no"], money(r["ov"]), money(r["aw"]), (r["lt"] or "")[:10]]
                         for r in rows],
                   chart=[(r["name"], r["ov"], money(r["ov"])) for r in rows if r["ov"]][:12],
                   links=[f"/orgs/{r['id']}" for r in rows])

    elif key == "tasks":
        today = date.today().isoformat()
        rows = db.q(f"""SELECT t.title, t.due_date, t.kind, t.priority, t.done, t.completed_at,
                          ct.id cid, ct.first_name || ' ' || ct.last_name nm
                        FROM tasks t LEFT JOIN contacts ct ON ct.id=t.contact_id LEFT JOIN orgs g ON g.id=ct.org_id
                        WHERE (t.done=0 OR t.completed_at BETWEEN ? AND ?) {tc}
                        ORDER BY t.done, t.due_date""", [s, e_end] + ta)
        kinds = {"manual": "Task", "awaiting_reply": "Awaiting reply (auto)", "reply_needed": "Reply needed (auto)"}

        def status(r):
            if r["done"]:
                return "Done"
            return "Overdue" if (r["due_date"] or "9999") < today else "Open"
        data = [[r["title"], (r["nm"] or "").strip(), kinds.get(r["kind"], r["kind"]), r["priority"],
                 r["due_date"] or "", status(r)] for r in rows]
        counts = {"Open": 0, "Overdue": 0, "Done": 0}
        for d in data:
            counts[d[-1]] += 1
        out.update(columns=["Task", "Contact", "Type", "Priority", "Due", "Status"], rows=data,
                   chart=[(k, v, str(v)) for k, v in counts.items()],
                   links=[f"/contacts/{r['cid']}" if r["cid"] else "" for r in rows])
        out["totals"] = [(k, str(v)) for k, v in counts.items()]
    else:
        raise KeyError(key)

    meta = {k: (t, d) for k, t, d in CATALOG}[key]
    out["title"], out["description"] = meta
    return out
