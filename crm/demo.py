"""Demo mode: realistic public-sector sample data for a small team, plus a
simulated mailbox for the signed-in administrator, so every feature (auto
sync, BCC capture, forward capture, new-contact creation, follow-up tasks,
ownership, team reports) can be tried without connecting Microsoft 365.

Two sample colleagues (Jordan Lee, Sam Patel) are created as *demo users*.
They cannot sign in and are removed when you switch to live mode.
"""
import json
import os
import random
from datetime import datetime, timedelta, timezone

from . import db
from .paths import DATA_DIR

DEMO_DOMAIN = "yourfirm-consulting.com"
OUTBOX = os.path.join(DATA_DIR, "demo_outbox.json")
TEAMMATES = [("Jordan Lee", f"jordan.lee@{DEMO_DOMAIN}", "Capture Manager"),
             ("Sam Patel", f"sam.patel@{DEMO_DOMAIN}", "Business Development Director")]


def _dt(days_ago, hour=10, minute=0):
    d = datetime.now() - timedelta(days=days_ago)
    return d.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _utc(local_dt):
    return local_dt.astimezone(timezone.utc).replace(tzinfo=None).isoformat() + "Z"


def demo_owner_id():
    r = db.query("SELECT id FROM users WHERE is_demo=0 AND active=1 ORDER BY role='admin' DESC, id LIMIT 1", one=True)
    return r["id"] if r else None


def _me():
    u = db.user(demo_owner_id())
    if not u:
        return {"name": "You", "email": f"you@{DEMO_DOMAIN}"}
    return {"name": u["name"] or "You", "email": (u["email"] or "").lower()}


def _teammate_ids():
    ids = []
    for name, email, title in TEAMMATES:
        u = db.user_by_email(email)
        if not u:
            uid = db.execute("INSERT INTO users(email, name, role, title, is_demo, active, created_at) "
                             "VALUES (?,?,?,?,1,1,?)", (email, name, "user", title, db.now_iso()))
        else:
            uid = u["id"]
        ids.append(uid)
    return ids


