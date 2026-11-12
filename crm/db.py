"""SQLite storage layer.

One file (relay.db in the data folder) holds everything, so a backup is a
single file copy. Schema changes are applied automatically on start-up using
SQLite's ``PRAGMA user_version`` (v1 single-user databases are upgraded in
place to the v2 team schema).
"""
import os
import sqlite3
import threading
from datetime import datetime

from .paths import DATA_DIR

DB_PATH = os.environ.get("RELAY_DB", os.path.join(DATA_DIR, "relay.db"))
SCHEMA_VERSION = 2

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'user',          -- admin | user | viewer
    password_hash TEXT DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    is_demo INTEGER NOT NULL DEFAULT 0,
    title TEXT DEFAULT '',
    signature TEXT DEFAULT '',
    aliases TEXT DEFAULT '',                    -- other addresses this person sends from
    default_scope TEXT DEFAULT 'mine',          -- mine | team
    ms_oid TEXT,                                -- Entra object id (Sign in with Microsoft)
    ms_email TEXT DEFAULT '',                   -- mailbox connected for sync
    ms_token_cache TEXT DEFAULT '',             -- encrypted MSAL cache
    ms_client_kind TEXT DEFAULT '',             -- public | confidential
    sync_enabled INTEGER NOT NULL DEFAULT 1,
    last_sync_inbox TEXT DEFAULT '',
    last_sync_sent TEXT DEFAULT '',
    last_sync_capture TEXT DEFAULT '',
    last_sync_at TEXT,
    last_sync_status TEXT DEFAULT '',
    failed_logins INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    session_version INTEGER NOT NULL DEFAULT 1,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_login TEXT
);

CREATE TABLE IF NOT EXISTS organizations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    domain TEXT UNIQUE,
    org_type TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    website TEXT DEFAULT '',
    address TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    owner_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    first_name TEXT DEFAULT '',
    last_name TEXT DEFAULT '',
    email TEXT UNIQUE,
    title TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    mobile TEXT DEFAULT '',
    org_id INTEGER REFERENCES organizations(id) ON DELETE SET NULL,
    status TEXT DEFAULT 'Lead',
    source TEXT DEFAULT 'Manual',
    tags TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    last_contacted TEXT,
    last_inbound TEXT,
    last_outbound TEXT,
    emails_in INTEGER DEFAULT 0,
    emails_out INTEGER DEFAULT 0,
    needs_review INTEGER DEFAULT 0,
    owner_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_contacts_org ON contacts(org_id);

CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    org_id INTEGER REFERENCES organizations(id) ON DELETE SET NULL,
    contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
    stage TEXT NOT NULL DEFAULT 'Lead',
    value REAL DEFAULT 0,
    probability INTEGER DEFAULT 10,
    close_date TEXT,
    solicitation_no TEXT DEFAULT '',
    contract_vehicle TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    owner_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    stage_changed_at TEXT,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
    opp_id INTEGER REFERENCES opportunities(id) ON DELETE SET NULL,
    org_id INTEGER REFERENCES organizations(id) ON DELETE SET NULL,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,   -- team member involved
    kind TEXT NOT NULL,             -- email_in, email_out, call, meeting, note, stage, system
    subject TEXT DEFAULT '',
    body TEXT DEFAULT '',
    message_id TEXT,
    conversation_id TEXT,
    outcome TEXT DEFAULT '',
    duration_min INTEGER,
    occurred_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    via TEXT DEFAULT ''             -- auto-sync, capture, compose, manual
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_act_msg ON activities(message_id, contact_id)
    WHERE message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_act_contact ON activities(contact_id, occurred_at);
CREATE INDEX IF NOT EXISTS ix_act_conv ON activities(conversation_id);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
    opp_id INTEGER REFERENCES opportunities(id) ON DELETE SET NULL,
    assigned_to INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    due_date TEXT,
    priority TEXT DEFAULT 'Normal',
    status TEXT DEFAULT 'Open',
    auto_rule TEXT DEFAULT '',
    conversation_id TEXT,
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_tasks_status ON tasks(status, due_date);

