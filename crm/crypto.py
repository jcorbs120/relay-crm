"""At-rest encryption for secrets stored in the database (Microsoft token caches,
the optional Entra client secret).

Uses Fernet (AES-128-CBC + HMAC-SHA256) with a random key kept in
``<data dir>/encryption.key``. Restrict access to the data folder (the Windows
installer limits it to SYSTEM and Administrators). If the key file is lost,
users simply reconnect their mailboxes - no CRM data is lost.
"""
import os
import secrets

from cryptography.fernet import Fernet, InvalidToken

from .paths import DATA_DIR

_KEY_PATH = os.path.join(DATA_DIR, "encryption.key")
_fernet = None


def _restrict(path):
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _get():
    global _fernet
    if _fernet is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        if not os.path.exists(_KEY_PATH):
            with open(_KEY_PATH, "wb") as f:
                f.write(Fernet.generate_key())
            _restrict(_KEY_PATH)
        with open(_KEY_PATH, "rb") as f:
            _fernet = Fernet(f.read().strip())
    return _fernet


def encrypt(text):
    if not text:
        return ""
    return _get().encrypt(text.encode("utf-8")).decode("ascii")


def decrypt(token):
    if not token:
        return ""
    try:
        return _get().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""


def secret_file(name, nbytes=32):
    """Return a persistent random hex secret stored in the data folder."""
    path = os.path.join(DATA_DIR, name)
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(nbytes))
        _restrict(path)
    with open(path) as f:
        return f.read().strip()