def seed():
    """Load sample organisations, contacts, opportunities and history."""
    owner = demo_owner_id()
    if owner is None:
        return False
    if db.query("SELECT COUNT(*) n FROM contacts", one=True)["n"] > 0:
        return False
    me_u = db.user(owner)
    jordan, sam = _teammate_ids()
    team = [owner, jordan, sam]
    doms = set(db.setting_list("internal_domains")) | {DEMO_DOMAIN}
    db.set_setting("internal_domains", ",".join(sorted(doms)))
    if not db.get_setting("firm_name"):
        db.set_setting("firm_name", "Your Firm Consulting")
    now = db.now_iso()

    orgs = [
        ("Dept. of Homeland Security - CISA", "cisa.dhs.gov", "Federal", "Cyber modernisation programme office", owner),
        ("General Services Administration", "gsa.gov", "Federal", "OASIS+ / MAS vehicle holder relationship", sam),
        ("State of Texas - DIR", "dir.texas.gov", "State", "Statewide IT procurement", jordan),
        ("City of Austin", "austintexas.gov", "Local", "Smart City office", jordan),
        ("U.S. Army PEO EIS", "army.mil", "Military / DoD", "", owner),
        ("Virginia Dept. of Transportation", "vdot.virginia.gov", "State", "", sam),
        ("Brightline Analytics (teaming partner)", "brightline-analytics.com", "Partner / Teaming",
         "Small business teaming partner - data & analytics", owner),
    ]
    org_ids = {}
    for name, dom, typ, notes, own in orgs:
        org_ids[dom] = db.execute("INSERT INTO organizations(name, domain, org_type, notes, owner_id, created_at) "
                                  "VALUES (?,?,?,?,?,?)", (name, dom, typ, notes, own, now))

    contacts = [
        ("Maria", "Delgado", "maria.delgado@cisa.dhs.gov", "Branch Chief, Cyber Modernization", "cisa.dhs.gov", "Client", "Referral", "decision-maker,cyber", owner),
        ("James", "Whitfield", "james.whitfield@cisa.dhs.gov", "Contracting Officer", "cisa.dhs.gov", "Client", "Referral", "contracting", owner),
        ("Priya", "Raman", "priya.raman@gsa.gov", "Program Manager, TTS", "gsa.gov", "Prospect", "Conference", "digital", sam),
        ("Daniel", "Okafor", "daniel.okafor@dir.texas.gov", "Deputy CIO", "dir.texas.gov", "Prospect", "Industry Day", "decision-maker", jordan),
        ("Lauren", "Price", "lauren.price@austintexas.gov", "Smart City Program Lead", "austintexas.gov", "Lead", "LinkedIn", "", jordan),
        ("Col. Robert", "Haynes", "robert.haynes@army.mil", "Product Lead", "army.mil", "Prospect", "Industry Day", "dod", owner),
        ("Sofia", "Nguyen", "sofia.nguyen@vdot.virginia.gov", "Asset Management Director", "vdot.virginia.gov", "Lead", "RFI response", "", sam),
        ("Marcus", "Bell", "marcus.bell@brightline-analytics.com", "CEO", "brightline-analytics.com", "Partner", "Manual", "teaming", owner),
    ]
    cids = {}
    for f, l, e, t, dom, st, src, tags, own in contacts:
        cids[e] = db.execute(
            """INSERT INTO contacts(first_name,last_name,email,title,phone,org_id,status,source,tags,owner_id,created_by,
                                    created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f, l, e, t, f"(555) {random.randint(200, 899)}-{random.randint(1000, 9999)}",
             org_ids[dom], st, src, tags, own, own, now, now))

    won, lost = db.closed_stages()
    probs = db.stage_probabilities()
    opps = [
        ("CISA Zero Trust Architecture Support", "cisa.dhs.gov", "maria.delgado@cisa.dhs.gov", "Proposal", 2400000, 45, "70QS0125R0012", "GSA MAS", owner),
        ("CISA CDM Dashboard O&M - Option Yr 2", "cisa.dhs.gov", "james.whitfield@cisa.dhs.gov", "Awarded", 850000, -40, "70QS0124F0331", "CIO-SP3", owner),
        ("GSA TTS Product Coaching BPA", "gsa.gov", "priya.raman@gsa.gov", "Qualified", 1200000, 60, "", "OASIS+", sam),
        ("Texas DIR Data Modernization", "dir.texas.gov", "daniel.okafor@dir.texas.gov", "Submitted", 640000, 20, "DIR-CPO-TMP-512", "DIR Cooperative", jordan),
        ("Austin Smart Mobility Pilot", "austintexas.gov", "lauren.price@austintexas.gov", "Lead", 180000, 120, "", "", jordan),
        ("Army EIS Cloud Migration Advisory", "army.mil", "robert.haynes@army.mil", "Proposal", 3100000, 75, "W52P1J-25-R-0044", "Direct", owner),
        ("VDOT Asset Mgmt Strategy", "vdot.virginia.gov", "sofia.nguyen@vdot.virginia.gov", "Lost", 300000, -20, "", "", sam),
        ("GSA FedRAMP Process Review", "gsa.gov", "priya.raman@gsa.gov", "Awarded", 420000, -75, "47QTCA25F0091", "GSA MAS", sam),
    ]
    for name, dom, email, stage, value, close_days, sol, veh, own in opps:
        close = (datetime.now() + timedelta(days=close_days)).date().isoformat()
        created = _dt(abs(close_days) + 90).isoformat(sep=" ")
        closed_at = close if stage in (won, lost) else None
        db.execute("""INSERT INTO opportunities(name, org_id, contact_id, stage, value, probability, close_date,
                      solicitation_no, contract_vehicle, owner_id, created_at, updated_at, stage_changed_at, closed_at)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (name, org_ids[dom], cids[email], stage, value, probs.get(stage, 10), close, sol, veh, own,
                    created, now, created, closed_at))

    # historical correspondence via the real sync engine (so counters and attribution are consistent)
    from .sync import process_message, Stats
    st = Stats()
    me = _me()
    people = {owner: me, jordan: {"name": TEAMMATES[0][0], "email": TEAMMATES[0][1]},
              sam: {"name": TEAMMATES[1][0], "email": TEAMMATES[1][1]}}
    hist = [
        (40, "in", owner, "maria.delgado@cisa.dhs.gov", "ZTA industry day questions", "Thanks for attending - please send your questions by Friday."),
        (39, "out", owner, "maria.delgado@cisa.dhs.gov", "RE: ZTA industry day questions", "Hi Maria, questions attached. Appreciate the opportunity."),
        (25, "out", sam, "priya.raman@gsa.gov", "Product coaching capability", "Hi Priya, sharing our capability statement as discussed."),
        (24, "in", sam, "priya.raman@gsa.gov", "RE: Product coaching capability", "Thanks Sam - let's meet next week."),
        (18, "out", jordan, "daniel.okafor@dir.texas.gov", "Proposal submitted - Data Modernization", "Daniel, confirming our submission went in today."),
        (12, "in", owner, "robert.haynes@army.mil", "Cloud migration - draft PWS", "Draft PWS attached for industry comment."),
        (11, "out", owner, "robert.haynes@army.mil", "RE: Cloud migration - draft PWS", "Colonel, comments enclosed. Happy to discuss."),
        (8, "out", jordan, "lauren.price@austintexas.gov", "Smart mobility pilot - intro", "Hi Lauren, great meeting you at the summit."),
        (6, "out", sam, "sofia.nguyen@vdot.virginia.gov", "Debrief request - Asset Mgmt Strategy", "Sofia, could we schedule a debrief?"),
        (3, "in", owner, "marcus.bell@brightline-analytics.com", "Teaming on CISA ZTA", "We're in. Sending our past performance writeups."),
    ]
    for days, d, uid, email, subj, body in hist:
        when = _dt(days, 9 + days % 7)
        mine = people[uid]
        other = {"name": "", "email": email}
        msg = {"internet_message_id": f"<seed-{days}-{email}>", "conversation_id": f"conv-{subj.replace('RE: ', '')}",
               "subject": subj, "body": body, "from": mine if d == "out" else other,
               "to": [other] if d == "out" else [mine], "cc": [], "date": when.isoformat(sep=" "),
               "date_utc": _utc(when), "headers": {}}
        process_message(msg, via="auto-sync", stats=st, mailbox_user_id=uid)

    db.execute("INSERT INTO activities(contact_id, org_id, user_id, kind, subject, body, outcome, duration_min, "
               "occurred_at, created_at, via) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (cids["daniel.okafor@dir.texas.gov"], org_ids["dir.texas.gov"], jordan, "call", "Pre-proposal call",
                "Discussed evaluation criteria; price is 30%.", "Positive", 25, _dt(20).isoformat(sep=" "), now, "manual"))
    db.execute("INSERT INTO activities(contact_id, org_id, user_id, kind, subject, body, occurred_at, created_at, via) "
               "VALUES (?,?,?,?,?,?,?,?,?)",
               (cids["maria.delgado@cisa.dhs.gov"], org_ids["cisa.dhs.gov"], owner, "meeting", "ZTA strategy session",
                "Maria flagged identity & ICAM as top priority for FY26.", _dt(30).isoformat(sep=" "), now, "manual"))
    db.execute("INSERT INTO activities(contact_id, org_id, user_id, kind, subject, body, occurred_at, created_at, via) "
               "VALUES (?,?,?,?,?,?,?,?,?)",
               (cids["priya.raman@gsa.gov"], org_ids["gsa.gov"], sam, "meeting", "TTS coaching discovery session",
                "Priya wants a pilot with two product teams before committing to the BPA.", _dt(15).isoformat(sep=" "),
                now, "manual"))
    tasks = [
        ("Send ZTA past-performance write-ups to Marcus", "marcus.bell@brightline-analytics.com", 2, "High", owner),
        ("Prep orals deck for Army EIS", "robert.haynes@army.mil", -1, "High", owner),
        ("Draft pilot proposal for GSA TTS", "priya.raman@gsa.gov", 4, "Normal", sam),
        ("Check DIR evaluation timeline", "daniel.okafor@dir.texas.gov", 1, "Normal", jordan),
    ]
    for title, email, days, prio, uid in tasks:
        db.execute("INSERT INTO tasks(title, contact_id, assigned_to, created_by, due_date, priority, notes, created_at) "
                   "VALUES (?,?,?,?,?,?,?,?)",
                   (title, cids[email], uid, owner, (datetime.now() + timedelta(days=days)).date().isoformat(), prio,
                    "", now))

    stamp = _utc(datetime.now() - timedelta(hours=6))
    db.set_setting("demo_anchor", (datetime.now() - timedelta(hours=3)).replace(microsecond=0).isoformat(sep=" "))
    for k in ("inbox", "sent", "capture"):
        db.set_setting(f"last_sync_{k}", stamp)
    _reset_outbox()
    from .sync import apply_followup_rules
    apply_followup_rules()
    return True


