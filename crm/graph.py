"""Microsoft 365 connector (Microsoft Graph) - team edition.

* One Entra ID app registration for the whole firm (Settings > Microsoft 365).
* Every team member connects **their own** mailbox. Their MSAL token cache
  (refresh token only - never a password) is stored encrypted in the database
  (see crypto.py), so the server can sync each mailbox in the background.
* Two ways to connect:
    - Device sign-in (public client): works on any server, no redirect URI.
    - Web sign-in (confidential client): used when the admin adds a client
      secret and the server's public URL. This also powers "Sign in with
      Microsoft" on the login page (SSO) - one click signs in AND connects the
      mailbox.
* Clouds: Commercial, GCC, GCC High, DoD.
* Delegated permissions only: User.Read, Mail.Read, Mail.Send
  (+ Mail.Read.Shared for whoever reads the optional shared capture mailbox).
"""
import json
import logging
import threading
import time
from datetime import datetime, timezone

import requests

from . import crypto, db

log = logging.getLogger("relay.graph")

CLOUDS = {
    "commercial": {"label": "Commercial / GCC", "authority": "https://login.microsoftonline.com",
                   "graph": "https://graph.microsoft.com", "portal": "entra.microsoft.com"},
    "gcc": {"label": "GCC (Moderate)", "authority": "https://login.microsoftonline.com",
            "graph": "https://graph.microsoft.com", "portal": "entra.microsoft.com"},
    "gcch": {"label": "GCC High", "authority": "https://login.microsoftonline.us",
             "graph": "https://graph.microsoft.us", "portal": "entra.microsoft.us"},
    "dod": {"label": "DoD", "authority": "https://login.microsoftonline.us",
            "graph": "https://dod-graph.microsoft.us", "portal": "entra.microsoft.us"},
}

MSG_SELECT = ("id,internetMessageId,conversationId,subject,bodyPreview,body,from,sender,toRecipients,"
              "ccRecipients,receivedDateTime,sentDateTime,isDraft,internetMessageHeaders")

CALLBACK_PATH = "/auth/microsoft/callback"


class GraphError(Exception):
    pass


class NotConnected(GraphError):
    pass


# ------------------------------------------------------------------ config
def config():
    s = db.get_settings()
    cloud = CLOUDS.get(s.get("ms_cloud") or "commercial", CLOUDS["commercial"])
    return {
        "cloud": cloud,
        "tenant": (s.get("ms_tenant") or "").strip(),
        "client_id": (s.get("ms_client_id") or "").strip(),
        "secret": crypto.decrypt(s.get("ms_client_secret") or ""),
        "base_url": (s.get("base_url") or "").strip().rstrip("/"),
        "capture_mailbox": (s.get("capture_mailbox") or "").strip(),
    }


def configured():
    c = config()
    return bool(c["client_id"] and c["tenant"])


def web_signin_available():
    c = config()
    return bool(c["client_id"] and c["tenant"] and c["secret"] and c["base_url"])


def redirect_uri():
    return config()["base_url"] + CALLBACK_PATH


def scopes(shared=False):
    g = config()["cloud"]["graph"]
    names = ["User.Read", "Mail.Read", "Mail.Send"]
    if shared:
        names.append("Mail.Read.Shared")
    return [f"{g}/{n}" for n in names]


def _build_app(kind, cache=None):
    import msal
    c = config()
    if not c["client_id"]:
        raise NotConnected("Microsoft 365 isn't set up yet. An administrator needs to add the app registration "
                           "in Settings > Microsoft 365.")
    if not c["tenant"]:
        raise NotConnected("The Directory (tenant) ID is missing in Settings > Microsoft 365.")
    authority = f"{c['cloud']['authority']}/{c['tenant']}"
    if kind == "confidential":
        if not c["secret"]:
            raise NotConnected("Web sign-in needs a client secret (Settings > Microsoft 365).")
        return msal.ConfidentialClientApplication(c["client_id"], client_credential=c["secret"],
                                                  authority=authority, token_cache=cache)
    return msal.PublicClientApplication(c["client_id"], authority=authority, token_cache=cache)


