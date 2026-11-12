"""Database backups.

* On-demand download (Admin > Backup).
* Automatic daily snapshot into <data dir>/backups, keeping N days
  (Settings > Security > "Keep automatic backups for").
Uses SQLite's online backup API, so it is safe while people are working.
"""
import glob
import os
import sqlite3
from datetime import date, datetime, timedelta

from . import db
from .paths import DATA_DIR

BACKUP_DIR = os.path.join(DATA_DIR, "backups")


def snapshot(path):
    src = db.connect()
    dst = sqlite3.connect(path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return path


def daily_backup(force=False):
    today = date.today().isoformat()
    if not force and db.get_setting("last_auto_backup") == today:
        return None
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(BACKUP_DIR, f"relay-{datetime.now():%Y%m%d-%H%M%S}.db")
    snapshot(path)
    db.set_setting("last_auto_backup", today)
    keep = int(db.get_setting("backup_keep_days") or 14)
    cutoff = datetime.now() - timedelta(days=keep)
    for f in glob.glob(os.path.join(BACKUP_DIR, "relay-*.db")):
        if datetime.fromtimestamp(os.path.getmtime(f)) < cutoff:
            try:
                os.remove(f)
            except OSError:
                pass
    return path


def list_backups():
    files = sorted(glob.glob(os.path.join(BACKUP_DIR, "relay-*.db")), reverse=True)
    return [{"name": os.path.basename(f), "size": os.path.getsize(f),
             "when": datetime.fromtimestamp(os.path.getmtime(f)).replace(microsecond=0).isoformat(sep=" ")}
            for f in files]
