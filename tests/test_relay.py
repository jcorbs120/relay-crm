import re
from datetime import datetime, timedelta, timezone

import pytest

from relay import classify, demo, importer
from relay.db import DB
from relay.graph import M365, normalize
from relay.sync import Engine, iso, run_sync

ME = "pat@relayconsulting.com"


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_DATA_DIR", str(tmp_path))
    d = DB(tmp_path / "t.db")
    d.set("my_email", ME)
    d.set("mode", "live")
    return d


def msg(i, frm, to, subj="Hello", hours_ago=1, body="hi", cc=()):
    return {"id": f"<m{i}@x>", "subject": subj, "from": (frm, ""), "to": [(t, "") for t in to],
            "cc": [(c, "") for c in cc], "date": iso(datetime.now(timezone.utc) - timedelta(hours=hours_ago)), "body": body}


# ---------- classification ----------
@pytest.mark.parametrize("domain,expected", [
    ("cisa.dhs.gov", "Federal"), ("va.gov", "Federal"), ("army.mil", "Federal"), ("ca.gov", "State"),
    ("dmv.ca.gov", "State"), ("michigan.gov", "State"), ("nyc.gov", "Local"), ("fairfaxcounty.gov", "Local"),
    ("state.tx.us", "State"), ("ci.austin.tx.us", "Local"), ("co.travis.tx.us", "Local"),
    ("austinisd.k12.tx.us", "Education"), ("gmu.edu", "Education"), ("deloitte.com", "Commercial"),
    ("redcross.org", "Nonprofit"), ("gmail.com", "Individual"),
])
def test_classify(domain, expected):
    assert classify.classify_domain(domain) == expected


def test_names_and_automated():
    assert classify.name_from_email("x@y.gov", "Smith, John (CISA)") == ("John", "Smith")
    assert classify.name_from_email("jane.doe2@y.gov") == ("Jane", "Doe")
    assert classify.is_automated("no-reply@sam.gov")
    assert classify.is_automated("news@public.govdelivery.com")
    assert not classify.is_automated("maria.lopez@cisa.dhs.gov")


# ---------- sync engine ----------
def test_outbound_creates_contact_and_org(db):
    e = Engine(db)
    assert e.process_message(msg(1, ME, ["sam.patel@ca.gov"]), "sent") == 1
    c = db.one("SELECT * FROM contacts WHERE email='sam.patel@ca.gov'")
    assert c["first_name"] == "Sam" and c["needs_review"] == 0 and c["source"] == "email-out"
    org = db.one("SELECT * FROM orgs WHERE id=?", (c["org_id"],))
    assert org["org_type"] == "State" and org["name"] == "State of California"
    assert c["last_contacted"]


def test_inbound_new_contact_flagged_for_review(db):
    Engine(db).process_message(msg(2, "daniel.okafor@dot.gov", [ME]))
    c = db.one("SELECT * FROM contacts WHERE email='daniel.okafor@dot.gov'")
    assert c["needs_review"] == 1 and c["source"] == "email-in"


def test_dedupe_same_message_twice(db):
    e = Engine(db)
    m = msg(3, "a.b@gsa.gov", [ME])
    e.process_message(m)
    e.process_message(m)
    assert db.val("SELECT count(*) FROM activities") == 1


def test_skips_me_colleagues_automated_and_respects_toggles(db):
    e = Engine(db)
    e.process_message(msg(4, ME, ["colleague@relayconsulting.com", "no-reply@sam.gov", "pat+crm@relayconsulting.com"]), "sent")
    assert db.val("SELECT count(*) FROM contacts") == 0
    db.set("auto_create_inbound", "0")
    e.process_message(msg(5, "new.person@epa.gov", [ME]))
    assert db.val("SELECT count(*) FROM contacts") == 0


def test_multiple_recipients_each_logged(db):
    Engine(db).process_message(msg(6, ME, ["a.one@va.gov"], cc=["b.two@va.gov"]), "sent")
    assert db.val("SELECT count(*) FROM activities") == 2
    assert db.val("SELECT count(DISTINCT org_id) FROM contacts") == 1


def test_forward_into_crm_folder_files_under_original_sender(db):
    body = ("FYI\n\n-----Original Message-----\nFrom: Tom Becker <tom.becker@fairfaxcounty.gov>\n"
            "Sent: Mon, 3 Nov 2025 09:12:00 -0500\nTo: pat@relayconsulting.com\nSubject: Budget\n\nCan we talk?")
    Engine(db).process_message({**msg(7, ME, ["pat+crm@relayconsulting.com"], subj="FW: Budget"), "body": body}, "crm")
    a = db.one("SELECT a.*, c.email FROM activities a JOIN contacts c ON c.id=a.contact_id")
    assert a["email"] == "tom.becker@fairfaxcounty.gov" and a["kind"] == "email_in" and a["subject"] == "Budget"
    assert a["occurred_at"].startswith("2025-11-03")


