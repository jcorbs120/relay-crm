"""Where Relay CRM keeps its files.

Code/templates live next to this module (or inside the PyInstaller bundle);
user data lives in a per-user folder so upgrades never touch it.
"""
import os
import sys
from pathlib import Path


def resource_dir() -> Path:
    """Folder holding templates/ and static/ (works frozen or from source)."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "relay"
    return Path(__file__).resolve().parent


def data_dir() -> Path:
    override = os.environ.get("RELAY_DATA_DIR")
    if override:
        p = Path(override)
    elif os.name == "nt":
        p = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "RelayCRM"
    elif sys.platform == "darwin":
        p = Path.home() / "Library" / "Application Support" / "RelayCRM"
    else:
        p = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "RelayCRM"
    p.mkdir(parents=True, exist_ok=True)
    (p / "backups").mkdir(exist_ok=True)
    return p


def db_path() -> Path:
    return data_dir() / "relay.db"


def token_cache_path() -> Path:
    return data_dir() / "m365_token_cache.bin"


def log_path() -> Path:
    return data_dir() / "relay.log"
