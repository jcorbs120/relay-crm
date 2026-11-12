"""Web, security and team tests (Flask test client, no network).

    python -m pytest -q tests
"""
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest

TMP = tempfile.mkdtemp()
os.environ.setdefault("RELAY_DATA_DIR", TMP)
os.environ["RELAY_NO_SCHEDULER"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crm import auth, db, reports  # noqa: E402
from crm.app import create_app  # noqa: E402

PW = "Str0ng-Passphrase!"


class WebBase(unittest.TestCase):
    seed_demo = False

    def setUp(self):
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(db.DB_PATH + ext):
                os.remove(db.DB_PATH + ext)
        db.init_db()
        db.set_setting("mode", "demo" if self.seed_demo else "live")
        self.app = create_app(False)
        self.app.config["TESTING"] = True
        self.admin = self.client()
        r = self.post(self.admin, "/setup", name="Ann Admin", email="ann@firm.com", firm_name="Acme",
                      password=PW, password2=PW)
        self.assertEqual(r.status_code, 302, r.data[:400])
        self.ann = db.user_by_email("ann@firm.com")["id"]

    # helpers ----------------------------------------------------------
    def client(self):
        c = self.app.test_client()
        c.get("/login" if db.user_count() else "/setup")     # establishes a CSRF token in the session
        return c

    @staticmethod
    def token(c):
        with c.session_transaction() as s:
            return s.get("csrf", "")

    def post(self, c, url, _csrf=True, **data):
        if _csrf:
            data.setdefault("csrf_token", self.token(c))
        return c.post(url, data=data)

    def make_user(self, name, email, role="user"):
        uid = db.execute("INSERT INTO users(email, name, role, password_hash, created_at) VALUES (?,?,?,?,?)",
                         (email, name, role, auth.hash_password(PW), db.now_iso()))
        return uid

    def login(self, email, pw=PW):
        c = self.client()
        r = self.post(c, "/login", email=email, password=pw)
        return c, r


class TestSetupAndLogin(WebBase):
    def test_setup_only_once(self):
        r = self.client().get("/setup")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])
        self.assertEqual(db.user(self.ann)["role"], "admin")

    def test_anonymous_redirected_to_login(self):
        r = self.app.test_client().get("/contacts")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])

    def test_ajax_anonymous_gets_401(self):
        r = self.app.test_client().get("/contacts", headers={"X-Requested-With": "fetch"})
        self.assertEqual(r.status_code, 401)

    def test_wrong_password_and_lockout(self):
        self.make_user("Bob", "bob@firm.com")
        for _ in range(auth.MAX_FAILED):
            _, r = self.login("bob@firm.com", "nope")
            self.assertIn(r.status_code, (401, 429))
        _, r = self.login("bob@firm.com")          # right password, but now locked
        self.assertEqual(r.status_code, 429)
        self.assertTrue(db.query("SELECT 1 FROM audit_log WHERE action='login_failed'", one=True))

    def test_deactivated_user_cannot_login_and_is_kicked_out(self):
        uid = self.make_user("Bob", "bob@firm.com")
        c, r = self.login("bob@firm.com")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(c.get("/contacts").status_code, 200)
        self.post(self.admin, f"/admin/users/{uid}", name="Bob", email="bob@firm.com", role="user")  # active unchecked
        self.assertEqual(db.user(uid)["active"], 0)
        self.assertEqual(c.get("/contacts").status_code, 302)        # existing session ends
        _, r = self.login("bob@firm.com")
        self.assertEqual(r.status_code, 401)

    def test_password_policy(self):
        self.assertIsNotNone(auth.password_problem("short"))
        self.assertIsNotNone(auth.password_problem("alllowercaseletters"[:12]))
        self.assertIsNone(auth.password_problem(PW))
        self.assertIsNone(auth.password_problem("a long simple passphrase"))

    def test_open_redirect_blocked(self):
        self.make_user("Bob", "bob@firm.com")
        c = self.client()
        r = c.post("/login?next=https://evil.example/x", data={"email": "bob@firm.com", "password": PW,
                                                               "csrf_token": self.token(c)})
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("evil.example", r.headers["Location"])

    def test_temp_password_forces_change(self):
        r = self.post(self.admin, "/admin/users/new", name="Cy New", email="cy@firm.com", role="user", signin="temp")
        self.assertEqual(r.status_code, 302)
        page = self.admin.get("/admin/users").data.decode()
        temp = re.search(r"shown only once\): (\S+)", page).group(1)
        c, r = self.login("cy@firm.com", temp)
        self.assertEqual(r.status_code, 302)
        r = c.get("/contacts")
        self.assertIn("/profile", r.headers["Location"])
        self.post(c, "/profile/password", new=PW, new2=PW)
        self.assertEqual(db.user_by_email("cy@firm.com")["must_change_password"], 0)
        self.assertEqual(c.get("/contacts").status_code, 200)


