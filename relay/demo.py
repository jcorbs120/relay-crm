"""Sample public-sector data + a simulated mailbox for trying the CRM safely."""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from .db import now_iso
from .sync import iso, parse_dt

ME = "you@relayconsulting.com"

ORGS = [
    ("Cybersecurity & Infrastructure Security Agency", "cisa.dhs.gov", "Federal"),
    ("Department of Veterans Affairs", "va.gov", "Federal"),
    ("General Services Administration", "gsa.gov", "Federal"),
    ("State of California", "ca.gov", "State"),
    ("Fairfax County", "fairfaxcounty.gov", "Local"),
    ("George Mason University", "gmu.edu", "Education"),
    ("Northwind Federal (teaming partner)", "northwindfed.com", "Commercial"),
]
CONTACTS = [
    # first, last, email, title, org_domain, phone, lifecycle, tags
    ("Maria", "Lopez", "maria.lopez@cisa.dhs.gov", "Branch Chief, Zero Trust", "cisa.dhs.gov", "202-555-0141", "Prospect", "zero-trust"),
    ("Kevin", "Ward", "kevin.ward@cisa.dhs.gov", "Contracting Officer", "cisa.dhs.gov", "202-555-0188", "Prospect", "contracting"),
    ("James", "Whitfield", "james.whitfield@va.gov", "Program Manager, EHR", "va.gov", "202-555-0102", "Customer", "health-it"),
    ("Alicia", "Brooks", "alicia.brooks@gsa.gov", "Category Manager, IT", "gsa.gov", "202-555-0177", "Lead", "schedule"),
    ("Sam", "Patel", "sam.patel@ca.gov", "Deputy CIO", "ca.gov", "916-555-0123", "Prospect", "state-it"),
    ("Linda", "Chen", "linda.chen@fairfaxcounty.gov", "Director, Emergency Mgmt", "fairfaxcounty.gov", "703-555-0190", "Customer", "local"),
    ("Robert", "Hayes", "rhayes@gmu.edu", "Associate Provost, Research IT", "gmu.edu", "703-555-0155", "Lead", "edu"),
    ("Erin", "Moss", "erin.moss@northwindfed.com", "Capture Manager", "northwindfed.com", "571-555-0110", "Partner", "teaming"),
]
OPPS = [
    # name, org, contact_email, stage, value, prob, close_in_days, solicitation, vehicle
    ("Zero Trust Architecture Pilot", "cisa.dhs.gov", "maria.lopez@cisa.dhs.gov", "Proposal", 1850000, 50, 45, "70RCSA25R0000012", "CIO-SP4"),
    ("SOC Analyst Surge Support", "cisa.dhs.gov", "kevin.ward@cisa.dhs.gov", "Qualified", 640000, 30, 75, "", "GSA MAS"),
    ("EHR Modernization Change Mgmt", "va.gov", "james.whitfield@va.gov", "Awarded", 2400000, 100, -20, "36C10B24Q0042", "T4NG2"),
    ("EHR Training Phase 2", "va.gov", "james.whitfield@va.gov", "Submitted", 980000, 70, 30, "36C10B25Q0107", "T4NG2"),
    ("MAS IT Category Onboarding", "gsa.gov", "alicia.brooks@gsa.gov", "Lead", 150000, 10, 120, "", "GSA MAS"),
    ("Statewide Cloud Strategy", "ca.gov", "sam.patel@ca.gov", "Proposal", 1200000, 40, 60, "RFP 25-0931", "CMAS"),
    ("Emergency Ops Dashboard", "fairfaxcounty.gov", "linda.chen@fairfaxcounty.gov", "Awarded", 310000, 100, -45, "RFP 2024-118", "Direct"),
    ("Hazard Mitigation Plan Update", "fairfaxcounty.gov", "linda.chen@fairfaxcounty.gov", "Lost", 220000, 0, -10, "RFP 2025-033", "Direct"),
    ("Research Computing Assessment", "gmu.edu", "rhayes@gmu.edu", "Lead", 90000, 10, 90, "", "Direct"),
]
TEMPLATES = [
    ("Intro / capabilities", "Relay Consulting capabilities for {org}",
     "Hi {first_name},\n\nThanks for your time. As promised, here is a short summary of our capabilities "
     "relevant to {org}. Happy to set up 30 minutes to walk through it.\n\nBest regards,\n{my_name}"),
    ("Follow-up after meeting", "Follow-up: our conversation",
     "Hi {first_name},\n\nThank you for meeting with us. Recapping next steps:\n\n- \n- \n\nPlease let me know "
     "if I missed anything.\n\nBest,\n{my_name}"),
    ("Checking in", "Checking in",
     "Hi {first_name},\n\nI wanted to check in on the item below and see whether there's anything you need "
     "from us.\n\nThanks,\n{my_name}"),
]


