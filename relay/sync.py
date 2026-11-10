"""Email -> CRM engine.

process_message() takes one normalized email and:
  * works out direction (me -> them = out, them -> me = in),
  * unpacks forwards sent to the CRM folder so they file under the original sender,
  * finds or creates each counterpart contact (+ organization from the domain),
  * logs the email on each contact's timeline (never twice),
  * links it to the contact's open opportunity,
  * updates last-contacted, moves Lead -> Prospect when mail has gone both ways,
  * closes the matching automatic follow-up task.
check_followups() then opens "Awaiting reply" / "Reply needed" tasks.
"""
import logging
import re
import threading
from datetime import datetime, timedelta, timezone

from . import classify
from .db import now_iso

log = logging.getLogger("relay.sync")

OPEN_STAGES = ("Lead", "Qualified", "Proposal", "Submitted")
FWD_SUBJECT = re.compile(r"^\s*(fw|fwd)\s*:", re.I)
FWD_FROM = re.compile(r"^\s*\*?From:\*?\s*(?:\"?([^\"<\n]*?)\"?\s*)?<?([\w.+'-]+@[\w-]+(?:\.[\w-]+)+)>?", re.I | re.M)
FWD_SENT = re.compile(r"^\s*\*?(?:Sent|Date):\*?\s*(.+)$", re.I | re.M)


def parse_dt(s):
    if not s:
        return datetime.now(timezone.utc)
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    s = s.replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return datetime.now(timezone.utc)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).replace(microsecond=0).isoformat()