CREATE TABLE IF NOT EXISTS templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    subject TEXT DEFAULT '',
    body TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    trigger TEXT DEFAULT '',
    scanned INTEGER DEFAULT 0,
    logged INTEGER DEFAULT 0,
    new_contacts INTEGER DEFAULT 0,
    tasks_created INTEGER DEFAULT 0,
    tasks_closed INTEGER DEFAULT 0,
    status TEXT DEFAULT 'running',
    message TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    user_id INTEGER,
    user_label TEXT DEFAULT '',
    action TEXT NOT NULL,           -- create, update, delete, login, login_failed, export, ...
    entity TEXT DEFAULT '',         -- contact, opportunity, user, settings ...
    entity_id INTEGER,
    detail TEXT DEFAULT '',
    ip TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit_log(ts);
"""

# Columns added in v2 to tables that already existed in v1 databases.
V2_COLUMNS = {
    "organizations": [("owner_id", "INTEGER REFERENCES users(id) ON DELETE SET NULL")],
    "contacts": [("owner_id", "INTEGER REFERENCES users(id) ON DELETE SET NULL"),
                 ("created_by", "INTEGER REFERENCES users(id) ON DELETE SET NULL")],
    "opportunities": [("owner_id", "INTEGER REFERENCES users(id) ON DELETE SET NULL")],
    "activities": [("user_id", "INTEGER REFERENCES users(id) ON DELETE SET NULL")],
    "tasks": [("assigned_to", "INTEGER REFERENCES users(id) ON DELETE SET NULL"),
              ("created_by", "INTEGER REFERENCES users(id) ON DELETE SET NULL")],
    "sync_log": [("user_id", "INTEGER REFERENCES users(id) ON DELETE SET NULL")],
}

POST_INDEXES = """
CREATE INDEX IF NOT EXISTS ix_contacts_owner ON contacts(owner_id);
CREATE INDEX IF NOT EXISTS ix_opps_owner ON opportunities(owner_id);
CREATE INDEX IF NOT EXISTS ix_tasks_assigned ON tasks(assigned_to, status);
CREATE INDEX IF NOT EXISTS ix_act_user ON activities(user_id, occurred_at);
"""

DEFAULT_SETTINGS = {
    # ---- general
    "firm_name": "",
    "mode": "demo",                      # demo | live
    "setup_complete": "0",
    # ---- Microsoft 365 / Entra app registration (team-wide)
    "ms_cloud": "commercial",            # commercial | gcc | gcch | dod
    "ms_tenant": "",
    "ms_client_id": "",
    "ms_client_secret": "",              # encrypted; optional (enables Web platform sign-in)
    "base_url": "",                      # public URL of this server, e.g. https://crm.yourfirm.com
    # ---- sign-in policy
    "allow_password_login": "1",
    "allow_ms_login": "1",
    "auto_provision": "0",               # create a 'user' account on first Microsoft sign-in
    "auto_provision_domains": "",        # restrict auto-provisioning to these email domains
    "session_timeout_min": "480",
    "password_min_length": "12",
    # ---- permissions
    "user_can_export": "1",
    "user_can_import": "0",
    "user_can_delete_own": "1",
    # ---- email capture
    "sync_auto": "1",
    "sync_capture": "1",
    "capture_folder": "CRM",
    "capture_mailbox": "",               # optional shared mailbox (team BCC address)
    "capture_reader_id": "",             # user whose connection reads the shared mailbox
    "last_sync_shared": "",
    "sync_interval_min": "10",
    "initial_lookback_days": "30",
    "internal_domains": "",              # your firm's domains - never auto-create
    "autocreate": "both",                # both | outbound | off
    "autocreate_cc": "1",
    "skip_bulk": "1",
    "ignore_patterns": ("noreply,no-reply,donotreply,do-not-reply,notifications,mailer-daemon,postmaster,"
                        "newsletter,news@,marketing@,bounce,calendar-notification"),
    # ---- rules
    "followup_days": "5",
    "reply_days": "2",
    "rule_awaiting_reply": "1",
    "rule_reply_needed": "1",
    "auto_promote": "1",
    "stale_days": "45",
    # ---- lists
    "stages": "Lead,Qualified,Proposal,Submitted,Awarded,Lost",
    "stage_probabilities": "Lead:10,Qualified:25,Proposal:50,Submitted:70,Awarded:100,Lost:0",
    "contact_statuses": "Lead,Prospect,Client,Partner,Inactive",
    "org_types": ("Federal,State,Local,Tribal,Military / DoD,Education,Non-profit,Private Sector,"
                  "Partner / Teaming"),
    # ---- maintenance
    "backup_keep_days": "14",
    "last_auto_backup": "",
}


def now_iso():
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def connect():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn):
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    upgrading_v1 = "contacts" in existing and "users" not in existing
    conn.executescript(SCHEMA)
    for table, cols in V2_COLUMNS.items():
        have = _columns(conn, table)
        for name, decl in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.executescript(POST_INDEXES)
    if upgrading_v1:
        # A v1 single-user install: keep the data, the setup wizard will create the
        # first admin and assign every existing record to them.
        conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('v1_upgrade_pending', '1')")
        row = conn.execute("SELECT value FROM settings WHERE key='ms_tenant'").fetchone()
        if row and row[0] == "organizations":
            conn.execute("UPDATE settings SET value='' WHERE key='ms_tenant'")
    if version < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def init_db():
    with _lock:
        conn = connect()
        _migrate(conn)
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
        if conn.execute("SELECT COUNT(*) FROM templates").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO templates(name, subject, body) VALUES (?,?,?)",
                [
                    ("Intro / capability statement",
                     "Introduction - {my_name}",
                     "Hi {first_name},\n\nI hope this finds you well. I wanted to briefly introduce myself and share "
                     "how we have supported agencies like {organization} with similar priorities.\n\n"
                     "Would you be open to a 20-minute conversation in the next couple of weeks?\n\nBest regards,\n{my_name}"),
                    ("Meeting follow-up",
                     "Thank you for your time",
                     "Hi {first_name},\n\nThank you for taking the time to meet today. As discussed, I will follow up "
                     "with the items below:\n\n- \n- \n\nPlease let me know if I missed anything.\n\nBest regards,\n{my_name}"),
                    ("Gentle nudge",
                     "Following up",
                     "Hi {first_name},\n\nI just wanted to follow up on my previous note. Happy to adjust to whatever "
                     "timing works best for you.\n\nBest regards,\n{my_name}"),
                ],
            )
        conn.commit()
        conn.close()


# ---------------------------------------------------------------- helpers
def query(sql, params=(), one=False):
    with _lock:
        conn = connect()
        try:
            cur = conn.execute(sql, params)
            rows = cur.fetchall()
            return (rows[0] if rows else None) if one else rows
        finally:
            conn.close()


def execute(sql, params=()):
    with _lock:
        conn = connect()
        try:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def get_setting(key, default=None):
    row = query("SELECT value FROM settings WHERE key=?", (key,), one=True)
    if row is None:
        return DEFAULT_SETTINGS.get(key, default)
    return row["value"]


def get_settings():
    data = dict(DEFAULT_SETTINGS)
    for r in query("SELECT key, value FROM settings"):
        data[r["key"]] = r["value"]
    return data


def set_setting(key, value):
    execute("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, "" if value is None else str(value)))


def setting_list(key):
    return [s.strip() for s in (get_setting(key) or "").split(",") if s.strip()]


def stage_probabilities():
    out = {}
    for part in setting_list("stage_probabilities"):
        if ":" in part:
            k, v = part.split(":", 1)
            try:
                out[k.strip()] = int(v)
            except ValueError:
                pass
    return out


def closed_stages():
    stages = setting_list("stages")
    won = "Awarded" if "Awarded" in stages else (stages[-2] if len(stages) >= 2 else "")
    lost = "Lost" if "Lost" in stages else (stages[-1] if stages else "")
    return won, lost


# ---------------------------------------------------------------- users
def user(uid):
    if not uid:
        return None
    return query("SELECT * FROM users WHERE id=?", (uid,), one=True)


def user_by_email(email):
    if not email:
        return None
    return query("SELECT * FROM users WHERE email=? COLLATE NOCASE", (email.strip().lower(),), one=True)


def users(active_only=True, include_demo=True):
    sql = "SELECT * FROM users WHERE 1=1"
    if active_only:
        sql += " AND active=1"
    if not include_demo:
        sql += " AND is_demo=0"
    return query(sql + " ORDER BY name COLLATE NOCASE")


def user_count():
    return query("SELECT COUNT(*) n FROM users WHERE is_demo=0", one=True)["n"]


def audit(user_row, action, entity="", entity_id=None, detail="", ip=""):
    label = ""
    uid = None
    if user_row is not None:
        uid = user_row["id"]
        label = f"{user_row['name']} <{user_row['email']}>"
    execute("INSERT INTO audit_log(ts, user_id, user_label, action, entity, entity_id, detail, ip) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (now_iso(), uid, label, action, entity, entity_id, (detail or "")[:2000], ip or ""))