# ------------------------------------------------------------------ per-user client
class GraphClient:
    """Graph access on behalf of one team member."""

    def __init__(self, user):
        if isinstance(user, int):
            user = db.user(user)
        self.user = user
        self.uid = user["id"] if user else None
        self.kind = (user["ms_client_kind"] if user else "") or "public"
        self.graph = config()["cloud"]["graph"]
        self._app = None
        self._cache = None

    # ----------------------------------------------------------- cache
    def _load_cache(self):
        import msal
        cache = msal.SerializableTokenCache()
        raw = crypto.decrypt(self.user["ms_token_cache"]) if self.user else ""
        if raw:
            try:
                cache.deserialize(raw)
            except Exception:  # corrupt / old format
                pass
        return cache

    def _save_cache(self):
        if self._cache is not None and self._cache.has_state_changed and self.uid:
            db.execute("UPDATE users SET ms_token_cache=? WHERE id=?",
                       (crypto.encrypt(self._cache.serialize()), self.uid))
            self._cache.has_state_changed = False

    def app(self):
        if self._app is None:
            self._cache = self._load_cache()
            self._app = _build_app(self.kind, self._cache)
        return self._app

    def account(self):
        if not self.user or not self.user["ms_token_cache"]:
            return None
        try:
            accts = self.app().get_accounts()
        except NotConnected:
            return None
        return accts[0] if accts else None

    def token(self, shared=False):
        acct = self.account()
        if not acct:
            raise NotConnected("Mailbox not connected.")
        result = self.app().acquire_token_silent(scopes(shared), account=acct)
        self._save_cache()
        if not result or "access_token" not in result:
            if shared:
                raise NotConnected("Reading the shared capture mailbox needs the Mail.Read.Shared permission - "
                                   "reconnect your mailbox from My profile to grant it.")
            raise NotConnected("Microsoft 365 session expired - reconnect your mailbox from My profile.")
        return result["access_token"]

    def is_connected(self):
        try:
            self.token()
            return True
        except Exception:
            return False

    def disconnect(self):
        if self.uid:
            db.execute("UPDATE users SET ms_token_cache='', ms_email='', ms_client_kind='' WHERE id=?", (self.uid,))
        DEVICE_FLOWS.pop(self.uid, None)

    # ----------------------------------------------------------- http
    def _req(self, method, path, shared=False, **kw):
        url = path if path.startswith("http") else f"{self.graph}/v1.0{path}"
        headers = kw.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.token(shared)}"
        headers.setdefault("Prefer", 'outlook.body-content-type="text"')
        r = None
        for attempt in range(4):
            r = requests.request(method, url, headers=headers, timeout=60, **kw)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(min(30, int(r.headers.get("Retry-After", 2 ** attempt))))
                continue
            break
        if r.status_code >= 400:
            try:
                msg = r.json().get("error", {}).get("message", r.text)
            except ValueError:
                msg = r.text
            raise GraphError(f"Graph {r.status_code}: {msg}")
        if r.status_code == 204 or not r.content:
            return {}
        return r.json()

    def me(self):
        return self._req("GET", "/me?$select=id,displayName,mail,userPrincipalName,jobTitle")

    def _mbx(self, mailbox=None):
        return f"/users/{mailbox}" if mailbox else "/me"

    def find_folder(self, name, mailbox=None):
        base = self._mbx(mailbox)
        safe = name.replace("'", "''")
        for path in (f"{base}/mailFolders?$filter=displayName eq '{safe}'",
                     f"{base}/mailFolders/inbox/childFolders?$filter=displayName eq '{safe}'"):
            try:
                res = self._req("GET", path, shared=bool(mailbox)).get("value", [])
            except GraphError:
                res = []
            if res:
                return res[0]["id"]
        return None

    def list_messages(self, folder, since_utc, mailbox=None, date_field="receivedDateTime", max_items=2000):
        base = self._mbx(mailbox)
        params = {"$select": MSG_SELECT, "$top": "50", "$orderby": f"{date_field} asc"}
        if since_utc:
            params["$filter"] = f"{date_field} ge {since_utc}"
        url = f"{self.graph}/v1.0{base}/mailFolders/{folder}/messages"
        count, first = 0, True
        while url and count < max_items:
            data = self._req("GET", url, shared=bool(mailbox), params=params if first else None)
            first = False
            for m in data.get("value", []):
                count += 1
                yield m
            url = data.get("@odata.nextLink")

    def send_mail(self, to, cc, bcc, subject, body, html=False):
        """Create a draft then send it, so we learn the internetMessageId for logging."""
        def rcpts(lst):
            return [{"emailAddress": {"address": a}} for a in lst if a]
        message = {"subject": subject, "body": {"contentType": "HTML" if html else "Text", "content": body},
                   "toRecipients": rcpts(to), "ccRecipients": rcpts(cc), "bccRecipients": rcpts(bcc)}
        draft = self._req("POST", "/me/messages", json=message)
        self._req("POST", f"/me/messages/{draft['id']}/send")
        return {"internet_message_id": draft.get("internetMessageId"), "conversation_id": draft.get("conversationId")}


# ------------------------------------------------------------------ device sign-in
DEVICE_FLOWS = {}          # uid -> state dict
_flow_lock = threading.Lock()


