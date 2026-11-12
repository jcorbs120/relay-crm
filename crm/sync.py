"""Email -> CRM sync engine (team edition).

Every team member's mailbox is synced into ONE shared CRM:

1. Automatic sync   - each connected user's Inbox and Sent Items are scanned
                      every N minutes.
2. Capture folder   - anything a user BCCs / forwards / drags into their "CRM"
                      folder is logged. Optionally a shared team capture mailbox
                      (crm@yourfirm.com) that anyone can BCC.
3. Compose          - email sent from inside Relay is logged immediately.

Messages are de-duplicated by Internet Message-ID, so when two colleagues are
on the same thread it is logged once per contact. Mail *between* colleagues is
never logged and colleagues never become contacts. Each logged email records
which team member was involved (activities.user_id); new contacts are owned by
the person who first corresponded with them; follow-up tasks are assigned to
the team member who owns the conversation.
"""
import logging
import re
import threading
from datetime import datetime, timedelta, timezone

from . import db

log = logging.getLogger("relay.sync")

PUBLIC_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com", "yahoo.com",
    "icloud.com", "me.com", "mac.com", "aol.com", "proton.me", "protonmail.com", "gmx.com", "comcast.net",
    "verizon.net", "att.net", "ymail.com", "mail.com", "zoho.com",
}

_sync_lock = threading.Lock()
sync_state = {"running": False, "last_result": None}


# ======================================================================= helpers
def domain_of(email):
    return email.split("@", 1)[1].lower() if email and "@" in email else ""


def user_addresses(u):
    """All addresses a team member sends/receives as."""
    out = set()
    for a in [u["email"], u["ms_email"]] + (u["aliases"] or "").split(","):
        a = (a or "").strip().lower()
        if a and "@" in a:
            out.add(a)
    return out


def team_directory():
    """address -> user id for every team member (active or not, so ex-staff aren't turned into contacts)."""
    d = {}
    for u in db.users(active_only=False):
        for a in user_addresses(u):
            d[a] = u["id"]
    return d


def shared_addresses():
    cap = (db.get_setting("capture_mailbox") or "").strip().lower()
    return {cap} if cap else set()


def internal_domains():
    doms = {d.lower().lstrip("@") for d in db.setting_list("internal_domains")}
    for u in db.users(active_only=True, include_demo=True):
        for a in user_addresses(u):
            d = domain_of(a)
            if d and d not in PUBLIC_DOMAINS:
                doms.add(d)
    return doms


def is_ignored(email, headers=None):
    """Filter automated / bulk / no-reply senders."""
    e = email.lower()
    for pat in db.setting_list("ignore_patterns"):
        if pat.lower() in e:
            return True
    if headers and db.get_setting("skip_bulk") == "1":
        prec = (headers.get("precedence") or "").lower()
        if prec in ("bulk", "list", "junk"):
            return True
        if headers.get("list-unsubscribe") or headers.get("list-id"):
            return True
        if (headers.get("auto-submitted") or "no").lower() not in ("no", ""):
            return True
    return False


def split_name(display, email):
    """Turn 'Smith, Jane (DHS)' / 'Jane Smith' / 'jane.smith@x' into (first, last)."""
    name = re.sub(r"\(.*?\)|\[.*?\]", "", display or "").strip().strip("'\"")
    name = re.sub(r"\s+(CTR|CIV|USA|USN|USAF|USMC|USCG|OST|OS|FED|Contractor)\b.*$", "", name, flags=re.I).strip()
    if name and "@" not in name:
        if "," in name:
            last, first = [p.strip() for p in name.split(",", 1)]
            first = first.split()[0] if first else ""
            return first.title() if first.isupper() else first, last.title() if last.isupper() else last
        parts = name.split()
        if len(parts) == 1:
            return parts[0], ""
        first, last = parts[0], parts[-1]
        return first, last
    local = (email or "").split("@")[0]
    local = re.sub(r"\d+", "", local)
    parts = [p for p in re.split(r"[._\-]+", local) if p]
    if len(parts) >= 2:
        return parts[0].title(), parts[-1].title()
    return (parts[0].title() if parts else ""), ""