class Engine:
    def __init__(self, db):
        self.db = db

    # ------------------------------------------------------------ identity
    def my_addresses(self):
        mine = {self.db.get("my_email").strip().lower()}
        for a in re.split(r"[,;\s]+", self.db.get("aliases", "")):
            if a.strip():
                mine.add(a.strip().lower())
        mine.discard("")
        return mine

    def my_domains(self):
        return {classify.split_email(a)[1] for a in self.my_addresses() if "@" in a}

    def is_me(self, addr):
        addr = (addr or "").lower()
        if addr in self.my_addresses():
            return True
        # plus-addressing: you+crm@agency.gov is still you
        local, dom = classify.split_email(addr)
        if "+" in local:
            return f"{local.split('+')[0]}@{dom}" in self.my_addresses()
        return False

    def should_track(self, addr):
        if not addr or "@" not in addr or self.is_me(addr):
            return False
        if classify.is_automated(addr):
            return False
        if self.db.get("skip_internal") == "1" and classify.split_email(addr)[1] in self.my_domains():
            return False
        return True

    # ------------------------------------------------------------ records
    def get_or_create_org(self, domain):
        if not domain or domain in classify.FREE_MAIL:
            return None
        base = classify.base_domain(domain)
        r = self.db.one("SELECT id FROM orgs WHERE domain=?", (base,))
        if r:
            return r["id"]
        # also match a parent domain the user may have entered by hand
        r = self.db.one("SELECT id FROM orgs WHERE ? LIKE '%.' || domain", (base,))
        if r:
            return r["id"]
        return self.db.ex(
            "INSERT INTO orgs(name, domain, org_type, created_at) VALUES(?,?,?,?)",
            (classify.org_name_for_domain(base), base, classify.classify_domain(base), now_iso()))

    def get_or_create_contact(self, addr, name, direction, stats):
        r = self.db.one("SELECT id FROM contacts WHERE email=?", (addr,))
        if r:
            return r["id"]
        if direction == "out" and self.db.get("auto_create_outbound", "1") != "1":
            return None
        if direction == "in" and self.db.get("auto_create_inbound", "1") != "1":
            return None
        first, last = classify.name_from_email(addr, name)
        org_id = self.get_or_create_org(classify.split_email(addr)[1])
        cid = self.db.ex(
            "INSERT INTO contacts(first_name, last_name, email, org_id, lifecycle, needs_review, source, created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (first, last, addr, org_id, "Lead", 1 if direction == "in" else 0,
             "email-out" if direction == "out" else "email-in", now_iso()))
        stats["contacts_created"] += 1
        log.info("created contact %s (%s)", addr, direction)
        return cid

    def open_opp_for(self, contact_id):
        r = self.db.one(
            f"SELECT id FROM opportunities WHERE contact_id=? AND stage IN ({','.join('?'*len(OPEN_STAGES))})"
            " ORDER BY updated_at DESC LIMIT 1", (contact_id, *OPEN_STAGES))
        if r:
            return r["id"]
        r = self.db.one(
            f"SELECT o.id FROM opportunities o JOIN contacts c ON c.org_id=o.org_id WHERE c.id=? AND o.org_id IS NOT NULL"
            f" AND o.stage IN ({','.join('?'*len(OPEN_STAGES))}) ORDER BY o.updated_at DESC LIMIT 1",
            (contact_id, *OPEN_STAGES))
        return r["id"] if r else None

    # ------------------------------------------------------------ core
    def unpack_forward(self, msg):
        """If I forwarded someone's email into the CRM, return (addr, name, date) of the original sender."""
        if not FWD_SUBJECT.match(msg.get("subject") or ""):
            return None
        body = msg.get("body") or msg.get("preview") or ""
        for m in FWD_FROM.finditer(body):
            addr = m.group(2).lower()
            if not self.is_me(addr):
                sent = FWD_SENT.search(body, m.end())
                when = None
                if sent:
                    try:
                        from email.utils import parsedate_to_datetime
                        when = parsedate_to_datetime(sent.group(1).strip())
                    except Exception:
                        when = None
                return addr, (m.group(1) or "").strip(), when
        return None

    def process_message(self, msg, folder="inbox", stats=None):
        """Returns number of timeline entries written."""
        stats = stats if stats is not None else new_stats()
        mid = msg.get("id")
        if not mid:
            return 0
        from_addr, from_name = msg.get("from") or ("", "")
        when = parse_dt(msg.get("date"))
        subject = (msg.get("subject") or "(no subject)").strip()
        text = (msg.get("body") or msg.get("preview") or "").strip()

        counterparts = []   # (addr, name)
        if self.is_me(from_addr):
            direction = "out"
            fwd = self.unpack_forward(msg) if folder == "crm" else None
            if fwd:
                direction = "in"
                counterparts = [(fwd[0], fwd[1])]
                if fwd[2]:
                    when = parse_dt(fwd[2])
                subject = FWD_SUBJECT.sub("", subject).strip() or subject
            else:
                counterparts = list(msg.get("to") or []) + list(msg.get("cc") or []) + list(msg.get("bcc") or [])
        else:
            direction = "in"
            counterparts = [(from_addr, from_name)]

        written = 0
        seen = set()
        for addr, name in counterparts:
            addr = (addr or "").lower().strip()
            if addr in seen or not self.should_track(addr):
                continue
            seen.add(addr)
            cid = self.get_or_create_contact(addr, name, direction, stats)
            if not cid:
                continue
            if self.db.one("SELECT 1 FROM activities WHERE message_id=? AND contact_id=?", (mid, cid)):
                continue
            # Same email logged moments ago by "send from CRM" under a temporary id?
            dup = self.db.one(
                "SELECT id FROM activities WHERE contact_id=? AND kind=? AND subject=? AND message_id LIKE 'local-%'"
                " AND abs(julianday(occurred_at) - julianday(?)) < 0.02",
                (cid, "email_" + direction, subject, iso(when)))
            if dup:
                self.db.ex("UPDATE activities SET message_id=? WHERE id=?", (mid, dup["id"]))
                continue
            self.log_email(cid, direction, subject, text, when, mid, msg.get("conversation_id"), stats)
            written += 1
        stats["logged"] += written
        return written

    def log_email(self, cid, direction, subject, text, when, mid, conv, stats):
        opp = self.open_opp_for(cid)
        self.db.ex(
            "INSERT OR IGNORE INTO activities(contact_id, opp_id, kind, subject, body, occurred_at, message_id,"
            " conversation_id, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (cid, opp, "email_" + direction, subject[:500], text[:8000], iso(when), mid, conv, now_iso()))
        self.db.ex("UPDATE contacts SET last_contacted=? WHERE id=? AND (last_contacted IS NULL OR last_contacted < ?)",
                   (iso(when), cid, iso(when)))
        if opp:
            self.db.ex("UPDATE opportunities SET updated_at=? WHERE id=?", (now_iso(), opp))
        # Lead -> Prospect once mail has gone both ways
        kinds = {r["kind"] for r in self.db.q(
            "SELECT DISTINCT kind FROM activities WHERE contact_id=? AND kind LIKE 'email_%'", (cid,))}
        if {"email_in", "email_out"} <= kinds:
            self.db.ex("UPDATE contacts SET lifecycle='Prospect' WHERE id=? AND lifecycle='Lead'", (cid,))
        # Close the follow-up this email answers
        close_kind = "awaiting_reply" if direction == "in" else "reply_needed"
        for t in self.db.q("SELECT id FROM tasks WHERE contact_id=? AND kind=? AND done=0", (cid, close_kind)):
            self.db.ex("UPDATE tasks SET done=1, completed_at=? WHERE id=?", (now_iso(), t["id"]))
            stats["tasks_closed"] += 1

    def check_followups(self, stats=None, now=None):
        stats = stats if stats is not None else new_stats()
        now = now or datetime.now(timezone.utc)
        wait_days = int(self.db.get("awaiting_reply_days", "5") or 5)
        reply_days = int(self.db.get("reply_needed_days", "2") or 2)
        rows = self.db.q("""
            SELECT a.contact_id, a.kind, a.subject, a.occurred_at, a.opp_id,
                   c.first_name, c.last_name, c.email
            FROM activities a JOIN contacts c ON c.id=a.contact_id
            WHERE a.kind IN ('email_in','email_out') AND a.occurred_at = (
                SELECT max(occurred_at) FROM activities b
                WHERE b.contact_id=a.contact_id AND b.kind IN ('email_in','email_out','call','meeting'))
              AND c.lifecycle != 'Inactive'
        """)
        for r in rows:
            last = parse_dt(r["occurred_at"])
            who = (f"{r['first_name']} {r['last_name']}".strip() or r["email"])
            if r["kind"] == "email_out":
                kind, days, title = "awaiting_reply", wait_days, f"Awaiting reply from {who}: {r['subject']}"
            else:
                kind, days, title = "reply_needed", reply_days, f"Reply needed to {who}: {r['subject']}"
            if now - last < timedelta(days=days):
                continue
            if last < now - timedelta(days=60):
                continue  # too old to nag about
            exists = self.db.one("SELECT 1 FROM tasks WHERE contact_id=? AND kind=? AND (done=0 OR created_at > ?)",
                                 (r["contact_id"], kind, r["occurred_at"]))
            if exists:
                continue
            self.db.ex("INSERT INTO tasks(contact_id, opp_id, title, due_date, kind, priority, created_at)"
                       " VALUES(?,?,?,?,?,?,?)",
                       (r["contact_id"], r["opp_id"], title[:300], now.date().isoformat(), kind,
                        "High" if kind == "reply_needed" else "Normal", now_iso()))
            stats["tasks_created"] += 1
        return stats


def new_stats():
    return {"fetched": 0, "logged": 0, "contacts_created": 0, "tasks_created": 0, "tasks_closed": 0}


# ---------------------------------------------------------------- runners
_run_lock = threading.Lock()


def run_sync(db, m365=None, mailbox=None):
    """One sync pass. mailbox: a demo mailbox object; otherwise uses Microsoft 365."""
    if not _run_lock.acquire(blocking=False):
        return {"error": "A sync is already running."}
    eng = Engine(db)
    stats = new_stats()
    started = now_iso()
    mode = "demo" if mailbox else "live"
    err = None
    try:
        last = db.get("last_sync_at")
        if last:
            since = parse_dt(last) - timedelta(hours=2)   # overlap; dedupe makes this safe
        else:
            since = datetime.now(timezone.utc) - timedelta(days=int(db.get("first_sync_days", "30") or 30))
        if mailbox:
            sources = mailbox.fetch(since)
        else:
            sources = _live_sources(db, m365, since)
        sources = sorted(sources, key=lambda fm: parse_dt(fm[1].get("date")))  # oldest first
        for folder, msg in sources:
            stats["fetched"] += 1
            try:
                eng.process_message(msg, folder, stats)
            except Exception as e:  # one bad message never stops the sync
                log.exception("message failed: %s", msg.get("id"))
                err = f"Some messages were skipped: {e}"
        eng.check_followups(stats)
        db.set("last_sync_at", started)
    except Exception as e:
        log.exception("sync failed")
        err = str(e)
    finally:
        db.ex("INSERT INTO sync_log(started_at, finished_at, mode, fetched, logged, contacts_created, tasks_created,"
              " tasks_closed, error) VALUES(?,?,?,?,?,?,?,?,?)",
              (started, now_iso(), mode, stats["fetched"], stats["logged"], stats["contacts_created"],
               stats["tasks_created"], stats["tasks_closed"], err))
        _run_lock.release()
    stats["error"] = err
    return stats


def _live_sources(db, m365, since):
    if not db.get("my_email"):
        try:
            me = m365.me()
            db.set("my_email", (me.get("mail") or me.get("userPrincipalName") or "").lower())
        except Exception:
            pass
    for folder in ("inbox", "sentitems"):
        for m in m365.list_messages(folder, since):
            yield ("sent" if folder == "sentitems" else "inbox"), m
    fid = m365.find_folder(db.get("crm_folder", "CRM"))
    if fid:
        for m in m365.list_messages(fid, since):
            yield "crm", m


class Scheduler:
    """Background auto-sync every N minutes (live mode only)."""

    def __init__(self, db, m365):
        self.db, self.m365 = db, m365
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="relay-sync")

    def start(self):
        self.thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        self._stop.wait(20)
        while not self._stop.is_set():
            try:
                if self.db.get("mode") == "live" and self.m365.configured() and self.m365.account():
                    run_sync(self.db, self.m365)
            except Exception:
                log.exception("scheduled sync failed")
            mins = max(1, int(self.db.get("sync_minutes", "5") or 5))
            self._stop.wait(mins * 60)