def _finish_connect(uid, kind, cache, claims_or_me):
    addr = (claims_or_me.get("mail") or claims_or_me.get("userPrincipalName")
            or claims_or_me.get("preferred_username") or "").lower()
    db.execute("UPDATE users SET ms_token_cache=?, ms_client_kind=?, ms_email=?, "
               "ms_oid=COALESCE(ms_oid, ?) WHERE id=?",
               (crypto.encrypt(cache.serialize()), kind, addr,
                claims_or_me.get("id") or claims_or_me.get("oid"), uid))
    return addr


def start_device_flow(user):
    """Begin device-code sign-in for this user; completes in a background thread."""
    import msal
    uid = user["id"]
    cache = msal.SerializableTokenCache()
    app = _build_app("public", cache)
    shared = bool(config()["capture_mailbox"]) and str(user["id"]) == (db.get_setting("capture_reader_id") or "")
    flow = app.initiate_device_flow(scopes=scopes(shared))
    if "user_code" not in flow:
        raise GraphError(flow.get("error_description") or json.dumps(flow))
    st = {"status": "pending", "message": flow.get("message", ""), "user_code": flow["user_code"],
          "verification_uri": flow.get("verification_uri", ""), "error": "", "email": ""}
    with _flow_lock:
        DEVICE_FLOWS[uid] = st

    def _wait():
        try:
            result = app.acquire_token_by_device_flow(flow)
            if "access_token" not in result:
                st.update(status="error", error=result.get("error_description", "Sign-in failed"))
                return
            r = requests.get(f"{config()['cloud']['graph']}/v1.0/me?$select=id,displayName,mail,userPrincipalName",
                             headers={"Authorization": f"Bearer {result['access_token']}"}, timeout=30)
            me = r.json() if r.ok else (result.get("id_token_claims") or {})
            addr = _finish_connect(uid, "public", cache, me)
            st.update(status="connected", email=addr, message="Connected.")
            db.audit(db.user(uid), "mailbox_connect", "user", uid, f"{addr} (device sign-in)")
        except Exception as e:  # pragma: no cover
            log.exception("device flow failed")
            st.update(status="error", error=str(e))

    threading.Thread(target=_wait, daemon=True, name=f"devflow-{uid}").start()
    return st


# ------------------------------------------------------------------ web sign-in (auth code)
def start_web_flow(shared=False):
    """Return (auth_url, flow) - store `flow` in the session until the callback."""
    app = _build_app("confidential")
    flow = app.initiate_auth_code_flow(scopes(shared), redirect_uri=redirect_uri(), prompt="select_account")
    if "auth_uri" not in flow:
        raise GraphError(flow.get("error_description") or "Could not start Microsoft sign-in")
    return flow["auth_uri"], flow


def finish_web_flow(flow, args):
    """Complete the auth-code flow. Returns (claims, cache) on success."""
    import msal
    cache = msal.SerializableTokenCache()
    app = _build_app("confidential", cache)
    result = app.acquire_token_by_auth_code_flow(flow, args)
    if "access_token" not in result:
        raise GraphError(result.get("error_description") or result.get("error") or "Microsoft sign-in failed")
    claims = result.get("id_token_claims") or {}
    return claims, cache


def store_web_connection(uid, claims, cache):
    return _finish_connect(uid, "confidential", cache, claims)


# ------------------------------------------------------------------ normalisation
def _addr(obj):
    if not obj:
        return None
    ea = obj.get("emailAddress", {}) if isinstance(obj, dict) else {}
    email = (ea.get("address") or "").strip().lower()
    if not email or "@" not in email:
        return None
    return {"name": (ea.get("name") or "").strip(), "email": email}


def to_local(iso_utc):
    if not iso_utc:
        return db.now_iso()
    dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    return dt.astimezone().replace(tzinfo=None, microsecond=0).isoformat(sep=" ")


def normalize(m, folder):
    headers = {h.get("name", "").lower(): h.get("value", "") for h in (m.get("internetMessageHeaders") or [])}
    date_utc = m.get("sentDateTime") if folder == "sent" else m.get("receivedDateTime")
    body = (m.get("body") or {}).get("content") or m.get("bodyPreview") or ""
    return {
        "id": m.get("id"),
        "internet_message_id": m.get("internetMessageId") or m.get("id"),
        "conversation_id": m.get("conversationId"),
        "subject": m.get("subject") or "(no subject)",
        "body": body,
        "from": _addr(m.get("from")) or _addr(m.get("sender")),
        "to": [a for a in (_addr(x) for x in m.get("toRecipients") or []) if a],
        "cc": [a for a in (_addr(x) for x in m.get("ccRecipients") or []) if a],
        "date": to_local(date_utc),
        "date_utc": date_utc,
        "folder": folder,
        "headers": headers,
        "is_draft": m.get("isDraft", False),
    }


def utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