def seed(db):
    """Load the demo data set (wipes existing CRM records, keeps settings)."""
    db.wipe_data()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    db.set("demo_anchor", iso(now))
    db.set("mode", "demo")
    db.set("my_email", ME)
    db.set("my_name", db.get("my_name") or "Demo User")
    db.set("last_sync_at", "")
    org_ids = {}
    for name, dom, typ in ORGS:
        org_ids[dom] = db.ex("INSERT INTO orgs(name, domain, org_type, created_at) VALUES(?,?,?,?)",
                             (name, dom, typ, iso(now - timedelta(days=120))))
    cids = {}
    for i, (f, l, e, t, dom, ph, life, tags) in enumerate(CONTACTS):
        cids[e] = db.ex(
            "INSERT INTO contacts(first_name,last_name,email,title,org_id,phone,lifecycle,tags,source,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (f, l, e, t, org_ids[dom], ph, life, tags, "demo", iso(now - timedelta(days=110 - i * 9))))
    oids = {}
    for name, dom, ce, stage, val, prob, close_in, sol, veh in OPPS:
        closed = iso(now + timedelta(days=close_in)) if stage in ("Awarded", "Lost") else None
        oids[name] = db.ex(
            "INSERT INTO opportunities(name,org_id,contact_id,stage,value,probability,close_date,solicitation,vehicle,"
            "created_at,updated_at,closed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, org_ids[dom], cids[ce], stage, val, prob, (now + timedelta(days=close_in)).date().isoformat(),
             sol, veh, iso(now - timedelta(days=100)), iso(now - timedelta(days=abs(close_in) % 20)), closed))

    def act(email, kind, subj, body, days_ago, opp=None, dur=None):
        db.ex("INSERT INTO activities(contact_id,opp_id,kind,subject,body,occurred_at,message_id,duration_min,created_at)"
              " VALUES(?,?,?,?,?,?,?,?,?)",
              (cids[email], oids.get(opp), kind, subj, body, iso(now - timedelta(days=days_ago)),
               f"demo-hist-{email}-{days_ago}-{kind}" if kind.startswith("email") else None, dur, now_iso()))
        db.ex("UPDATE contacts SET last_contacted=max(coalesce(last_contacted,''), ?) WHERE id=?",
              (iso(now - timedelta(days=days_ago)), cids[email]))

    m, k, j = "maria.lopez@cisa.dhs.gov", "kevin.ward@cisa.dhs.gov", "james.whitfield@va.gov"
    act(m, "meeting", "Zero Trust discovery session", "Met with Maria and 2 architects. Pain points: legacy VPN, identity sprawl.", 40, "Zero Trust Architecture Pilot", 60)
    act(m, "email_in", "RE: Zero Trust discovery follow-up", "Thanks for the recap. Can you send a rough approach?", 30, "Zero Trust Architecture Pilot")
    act(m, "email_out", "Zero Trust pilot - draft approach", "Hi Maria, attached is our draft approach and staffing plan. Let me know your thoughts.", 7, "Zero Trust Architecture Pilot")
    act(k, "call", "Intro call re: SOC surge", "Kevin confirmed a recompete is likely in Q2. Wants capability statement.", 22, "SOC Analyst Surge Support", 25)
    act(k, "email_out", "Capability statement - SOC support", "Kevin, as discussed, attached is our capability statement.", 21, "SOC Analyst Surge Support")
    act(k, "email_in", "RE: Capability statement - SOC support", "Received, thank you. Will share with the program office.", 20, "SOC Analyst Surge Support")
    act(j, "note", "Kickoff complete", "Phase 1 kickoff done. Weekly status every Tuesday.", 18, "EHR Modernization Change Mgmt")
    act(j, "email_in", "Phase 2 training - questions", "We reviewed your proposal. Two questions on the train-the-trainer model.", 3, "EHR Training Phase 2")
    act("sam.patel@ca.gov", "email_out", "Cloud strategy RFP - intent to bid", "Sam, confirming our intent to bid on RFP 25-0931.", 12, "Statewide Cloud Strategy")
    act("sam.patel@ca.gov", "email_in", "RE: Cloud strategy RFP - intent to bid", "Noted. Q&A deadline is next Friday.", 11, "Statewide Cloud Strategy")
    act("linda.chen@fairfaxcounty.gov", "meeting", "Dashboard go-live review", "Go-live successful. Linda interested in a hazard mitigation follow-on.", 35, None, 45)
    act("linda.chen@fairfaxcounty.gov", "email_in", "Hazard mitigation award notice", "Unfortunately we selected another vendor this time.", 10)
    act("alicia.brooks@gsa.gov", "email_out", "MAS onboarding questions", "Alicia, a few questions about the IT category refresh.", 75)
    act("erin.moss@northwindfed.com", "email_in", "Teaming on CISA SOC", "Interested in teaming as a sub on the SOC recompete?", 16, "SOC Analyst Surge Support")
    act("erin.moss@northwindfed.com", "email_out", "RE: Teaming on CISA SOC", "Yes - let's set up an NDA and a call.", 15, "SOC Analyst Surge Support")

    def task(email, title, due_in, kind="manual", prio="Normal", opp=None):
        db.ex("INSERT INTO tasks(contact_id,opp_id,title,due_date,kind,priority,created_at) VALUES(?,?,?,?,?,?,?)",
              (cids[email], oids.get(opp), title, (now + timedelta(days=due_in)).date().isoformat(), kind, prio,
               iso(now - timedelta(days=1))))

    task(m, "Awaiting reply from Maria Lopez: Zero Trust pilot - draft approach", -1, "awaiting_reply", opp="Zero Trust Architecture Pilot")
    task("sam.patel@ca.gov", "Submit Q&A questions for RFP 25-0931", 2, prio="High", opp="Statewide Cloud Strategy")
    task("alicia.brooks@gsa.gov", "Re-engage Alicia on MAS refresh", -3)
    task("erin.moss@northwindfed.com", "Send NDA to Northwind", 0, prio="High")
    task("rhayes@gmu.edu", "Schedule research computing discovery call", 5)
    for name, subj, body in TEMPLATES:
        db.ex("INSERT INTO email_templates(name, subject, body) VALUES(?,?,?)", (name, subj, body))


