"""SQLite storage. One file, no server."""
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from . import paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS orgs (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  domain TEXT UNIQUE,
  org_type TEXT DEFAULT 'Other',
  phone TEXT, website TEXT, address TEXT, notes TEXT,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS contacts (
  id INTEGER PRIMARY KEY,
  first_name TEXT DEFAULT '', last_name TEXT DEFAULT '',
  email TEXT UNIQUE COLLATE NOCASE,
  phone TEXT, mobile TEXT, title TEXT,
  org_id INTEGER REFERENCES orgs(id) ON DELETE SET NULL,
  lifecycle TEXT DEFAULT 'Lead',          -- Lead / Prospect / Customer / Partner / Inactive
  needs_review INTEGER DEFAULT 0,
  tags TEXT DEFAULT '',
  source TEXT DEFAULT 'manual',           -- manual / email-out / email-in / import / demo
  notes TEXT DEFAULT '',
  last_contacted TEXT,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS opportunities (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  org_id INTEGER REFERENCES orgs(id) ON DELETE SET NULL,
  contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
  stage TEXT DEFAULT 'Lead',
  value REAL DEFAULT 0,
  probability INTEGER DEFAULT 10,
  close_date TEXT,
  solicitation TEXT DEFAULT '',
  vehicle TEXT DEFAULT '',
  notes TEXT DEFAULT '',
  created_at TEXT, updated_at TEXT, closed_at TEXT
);

CREATE TABLE IF NOT EXISTS activities (
  id INTEGER PRIMARY KEY,
  contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
  opp_id INTEGER REFERENCES opportunities(id) ON DELETE SET NULL,
  kind TEXT NOT NULL,                     -- email_in / email_out / note / call / meeting
  subject TEXT DEFAULT '', body TEXT DEFAULT '',
  occurred_at TEXT NOT NULL,
  message_id TEXT, conversation_id TEXT,
  duration_min INTEGER,
  created_at TEXT,
  UNIQUE(message_id, contact_id)
);
CREATE INDEX IF NOT EXISTS ix_act_contact ON activities(contact_id, occurred_at);

CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY,
  contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
  opp_id INTEGER REFERENCES opportunities(id) ON DELETE SET NULL,
  title TEXT NOT NULL,
  due_date TEXT,
  kind TEXT DEFAULT 'manual',             -- manual / awaiting_reply / reply_needed
  priority TEXT DEFAULT 'Normal',
  done INTEGER DEFAULT 0,
  created_at TEXT, completed_at TEXT
);

CREATE TABLE IF NOT EXISTS email_templates (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, subject TEXT DEFAULT '', body TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sync_log (
  id INTEGER PRIMARY KEY,
  started_at TEXT, finished_at TEXT, mode TEXT,
  fetched INTEGER DEFAULT 0, logged INTEGER DEFAULT 0,
  contacts_created INTEGER DEFAULT 0, tasks_created INTEGER DEFAULT 0,
  tasks_closed INTEGER DEFAULT 0, error TEXT
);
"""

DEFAULT_SETTINGS = {
    "mode": "demo",                 # demo / live
    "my_email": "",
    "my_name": "",
    "aliases": "",                  # extra addresses that are "me"
    "crm_folder": "CRM",            # Outlook folder BCC/forwards are filed into
    "skip_internal": "1",           # don't create contacts for my own domain
    "awaiting_reply_days": "5",
    "reply_needed_days": "2",
    "sync_minutes": "5",
    "first_sync_days": "30",
    "auto_create_inbound": "1",
    "auto_create_outbound": "1",
    "m365_cloud": "commercial",     # commercial / gcc / gcchigh / dod
    "m365_tenant_id": "",
    "m365_client_id": "",
    "last_sync_at": "",
    "password_hash": "",
    "signature": "",
}

_lock = threading.RLock()


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path=None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or paths.db_path()), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


class DB:
    """Thin wrapper; all writes serialized with a lock (sync thread + web)."""

    def __init__(self, path=None):
        self.path = path or paths.db_path()
        self.conn = connect(self.path)
        with _lock:
            self.conn.executescript(SCHEMA)
            for k, v in DEFAULT_SETTINGS.items():
                self.conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
            self.conn.commit()

    # -- generic helpers --
    def q(self, sql, args=()):
        with _lock:
            return self.conn.execute(sql, args).fetchall()

    def one(self, sql, args=()):
        with _lock:
            return self.conn.execute(sql, args).fetchone()

    def val(self, sql, args=(), default=None):
        r = self.one(sql, args)
        return r[0] if r and r[0] is not None else default

    def ex(self, sql, args=()):
        with _lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.lastrowid

    @contextmanager
    def tx(self):
        with _lock:
            try:
                yield self.conn
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    # -- settings --
    def get(self, key, default=""):
        r = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return r["value"] if r and r["value"] is not None else default

    def set(self, key, value):
        self.ex("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, "" if value is None else str(value)))

    def settings(self):
        return {r["key"]: r["value"] for r in self.q("SELECT key, value FROM settings")}

    def backup_to(self, dest):
        with _lock:
            dst = sqlite3.connect(str(dest))
            self.conn.backup(dst)
            dst.close()

    def wipe_data(self):
        with self.tx() as c:
            for t in ("activities", "tasks", "opportunities", "contacts", "orgs", "sync_log", "email_templates"):
                c.execute(f"DELETE FROM {t}")