GOV_SUFFIX = re.compile(r"\.(gov|mil|edu|org|com|net|us|int|ca|uk|au)$", re.I)


US_STATES = ["alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware",
             "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky",
             "louisiana", "maine", "maryland", "massachusetts", "mass", "michigan", "minnesota", "mississippi",
             "missouri", "montana", "nebraska", "nevada", "newhampshire", "nh", "newjersey", "nj", "newmexico", "nm",
             "newyork", "ny", "northcarolina", "nc", "northdakota", "nd", "ohio", "oklahoma", "ok", "oregon",
             "pennsylvania", "pa", "rhodeisland", "ri", "southcarolina", "sc", "southdakota", "sd", "tennessee", "tn",
             "texas", "utah", "vermont", "virginia", "va", "washington", "wa", "westvirginia", "wv", "wisconsin",
             "wi", "wyoming", "wyo", "ca", "co", "ct", "de", "fl", "ga", "hi", "il", "in", "ia", "ky", "la", "md",
             "ma", "mi", "mn", "ms", "mo", "mt", "ne", "nv", "or", "ut", "vt", "dc"]
_STATE_SET = set(US_STATES)


def _gov_type(sld):
    """Classify a .gov second-level label: State, Local or Federal."""
    if sld in _STATE_SET:
        return "State"
    for st in sorted(_STATE_SET, key=len, reverse=True):
        if len(st) > 3 and sld.endswith(st) and sld != st:
            return "Local"         # austintexas.gov, cityofbostonma... -> local gov
    if sld.startswith(("cityof", "countyof", "townof", "villageof")) or sld.endswith(("county", "city")):
        return "Local"
    if sld in KNOWN_LOCAL_GOV:
        return "Local"
    return "Federal"


KNOWN_LOCAL_GOV = {"nyc", "lacity", "chicago", "houstontx", "phoenix", "philadelphia", "phila", "sanantonio",
                   "sandiego", "dallascityhall", "sanjoseca", "jacksonville", "coj", "fortworthtexas",
                   "columbus", "charlottenc", "indy", "sfgov", "seattle", "denvergov", "boston", "nashville",
                   "detroitmi", "portland", "portlandoregon", "baltimorecity", "atlantaga", "miamigov"}


def guess_org(domain):
    """Return (name, type) for a new organisation inferred from an email domain."""
    labels = domain.lower().split(".")
    tld = labels[-1]
    org_type = ""
    if tld == "mil":
        org_type = "Military / DoD"
    elif tld == "gov":
        org_type = _gov_type(labels[-2]) if len(labels) >= 2 else "Federal"
        if org_type == "State" and len(labels) >= 3:
            st = labels[-2]
            return f"{st.upper() if len(st) <= 3 else st.title()} - {labels[-3].upper()}", "State"
        if org_type == "Local":
            return labels[-2].replace("-", " ").title(), "Local"
    elif tld == "us":
        # state.tx.us -> State; ci.austin.tx.us / co.travis.tx.us / k12 -> Local
        org_type = "State" if "state" in labels[:-2] else ("Local" if len(labels) >= 4 else "State")
    elif tld == "edu":
        org_type = "Education"
    elif tld == "org":
        org_type = "Non-profit"
    else:
        org_type = "Private Sector"
    core = labels[-2] if len(labels) >= 2 else labels[0]
    if tld in ("gov", "mil") and len(labels) >= 3:
        # e.g. cio.dhs.gov -> DHS ; army.mil stays ARMY; state: dot.state.tx.us handled below
        core = labels[-2]
    if tld == "us" and len(labels) >= 3:
        core = ".".join(labels[:-1])
    name = core.upper() if (len(core) <= 5 or tld in ("gov", "mil")) else core.replace("-", " ").title()
    if tld == "us":
        name = " ".join(p.upper() if len(p) <= 3 else p.title() for p in core.split("."))
    return name, org_type