# ------------------------------------------------------------------ mailbox
def _reset_outbox():
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUTBOX, "w") as f:
        json.dump([], f)


def _incoming():
    """New mail that 'arrives' in the signed-in admin's demo mailbox."""
    me = _me()
    jordan = {"name": TEAMMATES[0][0], "email": TEAMMATES[0][1]}
    anchor = db.get_setting("demo_anchor")
    base = datetime.fromisoformat(anchor) if anchor else datetime.now() - timedelta(hours=3)

    def m(idx, folder, frm, to, subj, body, cc=None, headers=None, mins=0):
        when = base + timedelta(minutes=mins)
        return {"id": f"demo-{idx}", "internet_message_id": f"<demo-{idx}@relay.local>",
                "conversation_id": f"conv-demo-{idx}", "subject": subj, "body": body,
                "from": frm, "to": to, "cc": cc or [], "date": when.replace(microsecond=0).isoformat(sep=" "),
                "date_utc": _utc(when), "folder": folder, "headers": headers or {}}
    return [
        # inbound from an existing contact (owned by Jordan) -> logged, closes "awaiting reply" task
        m(1, "inbox", {"name": "Okafor, Daniel (DIR)", "email": "daniel.okafor@dir.texas.gov"}, [me],
          "RE: Proposal submitted - Data Modernization",
          "Thanks - evaluations wrap up next week. We may have clarification questions.", cc=[jordan], mins=5),
        # inbound from a brand-new person -> new contact + new org (flagged for review)
        m(2, "inbox", {"name": "Thompson, Kevin CTR (USA)", "email": "kevin.thompson.ctr@hhs.gov"}, [me],
          "Introduction via Maria Delgado",
          "Maria suggested I reach out about our identity modernisation effort at HHS.",
          cc=[{"name": "Maria Delgado", "email": "maria.delgado@cisa.dhs.gov"}], mins=20),
        # newsletter -> ignored
        m(3, "inbox", {"name": "GovTech Weekly", "email": "newsletter@govtech-weekly.com"}, [me],
          "This week in GovTech", "Top stories...", headers={"list-unsubscribe": "<mailto:x>"}, mins=30),
        # outbound to a brand-new contact (with cc) -> new contacts created
        m(4, "sent", me, [{"name": "Angela Ruiz", "email": "angela.ruiz@hcde.org"}],
          "Follow-up from the ed-tech roundtable",
          "Angela, great to meet you. Attaching the case study I mentioned.",
          cc=[{"name": "Tom Becker", "email": "t.becker@hcde.org"}], mins=45),
        # email to a colleague -> ignored (team member)
        m(5, "sent", me, [jordan], "Pricing for VDOT", "Internal only.", mins=50),
        # BCC'd to CRM folder: email sent from a phone to a new contact
        m(6, "capture", me, [{"name": "Chen, Wei", "email": "wei.chen@nyc.gov"}],
          "Smart city data platform - next steps",
          "Wei, as promised here is the outline for the pilot.", mins=70),
        # Forwarded to CRM: an old thread
        m(7, "capture", me, [{"name": "CRM", "email": me["email"]}], "FW: Teaming question - USDA RFP",
          "Log this one please.\n\n________________________________\n"
          "From: Rachel Ortiz <rachel.ortiz@usda.gov>\nSent: Monday, March 3, 2025 9:14 AM\n"
          "To: " + me["name"] + " <" + me["email"] + ">\nSubject: Teaming question - USDA RFP\n\n"
          "Are you planning to bid on the USDA analytics RFP?", mins=80),
    ]


class DemoSource:
    def fetch(self, key, since_utc):
        items = [x for x in _incoming() if x["folder"] == key]
        if key == "sent" and os.path.exists(OUTBOX):
            with open(OUTBOX) as f:
                items += json.load(f)
        for it in sorted(items, key=lambda x: x["date_utc"]):
            if not since_utc or it["date_utc"] > since_utc:
                yield it


def wipe_crm_data():
    for t in ("activities", "tasks", "opportunities", "contacts", "organizations", "sync_log"):
        db.execute(f"DELETE FROM {t}")
    for k in ("last_sync_inbox", "last_sync_sent", "last_sync_capture", "last_sync_shared"):
        db.set_setting(k, "")


def remove_demo_users():
    db.execute("DELETE FROM users WHERE is_demo=1")
    doms = [d for d in db.setting_list("internal_domains") if d != DEMO_DOMAIN]
    db.set_setting("internal_domains", ",".join(doms))


def reset_demo():
    """Wipe everything and reseed."""
    wipe_crm_data()
    seed()