class DemoMailbox:
    """A pretend Outlook mailbox. Messages are fixed relative to when demo data was loaded."""

    def __init__(self, db):
        self.db = db

    def messages(self):
        anchor = parse_dt(self.db.get("demo_anchor") or now_iso())

        def at(hours_ago):
            return iso(anchor - timedelta(hours=hours_ago))

        return [
            # 1. Existing contact replies -> logged, Awaiting-reply task closes, stays linked to the opportunity
            ("inbox", {"id": "<demo-1@cisa.dhs.gov>", "subject": "RE: Zero Trust pilot - draft approach",
                       "from": ("maria.lopez@cisa.dhs.gov", "Lopez, Maria"), "to": [(ME, "")], "date": at(1),
                       "body": "This looks good. Can your team present to our CISO on the 14th?"}),
            # 2. Brand-new sender -> new contact + new organization (DOT, Federal), flagged for review
            ("inbox", {"id": "<demo-2@dot.gov>", "subject": "Request for information - fleet analytics",
                       "from": ("daniel.okafor@dot.gov", "Okafor, Daniel (FHWA)"), "to": [(ME, "")], "date": at(5),
                       "body": "We found your firm on GSA eLibrary. Are you able to respond to an RFI on fleet analytics?"}),
            # 3. I email someone new -> contact created right away (no review), State of Michigan created
            ("sent", {"id": "<demo-3@relayconsulting.com>", "subject": "Introduction - Relay Consulting",
                      "from": (ME, "Demo User"), "to": [("priya.nair@michigan.gov", "Priya Nair")],
                      "cc": [("colleague@relayconsulting.com", "Colleague")], "date": at(4),
                      "body": "Hi Priya, Sam Patel suggested I reach out about your cloud program."}),
            # 4-5. Newsletters / no-reply -> ignored
            ("inbox", {"id": "<demo-4@govdelivery.com>", "subject": "Weekly acquisition news",
                       "from": ("news@public.govdelivery.com", "GovDelivery"), "to": [(ME, "")], "date": at(6), "body": "..."}),
            ("inbox", {"id": "<demo-5@sam.gov>", "subject": "Your SAM registration",
                       "from": ("no-reply@sam.gov", "SAM.gov"), "to": [(ME, "")], "date": at(7), "body": "..."}),
            # 6. Colleague -> ignored (internal domain)
            ("inbox", {"id": "<demo-6@relayconsulting.com>", "subject": "Lunch?",
                       "from": ("colleague@relayconsulting.com", "Colleague"), "to": [(ME, "")], "date": at(2), "body": "..."}),
            # 7. I forwarded a phone-sent email into the CRM folder -> filed under the original sender
            ("crm", {"id": "<demo-7@relayconsulting.com>", "subject": "FW: Budget for EOC upgrades",
                     "from": (ME, "Demo User"), "to": [("you+crm@relayconsulting.com", "")], "date": at(3),
                     "body": "FYI\n\n-----Original Message-----\nFrom: Tom Becker <tom.becker@fairfaxcounty.gov>\n"
                             f"Sent: {format_datetime(anchor - timedelta(hours=26))}\nTo: you@relayconsulting.com\n"
                             "Subject: Budget for EOC upgrades\n\nWe have FY26 funds for EOC upgrades. Can we talk?"}),
            # 8. BCC to CRM folder on an outbound email
            ("crm", {"id": "<demo-8@relayconsulting.com>", "subject": "Phase 2 training - answers",
                     "from": (ME, "Demo User"), "to": [("james.whitfield@va.gov", "James Whitfield")],
                     "bcc": [("you+crm@relayconsulting.com", "")], "date": at(0.5),
                     "body": "James, answers to your two questions are below."}),
        ]

    def fetch(self, since):
        return list(self.messages())

    def send(self, to, subject, body, cc=None, html=False):
        return {"internet_message_id": None, "conversation_id": None, "demo": True}