def get_or_create_org(domain, owner_id=None):
    if not domain or domain in PUBLIC_DOMAINS:
        return None
    row = db.query("SELECT id FROM organizations WHERE domain=?", (domain,), one=True)
    if row:
        return row["id"]
    # also match parent domain (e.g. cisa.dhs.gov -> dhs.gov org if exists)
    parts = domain.split(".")
    for i in range(1, len(parts) - 1):
        parent = ".".join(parts[i:])
        row = db.query("SELECT id FROM organizations WHERE domain=?", (parent,), one=True)
        if row:
            return row["id"]
    name, otype = guess_org(domain)
    return db.execute(
        "INSERT INTO organizations(name, domain, org_type, owner_id, created_at) VALUES (?,?,?,?,?)",
        (name, domain, otype, owner_id, db.now_iso()))


def find_contact(email):
    return db.query("SELECT * FROM contacts WHERE lower(email)=?", (email.lower(),), one=True)


def create_contact(party, source, needs_review=0, owner_id=None):
    first, last = split_name(party.get("name"), party["email"])
    org_id = get_or_create_org(domain_of(party["email"]), owner_id)
    now = db.now_iso()
    cid = db.execute(
        """INSERT INTO contacts(first_name,last_name,email,org_id,status,source,needs_review,owner_id,created_by,
                                created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (first, last, party["email"].lower(), org_id, "Lead", source, needs_review, owner_id, owner_id, now, now))
    db.execute("INSERT INTO activities(contact_id, org_id, user_id, kind, subject, body, occurred_at, created_at, via) "
               "VALUES (?,?,?,?,?,?,?,?,?)",
               (cid, org_id, owner_id, "system", "Contact created automatically",
                f"First seen in email ({source}).", now, now, "auto-sync"))
    return db.query("SELECT * FROM contacts WHERE id=?", (cid,), one=True)


# ------------------------------------------------------------ forwarded mail
FWD_SUBJECT = re.compile(r"^\s*(fw|fwd)\s*:", re.I)
FWD_FROM = re.compile(r"^\s*\**From:\**\s*(?P<v>.+)$", re.I | re.M)
FWD_SENT = re.compile(r"^\s*\**(Sent|Date):\**\s*(?P<v>.+)$", re.I | re.M)
FWD_TO = re.compile(r"^\s*\**To:\**\s*(?P<v>.+)$", re.I | re.M)
FWD_CC = re.compile(r"^\s*\**Cc:\**\s*(?P<v>.+)$", re.I | re.M)
FWD_SUBJ = re.compile(r"^\s*\**Subject:\**\s*(?P<v>.+)$", re.I | re.M)
ADDR_RE = re.compile(r"(?:\"?([^\"<>,;]*?)\"?\s*)?<?([A-Za-z0-9._%+'\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})>?")


def _parse_addr_list(text):
    out = []
    for name, email in ADDR_RE.findall(text or ""):
        out.append({"name": name.strip().strip(";,"), "email": email.lower()})
    return out


def _parse_fwd_date(s):
    s = re.sub(r"\s+", " ", s.strip())
    for fmt in ("%A, %B %d, %Y %I:%M %p", "%A, %B %d, %Y %H:%M", "%a, %b %d, %Y at %I:%M %p",
                "%a, %d %b %Y %H:%M:%S %z", "%m/%d/%Y %I:%M %p", "%B %d, %Y %I:%M %p", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo:
                dt = dt.astimezone().replace(tzinfo=None)
            return dt.replace(microsecond=0).isoformat(sep=" ")
        except ValueError:
            continue
    return None


def unwrap_forward(msg):
    """If msg is a message I forwarded to the CRM, return the original message."""
    if not FWD_SUBJECT.match(msg["subject"] or ""):
        return None
    body = msg.get("body") or ""
    m_from = FWD_FROM.search(body)
    if not m_from:
        return None
    start = m_from.start()
    block = body[start:start + 1500]
    orig_from = _parse_addr_list(m_from.group("v"))
    if not orig_from:
        return None
    def grab(rx):
        mm = rx.search(block)
        return mm.group("v").strip() if mm else ""
    to = _parse_addr_list(grab(FWD_TO))
    cc = _parse_addr_list(grab(FWD_CC))
    subj = grab(FWD_SUBJ) or FWD_SUBJECT.sub("", msg["subject"]).strip()
    date = _parse_fwd_date(grab(FWD_SENT)) if grab(FWD_SENT) else None
    # body of original = everything after the header block
    hdr_end = block.find("\n\n", 0)
    orig_body = body[start + hdr_end:].strip() if hdr_end > 0 else body[start:]
    note = body[:start].strip()
    return {
        **msg,
        "internet_message_id": f"fwd:{msg['internet_message_id']}",
        "conversation_id": None,
        "subject": subj,
        "body": (f"[Forwarded to CRM] {note}\n\n" if note else "") + orig_body,
        "from": orig_from[0],
        "to": to,
        "cc": cc,
        "date": date or msg["date"],
        "headers": {},
        "_forwarded": True,
    }


# ===================================================================== core
class Stats:
    def __init__(self):
        self.scanned = self.logged = self.new_contacts = self.tasks_created = self.tasks_closed = 0
        self.new_contact_ids = []

    def as_dict(self):
        return {k: v for k, v in self.__dict__.items()}

    def add(self, other):
        for k in ("scanned", "logged", "new_contacts", "tasks_created", "tasks_closed"):
            setattr(self, k, getattr(self, k) + getattr(other, k))
        self.new_contact_ids += other.new_contact_ids


def _open_opp_for(contact):
    rows = db.query(
        "SELECT id FROM opportunities WHERE (contact_id=? OR (org_id IS NOT NULL AND org_id=?)) "
        "AND stage NOT IN (?,?) ORDER BY updated_at DESC",
        (contact["id"], contact["org_id"], *db.closed_stages()))
    if len(rows) == 1:
        return rows[0]["id"]
    direct = [r for r in db.query(
        "SELECT id FROM opportunities WHERE contact_id=? AND stage NOT IN (?,?)",
        (contact["id"], *db.closed_stages()))]
    return direct[0]["id"] if len(direct) == 1 else None


def process_message(msg, via="auto-sync", stats=None, mailbox_user_id=None):
    """Log one normalised message against all external correspondents.

    mailbox_user_id - the team member whose mailbox the message came from
    (None for the shared capture mailbox). Returns list of contact ids touched.
    """
    stats = stats or Stats()
    stats.scanned += 1
    if msg.get("is_draft"):
        return []

    team = team_directory()
    shared = shared_addresses()
    internal = internal_domains()

    sender = msg.get("from") or {}
    sender_email = (sender.get("email") or "").lower()

    # Forwarded-to-CRM handling: a colleague forwarded an old thread to the capture folder/mailbox
    if via == "capture" and sender_email in team:
        unwrapped = unwrap_forward(msg)
        if unwrapped:
            unwrapped["_forwarded_by"] = team[sender_email]
            msg = unwrapped
            sender = msg["from"]
            sender_email = (sender.get("email") or "").lower()

    outbound = sender_email in team
    if outbound:
        acting_user = team[sender_email]
    else:
        acting_user = msg.get("_forwarded_by") or mailbox_user_id
        if not acting_user:
            for p in (msg.get("to") or []) + (msg.get("cc") or []):
                if (p.get("email") or "").lower() in team:
                    acting_user = team[p["email"].lower()]
                    break

    autocreate = db.get_setting("autocreate") or "both"
    if outbound:
        parties = list(msg.get("to") or [])
        if db.get_setting("autocreate_cc") == "1":
            parties += msg.get("cc") or []
        else:
            parties += [dict(p, _cc_only=True) for p in (msg.get("cc") or [])]
        kind = "email_out"
    else:
        parties = [sender] if sender.get("email") else []
        # CC'd people on inbound mail get logged only if they already exist
        parties += [dict(p, _cc_only=True) for p in (msg.get("cc") or [])]
        kind = "email_in"

    seen, touched = set(), []
    for p in parties:
        email = (p.get("email") or "").lower()
        if not email or email in seen or email in team or email in shared:
            continue
        seen.add(email)
        if domain_of(email) in internal:
            continue
        if is_ignored(email, msg.get("headers") if not outbound else None):
            continue

        contact = find_contact(email)
        if not contact:
            allowed = (autocreate == "both" or (autocreate == "outbound" and outbound)) and not p.get("_cc_only")
            if not allowed:
                continue
            source = "Email - sent" if outbound else "Email - received"
            if msg.get("_forwarded"):
                source = "Email - forwarded"
            contact = create_contact(p, source, needs_review=0 if outbound else 1, owner_id=acting_user)
            stats.new_contacts += 1
            stats.new_contact_ids.append(contact["id"])
        elif p.get("name") and not contact["first_name"] and not contact["last_name"]:
            f, l = split_name(p["name"], email)
            db.execute("UPDATE contacts SET first_name=?, last_name=? WHERE id=?", (f, l, contact["id"]))
        if contact["owner_id"] is None and acting_user:
            db.execute("UPDATE contacts SET owner_id=? WHERE id=? AND owner_id IS NULL", (acting_user, contact["id"]))

        if _log_email(contact, msg, kind, via, acting_user):
            stats.logged += 1
            touched.append(contact["id"])
            stats.tasks_closed += _close_tasks_on_email(contact["id"], kind)
    return touched


def _log_email(contact, msg, kind, via, user_id=None):
    mid = msg.get("internet_message_id")
    if mid and db.query("SELECT 1 FROM activities WHERE message_id=? AND contact_id=?",
                        (mid, contact["id"]), one=True):
        return False
    opp_id = None
    if msg.get("conversation_id"):
        r = db.query("SELECT opp_id FROM activities WHERE conversation_id=? AND opp_id IS NOT NULL LIMIT 1",
                     (msg["conversation_id"],), one=True)
        opp_id = r["opp_id"] if r else None
    if opp_id is None:
        opp_id = _open_opp_for(contact)

    body = (msg.get("body") or "")[:20000]
    db.execute(
        """INSERT INTO activities(contact_id, opp_id, org_id, user_id, kind, subject, body, message_id,
                                  conversation_id, occurred_at, created_at, via) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (contact["id"], opp_id, contact["org_id"], user_id, kind, msg.get("subject") or "", body, mid,
         msg.get("conversation_id"), msg["date"], db.now_iso(), via))

    when = msg["date"]
    if kind == "email_out":
        db.execute("""UPDATE contacts SET emails_out = emails_out + 1,
                      last_outbound = CASE WHEN last_outbound IS NULL OR last_outbound < ? THEN ? ELSE last_outbound END,
                      last_contacted = CASE WHEN last_contacted IS NULL OR last_contacted < ? THEN ? ELSE last_contacted END,
                      needs_review = 0, updated_at=? WHERE id=?""",
                   (when, when, when, when, db.now_iso(), contact["id"]))
    else:
        db.execute("""UPDATE contacts SET emails_in = emails_in + 1,
                      last_inbound = CASE WHEN last_inbound IS NULL OR last_inbound < ? THEN ? ELSE last_inbound END,
                      last_contacted = CASE WHEN last_contacted IS NULL OR last_contacted < ? THEN ? ELSE last_contacted END,
                      updated_at=? WHERE id=?""",
                   (when, when, when, when, db.now_iso(), contact["id"]))
    if opp_id:
        db.execute("UPDATE opportunities SET updated_at=? WHERE id=?", (db.now_iso(), opp_id))

    if db.get_setting("auto_promote") == "1":
        db.execute("UPDATE contacts SET status='Prospect' WHERE id=? AND status='Lead' "
                   "AND emails_in > 0 AND emails_out > 0", (contact["id"],))
    return True