class TestCsrfAndRoles(WebBase):
    def test_post_without_csrf_rejected(self):
        r = self.post(self.admin, "/contacts/new", _csrf=False, first_name="X", email="x@a.gov")
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(db.query("SELECT 1 FROM contacts WHERE email='x@a.gov'", one=True))
        r = self.admin.post("/tasks/new", data={"title": "t"}, headers={"X-Requested-With": "fetch"})
        self.assertEqual(r.status_code, 400)

    def test_csrf_header_accepted(self):
        r = self.admin.post("/contacts/new", data={"first_name": "Y", "email": "y@a.gov"},
                            headers={"X-CSRF-Token": self.token(self.admin)})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(db.query("SELECT 1 FROM contacts WHERE email='y@a.gov'", one=True))

    def test_user_cannot_reach_admin(self):
        self.make_user("Bob", "bob@firm.com")
        c, _ = self.login("bob@firm.com")
        for url in ("/admin/users", "/admin/settings", "/admin/audit", "/admin/backups", "/admin/backup"):
            self.assertEqual(c.get(url).status_code, 403, url)
        r = self.post(c, "/admin/users/new", name="Evil", email="evil@firm.com", role="admin")
        self.assertEqual(r.status_code, 403)
        self.assertIsNone(db.user_by_email("evil@firm.com"))

    def test_viewer_is_read_only(self):
        self.make_user("Val", "val@firm.com", "viewer")
        cid = db.execute("INSERT INTO contacts(first_name, email, created_at, updated_at) VALUES ('A','a@b.gov',?,?)",
                         (db.now_iso(), db.now_iso()))
        c, _ = self.login("val@firm.com")
        for url in ("/", "/contacts", f"/contacts/{cid}", "/pipeline", "/tasks", "/reports", "/reports/pipeline", "/profile"):
            self.assertEqual(c.get(url).status_code, 200, url)
        for url in ("/contacts/new", f"/contacts/{cid}/edit", "/compose", "/import"):
            self.assertEqual(c.get(url).status_code, 403, url)
        self.assertEqual(self.post(c, "/contacts/new", first_name="N", email="n@b.gov").status_code, 403)
        self.assertEqual(self.post(c, f"/contacts/{cid}/delete").status_code, 403)
        self.assertEqual(self.post(c, "/tasks/new", title="x").status_code, 403)
        # viewers may still manage their own profile
        self.assertEqual(self.post(c, "/profile", name="Val V", default_scope="team").status_code, 302)
        self.assertEqual(db.user_by_email("val@firm.com")["name"], "Val V")

    def test_user_delete_needs_setting(self):
        bob = self.make_user("Bob", "bob@firm.com")
        c, _ = self.login("bob@firm.com")
        cid = db.execute("INSERT INTO contacts(first_name, email, owner_id, created_at, updated_at) "
                         "VALUES ('A','own@b.gov',?,?,?)", (bob, db.now_iso(), db.now_iso()))
        db.set_setting("user_can_delete_own", "0")
        self.assertEqual(self.post(c, f"/contacts/{cid}/delete").status_code, 403)
        db.set_setting("user_can_delete_own", "1")
        self.post(c, f"/contacts/{cid}/delete")
        self.assertIsNone(db.query("SELECT 1 FROM contacts WHERE id=?", (cid,), one=True))

    def test_security_headers(self):
        r = self.admin.get("/")
        self.assertEqual(r.headers.get("X-Frame-Options"), "DENY")
        self.assertIn("default-src", r.headers.get("Content-Security-Policy", ""))