def test_lead_becomes_prospect_after_two_way_mail(db):
    e = Engine(db)
    e.process_message(msg(8, ME, ["k.w@cisa.dhs.gov"], hours_ago=5), "sent")
    assert db.val("SELECT lifecycle FROM contacts") == "Lead"
    e.process_message(msg(9, "k.w@cisa.dhs.gov", [ME], hours_ago=1))
    assert db.val("SELECT lifecycle FROM contacts") == "Prospect"


def test_followups_open_and_close(db):
    e = Engine(db)
    e.process_message(msg(10, ME, ["x.y@nasa.gov"], subj="Proposal", hours_ago=24 * 6), "sent")
    e.process_message(msg(11, "q.r@nasa.gov", [ME], subj="Question", hours_ago=24 * 3))
    st = e.check_followups()
    assert st["tasks_created"] == 2
    kinds = {r["kind"] for r in db.q("SELECT kind FROM tasks WHERE done=0")}
    assert kinds == {"awaiting_reply", "reply_needed"}
    assert e.check_followups()["tasks_created"] == 0          # not duplicated
    e.process_message(msg(12, "x.y@nasa.gov", [ME], subj="RE: Proposal", hours_ago=1))
    e.process_message(msg(13, ME, ["q.r@nasa.gov"], subj="RE: Question", hours_ago=1), "sent")
    assert db.val("SELECT count(*) FROM tasks WHERE done=0") == 0


def test_email_linked_to_open_opportunity(db):
    e = Engine(db)
    e.process_message(msg(14, ME, ["m.l@cisa.dhs.gov"]), "sent")
    cid = db.val("SELECT id FROM contacts")
    oid = db.ex("INSERT INTO opportunities(name, contact_id, stage, updated_at) VALUES('ZT', ?, 'Proposal', '2020')", (cid,))
    e.process_message(msg(15, "m.l@cisa.dhs.gov", [ME]))
    assert db.val("SELECT opp_id FROM activities WHERE message_id='<m15@x>'") == oid


def test_send_from_crm_then_sync_not_duplicated(db):
    e = Engine(db)
    now = iso(datetime.now(timezone.utc))
    e.process_message({"id": "local-abc", "subject": "Hi", "from": (ME, ""), "to": [("z.z@gsa.gov", "")], "date": now}, "sent")
    e.process_message({"id": "<real@x>", "subject": "Hi", "from": (ME, ""), "to": [("z.z@gsa.gov", "")], "date": now}, "sent")
    assert db.val("SELECT count(*) FROM activities") == 1
    assert db.val("SELECT message_id FROM activities") == "<real@x>"


def test_demo_end_to_end(db):
    demo.seed(db)
    res = run_sync(db, mailbox=demo.DemoMailbox(db))
    assert res["error"] is None
    assert res["contacts_created"] == 3 and res["logged"] == 5 and res["tasks_closed"] == 1
    again = run_sync(db, mailbox=demo.DemoMailbox(db))
    assert again["logged"] == 0 and again["contacts_created"] == 0


# ---------- Microsoft Graph (mocked HTTP) ----------
class FakeResp:
    def __init__(self, data, status=200):
        self._d, self.status_code, self.headers = data, status, {}
        self.content = b"x"
        self.text = str(data)

    def json(self):
        return self._d


def test_graph_listing_paging_and_send(db, monkeypatch):
    db.set("m365_tenant_id", "t"); db.set("m365_client_id", "c")
    g = M365(db)
    monkeypatch.setattr(g, "token", lambda: "TOKEN")
    calls = []
    page1 = {"value": [{"id": "1", "internetMessageId": "<a@x>", "subject": "S", "from": {"emailAddress": {"address": "A@B.GOV", "name": "A"}},
                        "toRecipients": [{"emailAddress": {"address": ME}}], "receivedDateTime": "2026-01-01T00:00:00Z",
                        "body": {"contentType": "text", "content": "hello"}}],
             "@odata.nextLink": "https://graph.microsoft.com/v1.0/next"}
    page2 = {"value": [{"id": "2", "isDraft": True}]}

    def fake(method, url, headers=None, timeout=None, **kw):
        calls.append((method, url, headers, kw))
        if url.endswith("/next"):
            return FakeResp(page2)
        if method == "POST" and url.endswith("/me/messages"):
            return FakeResp({"id": "D1", "internetMessageId": "<sent@x>"})
        if method == "POST":
            return FakeResp({}, 202)
        return FakeResp(page1)
    monkeypatch.setattr("relay.graph.requests.request", fake)
    msgs = list(g.list_messages("inbox", datetime(2026, 1, 1, tzinfo=timezone.utc)))
    assert len(msgs) == 1 and msgs[0]["from"] == ("a@b.gov", "A") and msgs[0]["id"] == "<a@x>"
    assert calls[0][2]["Authorization"] == "Bearer TOKEN"
    assert "receivedDateTime ge 2026-01-01T00:00:00Z" in calls[0][1]
    r = g.send(["x@y.gov"], "Subj", "Body")
    assert r["internet_message_id"] == "<sent@x>" and calls[-1][1].endswith("/me/messages/D1/send")