def _close_tasks_on_email(contact_id, kind):
    """Inbound reply closes 'awaiting reply'; any team member's outbound closes 'reply needed'."""
    rule = "awaiting_reply" if kind == "email_in" else "reply_needed"
    rows = db.query("SELECT id FROM tasks WHERE contact_id=? AND status='Open' AND auto_rule=?",
                    (contact_id, rule))
    for r in rows:
        db.execute("UPDATE tasks SET status='Done', completed_at=?, notes=notes || ? WHERE id=?",
                   (db.now_iso(), "\nAuto-closed: correspondence detected.", r["id"]))
    return len(rows)


def apply_followup_rules(stats=None, today=None):
    """Create follow-up tasks from the state of correspondence, assigned to the
    team member who owns that side of the conversation."""
    stats = stats or Stats()
    today = today or datetime.now()
    s = db.get_settings()
    window = 21  # don't create tasks for conversations that went quiet long ago

    def _last(cid, kind):
        r = db.query("SELECT subject, conversation_id, user_id FROM activities WHERE contact_id=? AND kind=? "
                     "ORDER BY occurred_at DESC LIMIT 1", (cid, kind), one=True)
        return (r["subject"], r["conversation_id"], r["user_id"]) if r else ("", None, None)

    def _name(c):
        return f"{c['first_name']} {c['last_name']}".strip() or c["email"]

    if s.get("rule_awaiting_reply") == "1":
        n = int(s.get("followup_days") or 5)
        lo = (today - timedelta(days=n + window)).isoformat(sep=" ")
        hi = (today - timedelta(days=n)).isoformat(sep=" ")
        rows = db.query("""SELECT * FROM contacts WHERE last_outbound IS NOT NULL
                           AND (last_inbound IS NULL OR last_inbound < last_outbound)
                           AND last_outbound BETWEEN ? AND ? AND status != 'Inactive'""", (lo, hi))
        for c in rows:
            if db.query("SELECT 1 FROM tasks WHERE contact_id=? AND auto_rule='awaiting_reply' AND "
                        "(status='Open' OR created_at >= ?)", (c["id"], c["last_outbound"]), one=True):
                continue
            subj, conv, uid = _last(c["id"], "email_out")
            db.execute("""INSERT INTO tasks(title, contact_id, assigned_to, due_date, priority, auto_rule,
                          conversation_id, notes, created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                       (f"Follow up with {_name(c)} - no reply to \"{subj[:60]}\"", c["id"], uid or c["owner_id"],
                        today.date().isoformat(), "Normal", "awaiting_reply", conv,
                        f"Auto-created: no reply {n}+ days after the email on {c['last_outbound'][:10]}.",
                        db.now_iso()))
            stats.tasks_created += 1

    if s.get("rule_reply_needed") == "1":
        n = int(s.get("reply_days") or 2)
        lo = (today - timedelta(days=n + window)).isoformat(sep=" ")
        hi = (today - timedelta(days=n)).isoformat(sep=" ")
        rows = db.query("""SELECT * FROM contacts WHERE last_inbound IS NOT NULL
                           AND (last_outbound IS NULL OR last_outbound < last_inbound)
                           AND last_inbound BETWEEN ? AND ? AND needs_review = 0
                           AND status != 'Inactive'""", (lo, hi))
        for c in rows:
            if db.query("SELECT 1 FROM tasks WHERE contact_id=? AND auto_rule='reply_needed' AND "
                        "(status='Open' OR created_at >= ?)", (c["id"], c["last_inbound"]), one=True):
                continue
            subj, conv, uid = _last(c["id"], "email_in")
            db.execute("""INSERT INTO tasks(title, contact_id, assigned_to, due_date, priority, auto_rule,
                          conversation_id, notes, created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                       (f"Reply to {_name(c)} - \"{subj[:60]}\"", c["id"], uid or c["owner_id"],
                        today.date().isoformat(), "High", "reply_needed", conv,
                        f"Auto-created: their email on {c['last_inbound'][:10]} has no response yet.",
                        db.now_iso()))
            stats.tasks_created += 1
    return stats


# ===================================================================== sources
class LiveSource:
    """One team member's mailbox via Microsoft Graph."""

    def __init__(self, client):
        self.c = client

    def fetch(self, key, since_utc):
        from .graph import normalize
        if key == "inbox":
            for m in self.c.list_messages("inbox", since_utc, date_field="receivedDateTime"):
                yield normalize(m, "inbox")
        elif key == "sent":
            for m in self.c.list_messages("sentitems", since_utc, date_field="sentDateTime"):
                yield normalize(m, "sent")
        elif key == "capture":
            folder = self.c.find_folder(db.get_setting("capture_folder") or "CRM")
            if not folder:
                return
            for m in self.c.list_messages(folder, since_utc, date_field="receivedDateTime"):
                yield normalize(m, "capture")
        elif key == "shared":
            mbx = (db.get_setting("capture_mailbox") or "").strip()
            if not mbx:
                return
            for m in self.c.list_messages("inbox", since_utc, mailbox=mbx, date_field="receivedDateTime"):
                yield normalize(m, "capture")


def _default_since():
    lookback = int(db.get_setting("initial_lookback_days") or 30)
    d = (datetime.now(timezone.utc) - timedelta(days=lookback)).replace(microsecond=0)
    return d.isoformat().replace("+00:00", "Z")


def _run_source(src, jobs, stamps, stats, mailbox_user_id):
    """jobs: [(key, via)]; stamps: dict key->since. Returns updated stamps."""
    out = {}
    for key, via in jobs:
        since = stamps.get(key) or _default_since()
        newest = since
        for msg in src.fetch(key, since):
            try:
                process_message(msg, via=via, stats=stats, mailbox_user_id=mailbox_user_id)
            except Exception as e:  # keep going on bad messages
                log.exception("Failed to process message %s: %s", msg.get("subject"), e)
            if msg.get("date_utc") and msg["date_utc"] > (newest or ""):
                newest = msg["date_utc"]
        out[key] = newest
    return out


def _sync_user(u, stats, s):
    """Sync one team member's mailbox (live mode)."""
    from .graph import GraphClient
    client = GraphClient(u)
    jobs = []
    if s.get("sync_auto") == "1":
        jobs += [("inbox", "auto-sync"), ("sent", "auto-sync")]
    if s.get("sync_capture") == "1":
        jobs.append(("capture", "capture"))
    stamps = {"inbox": u["last_sync_inbox"], "sent": u["last_sync_sent"], "capture": u["last_sync_capture"]}
    new = _run_source(LiveSource(client), jobs, stamps, stats, u["id"])
    sets = {f"last_sync_{k}": v for k, v in new.items()}
    if sets:
        db.execute(f"UPDATE users SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), u["id"]))


def _sync_shared(stats, s):
    reader = db.user(int(s.get("capture_reader_id") or 0)) if (s.get("capture_reader_id") or "").isdigit() else None
    if not reader or not reader["ms_token_cache"]:
        return "shared capture mailbox skipped (no connected reader)"
    from .graph import GraphClient
    new = _run_source(LiveSource(GraphClient(reader)), [("shared", "capture")],
                      {"shared": s.get("last_sync_shared")}, stats, None)
    db.set_setting("last_sync_shared", new.get("shared", ""))
    return ""


def connected_users():
    return db.query("SELECT * FROM users WHERE active=1 AND is_demo=0 AND sync_enabled=1 "
                    "AND ms_token_cache IS NOT NULL AND ms_token_cache != ''")


def run_sync(trigger="manual", user_ids=None, include_shared=True):
    """Sync mailboxes. user_ids=None -> every connected team member."""
    if not _sync_lock.acquire(blocking=False):
        return {"status": "busy", "message": "A sync is already running - try again in a moment."}
    sync_state["running"] = True
    total = Stats()
    problems = []
    synced = 0
    try:
        s = db.get_settings()
        if s.get("mode") != "live":
            from .demo import DemoSource, demo_owner_id
            owner = demo_owner_id()
            log_id = db.execute("INSERT INTO sync_log(user_id, started_at, trigger) VALUES (?,?,?)",
                                (owner, db.now_iso(), trigger))
            st = Stats()
            jobs = [("inbox", "auto-sync"), ("sent", "auto-sync"), ("capture", "capture")]
            stamps = {k: s.get(f"last_sync_{k}") for k in ("inbox", "sent", "capture")}
            new = _run_source(DemoSource(), jobs, stamps, st, owner)
            for k, v in new.items():
                db.set_setting(f"last_sync_{k}", v)
            _finish_log(log_id, st, "ok", "Demo mailbox")
            total.add(st)
            synced = 1
        else:
            users = connected_users()
            if user_ids is not None:
                users = [u for u in users if u["id"] in user_ids]
            for u in users:
                st = Stats()
                log_id = db.execute("INSERT INTO sync_log(user_id, started_at, trigger) VALUES (?,?,?)",
                                    (u["id"], db.now_iso(), trigger))
                try:
                    _sync_user(u, st, s)
                    _finish_log(log_id, st, "ok", "")
                    db.execute("UPDATE users SET last_sync_at=?, last_sync_status='ok' WHERE id=?",
                               (db.now_iso(), u["id"]))
                    synced += 1
                except Exception as e:
                    log.exception("Sync failed for %s", u["email"])
                    problems.append(f"{u['name'] or u['email']}: {e}")
                    _finish_log(log_id, st, "error", str(e))
                    db.execute("UPDATE users SET last_sync_at=?, last_sync_status=? WHERE id=?",
                               (db.now_iso(), f"error: {e}"[:300], u["id"]))
                total.add(st)
            if include_shared and s.get("sync_capture") == "1" and s.get("capture_mailbox"):
                st = Stats()
                try:
                    note = _sync_shared(st, s)
                    if note:
                        problems.append(note)
                except Exception as e:
                    log.exception("Shared capture mailbox sync failed")
                    problems.append(f"Shared capture mailbox: {e}")
                total.add(st)
        rules = apply_followup_rules()
        total.tasks_created += rules.tasks_created
        status = "ok" if not problems else ("partial" if synced else "error")
        if not synced and s.get("mode") == "live" and not problems:
            message = "No connected mailboxes to sync yet - connect yours from My profile."
        else:
            message = (f"Scanned {total.scanned} emails, logged {total.logged}, {total.new_contacts} new contacts, "
                       f"{total.tasks_created} tasks created, {total.tasks_closed} auto-closed.")
        if problems:
            message += " Problems: " + "; ".join(problems)
    except Exception as e:
        log.exception("Sync failed")
        status, message = "error", str(e)
    finally:
        sync_state["running"] = False
        _sync_lock.release()
    sync_state["last_result"] = {"status": status, "message": message, **total.as_dict()}
    return sync_state["last_result"]


def _finish_log(log_id, st, status, message):
    db.execute("""UPDATE sync_log SET finished_at=?, scanned=?, logged=?, new_contacts=?, tasks_created=?,
                  tasks_closed=?, status=?, message=? WHERE id=?""",
               (db.now_iso(), st.scanned, st.logged, st.new_contacts, st.tasks_created, st.tasks_closed,
                status, message or (f"Scanned {st.scanned}, logged {st.logged}, {st.new_contacts} new contacts"),
                log_id))


class Scheduler(threading.Thread):
    """Background thread: syncs every connected mailbox on the configured interval,
    and takes a daily database backup."""
    daemon = True

    def __init__(self):
        super().__init__(name="relay-sync-scheduler")
        self._stop = threading.Event()

    def run(self):
        import time
        time.sleep(15)
        while not self._stop.is_set():
            try:
                if db.get_setting("mode") == "live" and connected_users():
                    run_sync("scheduled")
            except Exception:
                log.exception("Scheduled sync failed")
            try:
                from .backup import daily_backup
                daily_backup()
            except Exception:
                log.exception("Automatic backup failed")
            minutes = max(1, int(db.get_setting("sync_interval_min") or 10))
            self._stop.wait(minutes * 60)

    def stop(self):
        self._stop.set()
