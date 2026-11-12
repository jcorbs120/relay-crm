"""Filesystem locations.

* RES_DIR  - read-only program files (templates, static). Inside a PyInstaller
             build this is the temporary extraction folder.
* DATA_DIR - the writable folder holding the database, logs, keys and config.
             Override with the RELAY_DATA_DIR environment variable or the
             --data-dir command-line option.
             Defaults: source install -> ./data
                       Windows installer -> C:\\ProgramData\\RelayCRM
                       other frozen builds -> ~/.relay-crm
"""
import os
import sys

FROZEN = bool(getattr(sys, "frozen", False))

if FROZEN:
    RES_DIR = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    APP_DIR = os.path.dirname(sys.executable)
else:
    RES_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    APP_DIR = RES_DIR


def default_data_dir():
    env = os.environ.get("RELAY_DATA_DIR")
    if env:
        return os.path.abspath(env)
    if FROZEN:
        if os.name == "nt":
            return os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), "RelayCRM")
        return os.path.join(os.path.expanduser("~"), ".relay-crm")
    return os.path.join(APP_DIR, "data")


DATA_DIR = default_data_dir()
TEMPLATE_DIR = os.path.join(RES_DIR, "templates")
STATIC_DIR = os.path.join(RES_DIR, "static")