class TestAdmin(WebBase):
    def test_last_admin_cannot_be_demoted_or_deactivated(self):
        self.post(self.admin, f"/admin/users/{self.ann}", name="Ann", email="ann@firm.com", role="user", active="1")
        self.assertEqual(db.user(self.ann)["role"], "admin")
        other = self.make_user("Second", "two@firm.com", "admin")
        self.post(self.admin, f"/admin/users/{self.ann}", name="Ann", email="ann@firm.com", role="user", active="1")
        self.assertEqual(db.user(self.ann)["role"], "user")
        self.assertEqual(db.user(other)["role"], "admin")

    def test_delete_user_reassigns_records(self):
        bob = self.make_user("Bob", "bob@firm.com")
        joe = self.make_user("Joe", "joe@firm.com")
        cid = db.execute("INSERT INTO contacts(first_name, email, owner_id, created_by, created_at, updated_at) "
                         "VALUES ('A','a@b.gov',?,?,?,?)", (bob, bob, db.now_iso(), db.now_iso()))
        tid = db.execute("INSERT INTO tasks(title, assigned_to, created_at) VALUES ('t',?,?)", (bob, db.now_iso()))
        self.post(self.admin, f"/admin/users/{bob}/delete", reassign_to=str(joe))
        self.assertIsNone(db.user(bob))
        self.assertEqual(db.query("SELECT owner_id FROM contacts WHERE id=?", (cid,), one=True)["owner_id"], joe)
        self.assertEqual(db.query("SELECT assigned_to FROM tasks WHERE id=?", (tid,), one=True)["assigned_to"], joe)
        self.assertTrue(db.query("SELECT 1 FROM audit_log WHERE action='delete' AND entity='user'", one=True))

    def test_settings_secret_encrypted_and_kept(self):
        r = self.post(self.admin, "/admin/settings", section="m365", ms_client_id="11111111-2222-3333-4444-555555555555",
                      ms_tenant="contoso.onmicrosoft.com", ms_cloud="commercial", ms_client_secret="s3cret-value",
                      base_url="https://relay.example.com")
        self.assertEqual(r.status_code, 302)
        raw = db.get_setting("ms_client_secret")
        self.assertTrue(raw and "s3cret-value" not in raw)
        self.post(self.admin, "/admin/settings", section="m365", ms_client_id="11111111-2222-3333-4444-555555555555",
                  ms_tenant="contoso.onmicrosoft.com", ms_cloud="commercial", ms_client_secret="",
                  base_url="https://relay.example.com")
        self.assertEqual(db.get_setting("ms_client_secret"), raw)       # blank keeps existing
        self.assertNotIn("s3cret-value", self.admin.get("/admin/settings").data.decode())

    def test_audit_and_backup(self):
        self.assertEqual(self.admin.get("/admin/audit").status_code, 200)
        r = self.admin.get("/admin/audit.csv")
        self.assertIn("text/csv", r.headers["Content-Type"])
        self.post(self.admin, "/admin/backups/now")
        page = self.admin.get("/admin/backups").data.decode()
        name = re.search(r"relay-\d{8}-\d{6}\.db", page).group(0)
        r = self.admin.get(f"/admin/backups/{name}")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data.startswith(b"SQLite format 3"))
        self.assertEqual(self.admin.get("/admin/backups/..%2Frelay.db").status_code, 404)