def test_gov_cloud_endpoints(db):
    db.set("m365_cloud", "gcchigh")
    g = M365(db)
    assert g.hosts == ("https://login.microsoftonline.us", "https://graph.microsoft.us")
    assert g.scopes[0] == "https://graph.microsoft.us/User.Read"
    db.set("m365_cloud", "dod")
    assert M365(db).hosts[1] == "https://dod-graph.microsoft.us"


def test_normalize_html_body():
    m = normalize({"id": "x", "body": {"contentType": "html", "content": "<p>Hi <b>there</b></p>"}})
    assert "Hi" in m["body"] and "<" not in m["body"]


# ---------- import ----------
def test_import_csv_auto_mapping_and_update(db):
    data = b"First Name,Last Name,E-mail Address,Company,Job Title\nJordan,Reyes,jordan.reyes@epa.gov,EPA Region 3,PM\n,,,,\n"
    headers, rows = importer.read_rows("x.csv", data)
    mapping = importer.guess_mapping(headers)
    assert mapping == ["first_name", "last_name", "email", "org", "title"]
    res = importer.run_import(db, headers, rows, mapping, tag="expo")
    assert res["created"] == 1
    res = importer.run_import(db, headers, [["Jordan", "Reyes", "jordan.reyes@epa.gov", "EPA Region 3", "Director"]], mapping)
    assert res["updated"] == 1 and db.val("SELECT title FROM contacts") == "Director"


def test_import_xlsx(db, tmp_path):
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active
    ws.append(["Name", "Email", "Agency"]); ws.append(["Lee Kim", "lee.kim@gsa.gov", "GSA"])
    p = tmp_path / "c.xlsx"; wb.save(p)
    headers, rows = importer.read_rows("c.xlsx", p.read_bytes())
    res = importer.run_import(db, headers, rows, importer.guess_mapping(headers))
    assert res["created"] == 1
    assert db.one("SELECT first_name, last_name FROM contacts")[:] == ("Lee", "Kim")


# ---------- web app ----------
def test_web_pages_and_write_flows(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_DATA_DIR", str(tmp_path))
    from relay.app import create_app
    from relay import reports
    app = create_app(DB(tmp_path / "w.db"), start_scheduler=False)
    c = app.test_client()
    H = {"Host": "127.0.0.1:5055"}
    r = c.get("/", headers=H)
    tok = re.search(rb'name="csrf" content="([^"]+)"', r.data).group(1).decode()
    for p in ["/contacts", "/orgs", "/pipeline", "/tasks", "/compose", "/reports", "/import", "/settings",
              "/contacts/1", "/opps/1", "/orgs/1", "/search?q=cisa"] + [f"/reports/{k}" for k, _, _ in reports.CATALOG]:
        assert c.get(p, headers=H).status_code == 200, p
    assert c.post("/contacts/new", data={"_csrf": tok, "first_name": "Ana", "last_name": "Diaz", "email": "ana.diaz@hhs.gov",
                                         "lifecycle": "Lead"}, headers=H).status_code == 302
    cid = app.extensions["relay_db"].val("SELECT id FROM contacts WHERE email='ana.diaz@hhs.gov'")
    assert c.post(f"/contacts/{cid}/activity", data={"_csrf": tok, "kind": "call", "subject": "Intro", "body": "ok",
                                                     "followup_days": "3"}, headers=H).status_code == 302
    assert c.post("/compose", data={"_csrf": tok, "contact_id": cid, "to": "ana.diaz@hhs.gov", "subject": "Hello",
                                    "body": "Hi", "followup_days": "5"}, headers=H).status_code == 302
    d = app.extensions["relay_db"]
    assert d.val("SELECT count(*) FROM activities WHERE contact_id=?", (cid,)) == 2
    assert d.val("SELECT count(*) FROM tasks WHERE contact_id=?", (cid,)) == 2
    assert c.post("/opps/1/stage", json={"stage": "Awarded"}, headers={**H, "X-CSRF": tok}).status_code == 200
    assert c.post("/sync", headers=H).status_code == 400            # no CSRF token
    assert c.get("/", headers={"Host": "attacker.example"}).status_code == 400
    assert c.get("/reports/pipeline?format=csv", headers=H).data.startswith("﻿Stage".encode())
    assert c.get("/backup", headers=H).status_code == 200
