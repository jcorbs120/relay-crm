"""Run with:  python -m pytest -q tests     (or: python -m unittest discover -s tests -v)"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

TMP = tempfile.mkdtemp()
os.environ["RELAY_DATA_DIR"] = TMP
os.environ["RELAY_DB"] = os.path.join(TMP, "test.db")
os.environ["RELAY_NO_SCHEDULER"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crm import db, sync, graph  # noqa: E402

ME = {"name": "Me", "email": "me@myfirm.com"}


def msg(i, frm, to, subject="Hello", body="Body", cc=None, days_ago=0, headers=None, conv=None):
    when = datetime.now() - timedelta(days=days_ago)
    return {"internet_message_id": f"<m{i}@x>", "conversation_id": conv or f"c{i}", "subject": subject,
            "body": body, "from": frm, "to": to, "cc": cc or [], "date": when.replace(microsecond=0).isoformat(sep=" "),
            "date_utc": when.isoformat() + "Z", "headers": headers or {}}


class Base(unittest.TestCase):
    def setUp(self):
        if os.path.exists(db.DB_PATH):
            os.remove(db.DB_PATH)
        db.init_db()
        db.set_setting("mode", "live")
        self.me_id = add_user("Me", ME["email"], "admin")


def add_user(name, email, role="user", **extra):
    cols = {"name": name, "email": email, "role": role, "created_at": db.now_iso(), **extra}
    return db.execute(f"INSERT INTO users({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", tuple(cols.values()))


class TestParsing(Base):
    def test_split_name_variants(self):
        self.assertEqual(sync.split_name("Smith, Jane (DHS)", "x@y.gov"), ("Jane", "Smith"))
        self.assertEqual(sync.split_name("Thompson, Kevin CTR (USA)", "k@y.gov"), ("Kevin", "Thompson"))
        self.assertEqual(sync.split_name("Jane Q. Public", "x@y.gov"), ("Jane", "Public"))
        self.assertEqual(sync.split_name("", "john.doe2@agency.gov"), ("John", "Doe"))
        self.assertEqual(sync.split_name("DOE, JOHN", "x@y.gov"), ("John", "Doe"))

    def test_guess_org(self):
        self.assertEqual(sync.guess_org("cisa.dhs.gov"), ("DHS", "Federal"))
        self.assertEqual(sync.guess_org("gsa.gov"), ("GSA", "Federal"))
        self.assertEqual(sync.guess_org("dir.texas.gov"), ("Texas - DIR", "State"))
        self.assertEqual(sync.guess_org("austintexas.gov"), ("Austintexas", "Local"))
        self.assertEqual(sync.guess_org("nyc.gov")[1], "Local")
        self.assertEqual(sync.guess_org("dot.state.tx.us")[1], "State")
        self.assertEqual(sync.guess_org("ci.austin.tx.us")[1], "Local")
        self.assertEqual(sync.guess_org("army.mil")[1], "Military / DoD")
        self.assertEqual(sync.guess_org("brightline-analytics.com"), ("Brightline Analytics", "Private Sector"))

    def test_parent_domain_matches_existing_org(self):
        oid = db.execute("INSERT INTO organizations(name, domain, org_type, created_at) VALUES ('DHS','dhs.gov','Federal',?)",
                         (db.now_iso(),))
        self.assertEqual(sync.get_or_create_org("cisa.dhs.gov"), oid)

    def test_public_domains_have_no_org(self):
        self.assertIsNone(sync.get_or_create_org("gmail.com"))


class TestProcess(Base):
    def test_outbound_creates_contacts_including_cc(self):
        st = sync.Stats()
        sync.process_message(msg(1, ME, [{"name": "Ann Lee", "email": "ann@agency.gov"}],
                                 cc=[{"name": "Bob Ray", "email": "bob@agency.gov"}]), stats=st)
        self.assertEqual(st.new_contacts, 2)
        ann = sync.find_contact("ann@agency.gov")
        self.assertEqual((ann["first_name"], ann["emails_out"], ann["needs_review"]), ("Ann", 1, 0))
        self.assertIsNotNone(ann["last_outbound"])

    def test_inbound_new_sender_flagged_for_review(self):
        sync.process_message(msg(2, {"name": "Cy Dee", "email": "cy@city.gov"}, [ME]))
        c = sync.find_contact("cy@city.gov")
        self.assertEqual((c["needs_review"], c["emails_in"], c["source"]), (1, 1, "Email - received"))

    def test_inbound_cc_strangers_not_created(self):
        sync.process_message(msg(3, {"name": "A", "email": "a@x.gov"}, [ME], cc=[{"name": "S", "email": "s@x.gov"}]))
        self.assertIsNone(sync.find_contact("s@x.gov"))

    def test_dedupe_same_message(self):
        m = msg(4, ME, [{"name": "", "email": "d@x.gov"}])
        sync.process_message(m)
        sync.process_message(m)
        self.assertEqual(sync.find_contact("d@x.gov")["emails_out"], 1)
        self.assertEqual(db.query("SELECT COUNT(*) n FROM activities WHERE kind='email_out'", one=True)["n"], 1)

    def test_internal_and_ignored(self):
        db.set_setting("internal_domains", "partnerco.com")
        sync.process_message(msg(5, ME, [{"name": "", "email": "colleague@myfirm.com"},
                                         {"name": "", "email": "x@partnerco.com"},
                                         {"name": "", "email": "noreply@vendor.com"}]))
        self.assertEqual(db.query("SELECT COUNT(*) n FROM contacts", one=True)["n"], 0)

    def test_bulk_headers_skipped(self):
        sync.process_message(msg(6, {"name": "News", "email": "editor@site.com"}, [ME],
                                 headers={"list-unsubscribe": "<mailto:u@site.com>"}))
        self.assertIsNone(sync.find_contact("editor@site.com"))

    def test_autocreate_modes(self):
        db.set_setting("autocreate", "outbound")
        sync.process_message(msg(7, {"name": "", "email": "in@x.gov"}, [ME]))
        self.assertIsNone(sync.find_contact("in@x.gov"))
        db.set_setting("autocreate", "off")
        sync.process_message(msg(8, ME, [{"name": "", "email": "out@x.gov"}]))
        self.assertIsNone(sync.find_contact("out@x.gov"))

    def test_auto_promote_two_way(self):
        sync.process_message(msg(9, ME, [{"name": "", "email": "p@x.gov"}], days_ago=2))
        self.assertEqual(sync.find_contact("p@x.gov")["status"], "Lead")
        sync.process_message(msg(10, {"name": "", "email": "p@x.gov"}, [ME], days_ago=1))
        self.assertEqual(sync.find_contact("p@x.gov")["status"], "Prospect")

    def test_links_single_open_opportunity(self):
        sync.process_message(msg(11, ME, [{"name": "", "email": "o@x.gov"}], days_ago=3))
        c = sync.find_contact("o@x.gov")
        now = db.now_iso()
        oid = db.execute("INSERT INTO opportunities(name, contact_id, org_id, stage, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                         ("Deal", c["id"], c["org_id"], "Proposal", now, now))
        sync.process_message(msg(12, {"name": "", "email": "o@x.gov"}, [ME]))
        a = db.query("SELECT opp_id FROM activities WHERE message_id='<m12@x>'", one=True)
        self.assertEqual(a["opp_id"], oid)


class TestForward(Base):
    def test_unwrap_outlook_forward(self):
        body = ("please log\n\n________________________________\nFrom: Rachel Ortiz <rachel.ortiz@usda.gov>\n"
                "Sent: Monday, March 3, 2025 9:14 AM\nTo: Me <me@myfirm.com>\nCc: Sam Lo <sam.lo@usda.gov>\n"
                "Subject: Teaming question\n\nAre you bidding?")
        m = msg(20, ME, [ME], subject="FW: Teaming question", body=body)
        st = sync.Stats()
        sync.process_message(m, via="capture", stats=st)
        c = sync.find_contact("rachel.ortiz@usda.gov")
        self.assertIsNotNone(c)
        self.assertEqual(c["source"], "Email - forwarded")
        a = db.query("SELECT * FROM activities WHERE contact_id=? AND kind='email_in'", (c["id"],), one=True)
        self.assertEqual(a["subject"], "Teaming question")
        self.assertTrue(a["occurred_at"].startswith("2025-03-03 09:14"))

    def test_bcc_capture_logs_outbound(self):
        m = msg(21, ME, [{"name": "Wei Chen", "email": "wei@nyc.gov"}])
        sync.process_message(m, via="capture")
        self.assertEqual(sync.find_contact("wei@nyc.gov")["emails_out"], 1)


class TestRules(Base):
    def test_awaiting_reply_created_then_closed(self):
        sync.process_message(msg(30, ME, [{"name": "", "email": "r@x.gov"}], days_ago=6))
        st = sync.apply_followup_rules()
        self.assertEqual(st.tasks_created, 1)
        self.assertEqual(sync.apply_followup_rules().tasks_created, 0)  # idempotent
        st2 = sync.Stats()
        sync.process_message(msg(31, {"name": "", "email": "r@x.gov"}, [ME]), stats=st2)
        self.assertEqual(st2.tasks_closed, 1)

    def test_reply_needed(self):
        sync.process_message(msg(32, {"name": "", "email": "q@x.gov"}, [ME], days_ago=3))
        db.execute("UPDATE contacts SET needs_review=0")
        self.assertEqual(sync.apply_followup_rules().tasks_created, 1)
        st = sync.Stats()
        sync.process_message(msg(33, ME, [{"name": "", "email": "q@x.gov"}]), stats=st)
        self.assertEqual(st.tasks_closed, 1)


class FakeGraph(graph.GraphClient):
    """GraphClient with HTTP replaced by canned Graph JSON (tests paging + normalisation)."""
    def __init__(self, pages):
        self.pages = pages
        self.graph = "https://graph.microsoft.com"
        self.capture_mailbox = ""
        self.calls = []

    def _req(self, method, path, **kw):
        self.calls.append(path)
        if "mailFolders?$filter" in path:
            return {"value": [{"id": "CRMFOLDER"}]}
        folder = path.split("/mailFolders/")[1].split("/")[0]
        page = self.pages.get((folder, len([c for c in self.calls if f"/mailFolders/{folder}/" in c or c.startswith("next:" + folder)])))
        return page or {"value": []}


def graph_msg(i, frm, to, when, folder_dt="receivedDateTime"):
    return {"id": f"g{i}", "internetMessageId": f"<g{i}@outlook>", "conversationId": "conv",
            "subject": f"Graph {i}", "bodyPreview": "hi", "body": {"contentType": "text", "content": "hello"},
            "from": {"emailAddress": {"name": frm[0], "address": frm[1]}},
            "toRecipients": [{"emailAddress": {"name": n, "address": a}} for n, a in to],
            "ccRecipients": [], folder_dt: when, "isDraft": False, "internetMessageHeaders": []}


class TestLiveSource(Base):
    def test_live_sync_with_paging(self):
        now = datetime.utcnow().replace(microsecond=0)
        t1 = (now - timedelta(hours=2)).isoformat() + "Z"
        t2 = (now - timedelta(hours=1)).isoformat() + "Z"
        pages = {
            ("inbox", 1): {"value": [graph_msg(1, ("Lee, Pat (GSA)", "pat.lee@gsa.gov"), [("Me", ME["email"])], t1)],
                           "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?skip=1"},
            ("inbox", 2): {"value": [graph_msg(2, ("Pat Lee", "pat.lee@gsa.gov"), [("Me", ME["email"])], t2)]},
            ("sentitems", 1): {"value": [graph_msg(3, ("Me", ME["email"]), [("Kim Ho", "kim.ho@state.va.us")], t2, "sentDateTime")]},
        }
        fake = FakeGraph(pages)
        db.execute("UPDATE users SET ms_token_cache='x', ms_email=? WHERE id=?", (ME["email"], self.me_id))
        real = graph.GraphClient
        graph.GraphClient = lambda u: fake
        try:
            res = sync.run_sync("test")
        finally:
            graph.GraphClient = real
        self.assertEqual(res["status"], "ok", res)
        pat = sync.find_contact("pat.lee@gsa.gov")
        self.assertEqual((pat["first_name"], pat["last_name"], pat["emails_in"]), ("Pat", "Lee", 2))
        self.assertEqual(sync.find_contact("kim.ho@state.va.us")["emails_out"], 1)
        self.assertEqual(db.user(self.me_id)["last_sync_inbox"], t2)      # bookmark is per person
        log = db.query("SELECT * FROM sync_log ORDER BY id DESC", one=True)
        self.assertEqual((log["user_id"], log["status"]), (self.me_id, "ok"))


class TestTeam(Base):
    """Several people share one CRM: attribution, ownership and colleague handling."""

    def setUp(self):
        super().setUp()
        self.bob = add_user("Bob Ray", "bob@myfirm.com", aliases="robert@myfirm-alt.com")
        self.BOB = {"name": "Bob Ray", "email": "bob@myfirm.com"}

    def test_colleagues_never_become_contacts(self):
        sync.process_message(msg(40, ME, [self.BOB, {"name": "", "email": "robert@myfirm-alt.com"},
                                          {"name": "Ext", "email": "ext@agency.gov"}]), mailbox_user_id=self.me_id)
        emails = {r["email"] for r in db.query("SELECT email FROM contacts")}
        self.assertEqual(emails, {"ext@agency.gov"})

    def test_first_corresponder_owns_contact_and_activity_attributed(self):
        sync.process_message(msg(41, self.BOB, [{"name": "Gov Person", "email": "gp@agency.gov"}]),
                             mailbox_user_id=self.bob)
        c = sync.find_contact("gp@agency.gov")
        self.assertEqual(c["owner_id"], self.bob)
        # later, I also email them - the owner stays Bob, but my email is attributed to me
        sync.process_message(msg(42, ME, [{"name": "", "email": "gp@agency.gov"}]), mailbox_user_id=self.me_id)
        self.assertEqual(sync.find_contact("gp@agency.gov")["owner_id"], self.bob)
        who = {r["message_id"]: r["user_id"] for r in db.query("SELECT message_id, user_id FROM activities WHERE message_id IS NOT NULL")}
        self.assertEqual(who, {"<m41@x>": self.bob, "<m42@x>": self.me_id})

    def test_same_email_in_two_mailboxes_logged_once(self):
        m = msg(43, ME, [self.BOB, {"name": "", "email": "x@agency.gov"}])
        sync.process_message(m, mailbox_user_id=self.me_id)          # my Sent Items
        sync.process_message(m, mailbox_user_id=self.bob)            # Bob's Inbox (he was on To)
        self.assertEqual(db.query("SELECT COUNT(*) n FROM activities WHERE kind='email_out'", one=True)["n"], 1)
        self.assertEqual(sync.find_contact("x@agency.gov")["emails_out"], 1)

    def test_inbound_attributed_to_mailbox_owner(self):
        sync.process_message(msg(44, {"name": "", "email": "in@agency.gov"}, [self.BOB]), mailbox_user_id=self.bob)
        c = sync.find_contact("in@agency.gov")
        self.assertEqual(c["owner_id"], self.bob)
        a = db.query("SELECT user_id FROM activities WHERE kind='email_in'", one=True)
        self.assertEqual(a["user_id"], self.bob)

    def test_shared_capture_attributes_to_teammate_on_message(self):
        db.set_setting("capture_mailbox", "crm@myfirm.com")
        sync.process_message(msg(45, self.BOB, [{"name": "", "email": "cap@agency.gov"}],
                                 cc=[{"name": "CRM", "email": "crm@myfirm.com"}]), via="capture")
        c = sync.find_contact("cap@agency.gov")
        self.assertEqual(c["owner_id"], self.bob)
        self.assertIsNone(sync.find_contact("crm@myfirm.com"))

    def test_forward_by_colleague_credits_them(self):
        body = ("FYI\n\n-----Original Message-----\nFrom: Dana Fox <dana.fox@va.gov>\nSent: Tuesday, April 1, 2025 10:00 AM\n"
                "To: Bob Ray <bob@myfirm.com>\nSubject: RFI\n\nQuestion about the RFI")
        sync.process_message(msg(46, self.BOB, [ME], subject="FW: RFI", body=body), via="capture",
                             mailbox_user_id=self.me_id)
        c = sync.find_contact("dana.fox@va.gov")
        self.assertIsNotNone(c)
        self.assertEqual(c["owner_id"], self.bob)

    def test_deactivated_colleague_still_not_a_contact(self):
        db.execute("UPDATE users SET active=0 WHERE id=?", (self.bob,))
        sync.process_message(msg(47, ME, [self.BOB]), mailbox_user_id=self.me_id)
        self.assertIsNone(sync.find_contact("bob@myfirm.com"))

    def test_awaiting_reply_task_assigned_to_sender(self):
        sync.process_message(msg(48, self.BOB, [{"name": "", "email": "slow@agency.gov"}], days_ago=6),
                             mailbox_user_id=self.bob)
        sync.apply_followup_rules()
        t = db.query("SELECT * FROM tasks WHERE auto_rule<>''", one=True)
        self.assertIsNotNone(t)
        self.assertEqual(t["assigned_to"], self.bob)


if __name__ == "__main__":
    unittest.main()