class TestOwnershipAndReports(WebBase):
    seed_demo = True

    def test_all_pages_render_with_demo_team(self):
        urls = ["/", "/contacts", "/contacts?owner=all", "/contacts?owner=none", "/orgs", "/pipeline",
                "/pipeline?owner=all", "/tasks", "/tasks?owner=all", "/reports", "/import", "/compose",
                "/templates", "/sync/log", "/profile", "/admin/users", "/admin/users/new", "/admin/settings",
                "/admin/audit", "/admin/backups", "/opps/new", "/contacts/new", "/search?q=a"]
        urls += [f"/contacts/{r['id']}" for r in db.query("SELECT id FROM contacts LIMIT 3")]
        urls += [f"/orgs/{r['id']}" for r in db.query("SELECT id FROM organizations LIMIT 3")]
        urls += [f"/opps/{r['id']}" for r in db.query("SELECT id FROM opportunities LIMIT 3")]
        urls += [f"/admin/users/{u['id']}" for u in db.users(active_only=False)]
        for key in reports.REPORTS:
            urls += [f"/reports/{key}", f"/reports/{key}?user=me", f"/reports/{key}.csv"]
        for u in urls:
            r = self.admin.get(u)
            self.assertEqual(r.status_code, 200, f"{u} -> {r.status_code}")

    def test_mine_filter_and_reports_by_user(self):
        users = [u for u in db.users(active_only=False) if u["is_demo"]]
        self.assertTrue(users, "demo teammates expected")
        uid = users[0]["id"]
        n_all = len(reports.run("pipeline")["rows"])
        n_one = len(reports.run("pipeline", user_id=uid)["rows"])
        self.assertLessEqual(n_one, n_all)
        mine = db.query("SELECT COUNT(*) n FROM opportunities WHERE owner_id=?", (uid,), one=True)["n"]
        self.assertGreaterEqual(n_all, mine)
        team = reports.run("team")
        self.assertTrue(team["rows"])

    def test_new_contact_owned_by_creator(self):
        bob = self.make_user("Bob", "bob@firm.com")
        c, _ = self.login("bob@firm.com")
        self.post(c, "/contacts/new", first_name="Zed", email="zed@agency.gov", status="Lead", owner_id=str(bob))
        row = db.query("SELECT owner_id, created_by FROM contacts WHERE email='zed@agency.gov'", one=True)
        self.assertEqual((row["owner_id"], row["created_by"]), (bob, bob))
        self.assertIn(b"zed@agency.gov", c.get("/contacts?owner=mine").data)


class TestV1Upgrade(unittest.TestCase):
    """A single-user v1 database is adopted by the first admin."""

    def test_upgrade(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "relay.db")
        con = sqlite3.connect(path)
        con.executescript("""
            CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE organizations(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, domain TEXT,
              org_type TEXT, website TEXT DEFAULT '', phone TEXT DEFAULT '', address TEXT DEFAULT '',
              notes TEXT DEFAULT '', created_at TEXT NOT NULL);
            CREATE TABLE contacts(id INTEGER PRIMARY KEY AUTOINCREMENT, first_name TEXT DEFAULT '',
              last_name TEXT DEFAULT '', email TEXT UNIQUE, org_id INTEGER, status TEXT DEFAULT 'Lead',
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            INSERT INTO settings VALUES ('my_email','old@firm.com'), ('signature','-- Old sig');
            INSERT INTO contacts(first_name, email, created_at, updated_at) VALUES ('Legacy','legacy@agency.gov','2024-01-01','2024-01-01');
        """)
        con.commit()
        con.close()
        old_path = db.DB_PATH
        try:
            db.DB_PATH = path
            db.init_db()
            self.assertEqual(db.get_setting("v1_upgrade_pending"), "1")
            app = create_app(False)
            c = app.test_client()
            r = c.get("/setup")
            self.assertIn(b"upgrad", r.data.lower())
            with c.session_transaction() as s:
                tok = s["csrf"]
            r = c.post("/setup", data={"csrf_token": tok, "name": "Ann", "email": "ann@firm.com",
                                       "password": PW, "password2": PW})
            self.assertEqual(r.status_code, 302)
            ann = db.user_by_email("ann@firm.com")
            row = db.query("SELECT owner_id FROM contacts WHERE email='legacy@agency.gov'", one=True)
            self.assertEqual(row["owner_id"], ann["id"])
            self.assertIn("old@firm.com", ann["aliases"])
            self.assertEqual(ann["signature"], "-- Old sig")
            self.assertIsNone(db.get_setting("v1_upgrade_pending") or None)
        finally:
            db.DB_PATH = old_path
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
