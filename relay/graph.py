"""Microsoft 365 (Outlook) access through Microsoft Graph.

Sign-in uses the device-code flow: the app shows a short code, the user enters
it at microsoft.com/devicelogin in their browser and signs in normally (MFA,
PIV/CAC, conditional access all work). No password ever touches this app.
Tokens are cached in the user's data folder and refreshed automatically.
"""
import json
import logging
import re
import threading
from datetime import datetime, timedelta, timezone

import requests

from . import paths

log = logging.getLogger("relay.graph")

CLOUDS = {
    # name: (authority host, graph host)
    "commercial": ("https://login.microsoftonline.com", "https://graph.microsoft.com"),
    "gcc": ("https://login.microsoftonline.com", "https://graph.microsoft.com"),
    "gcchigh": ("https://login.microsoftonline.us", "https://graph.microsoft.us"),
    "dod": ("https://login.microsoftonline.us", "https://dod-graph.microsoft.us"),
}
CLOUD_LABELS = {
    "commercial": "Microsoft 365 (commercial)",
    "gcc": "Microsoft 365 GCC (government community cloud)",
    "gcchigh": "Microsoft 365 GCC High",
    "dod": "Microsoft 365 DoD",
}
SCOPE_NAMES = ["User.Read", "Mail.ReadWrite", "Mail.Send"]
SELECT = ("id,internetMessageId,conversationId,subject,bodyPreview,body,from,sender,toRecipients,"
          "ccRecipients,bccRecipients,receivedDateTime,sentDateTime,isDraft")


class GraphError(Exception):
    pass


class NotConnected(GraphError):
    pass


class M365:
    def __init__(self, db):
        self.db = db
        self._flow = None
        self._flow_state = {"status": "idle"}   # idle / pending / done / error
        self._lock = threading.Lock()

    # ---------- config ----------
    @property
    def cloud(self):
        return self.db.get("m365_cloud", "commercial") or "commercial"

    @property
    def hosts(self):
        return CLOUDS.get(self.cloud, CLOUDS["commercial"])

    @property
    def scopes(self):
        graph = self.hosts[1]
        if graph == "https://graph.microsoft.com":
            return SCOPE_NAMES
        return [f"{graph}/{s}" for s in SCOPE_NAMES]

    def configured(self):
        return bool(self.db.get("m365_client_id") and self.db.get("m365_tenant_id"))

    def _app(self):
        import msal  # imported lazily so demo mode never needs it
        if not self.configured():
            raise NotConnected("Microsoft 365 is not set up yet. Add the Tenant ID and Client ID in Settings.")
        cache = msal.SerializableTokenCache()
        p = paths.token_cache_path()
        if p.exists():
            try:
                cache.deserialize(p.read_text())
            except Exception:
                log.warning("token cache unreadable; ignoring")
        authority = f"{self.hosts[0]}/{self.db.get('m365_tenant_id').strip()}"
        app = msal.PublicClientApplication(
            self.db.get("m365_client_id").strip(), authority=authority, token_cache=cache,
            instance_discovery=False if self.cloud in ("gcchigh", "dod") else None,
        )
        return app, cache

    def _save(self, cache):
        if cache.has_state_changed:
            p = paths.token_cache_path()
            p.write_text(cache.serialize())
            try:
                p.chmod(0o600)
            except Exception:
                pass

    # ---------- auth ----------
    def account(self):
        try:
            app, _ = self._app()
        except NotConnected:
            return None
        accts = app.get_accounts()
        return accts[0] if accts else None

    def token(self):
        app, cache = self._app()
        accts = app.get_accounts()
        if not accts:
            raise NotConnected("Not signed in to Microsoft 365. Go to Settings and click Sign in.")
        res = app.acquire_token_silent(self.scopes, account=accts[0])
        self._save(cache)
        if not res or "access_token" not in res:
            raise NotConnected("Microsoft 365 sign-in expired. Go to Settings and sign in again.")
        return res["access_token"]

    def start_device_login(self):
        """Begin sign-in. Returns {user_code, verification_uri, message}."""
        try:
            app, cache = self._app()
        except ValueError:
            raise GraphError("Microsoft didn't recognize that Directory (tenant) ID for the selected cloud. "
                             "Check the ID and the Microsoft cloud setting with your IT admin.")
        flow = app.initiate_device_flow(scopes=self.scopes)
        if "user_code" not in flow:
            desc = flow.get("error_description") or "Could not start Microsoft sign-in."
            if "AADSTS700016" in desc:
                desc = "Microsoft didn't find that Application (client) ID in your tenant. Check it with your IT admin."
            elif "AADSTS7000218" in desc or "AADSTS70002" in desc:
                desc = ("The app registration must allow public client flows: in Azure > App registrations > "
                        "Authentication, set 'Allow public client flows' to Yes.")
            raise GraphError(desc.split(" Trace ID")[0])
        with self._lock:
            self._flow_state = {"status": "pending", "user_code": flow["user_code"],
                                "verification_uri": flow.get("verification_uri"),
                                "message": flow.get("message")}

        def wait():
            try:
                res = app.acquire_token_by_device_flow(flow)  # blocks until done/expired
                self._save(cache)
                if "access_token" in res:
                    claims = res.get("id_token_claims") or {}
                    email = claims.get("preferred_username") or claims.get("email") or ""
                    if email and not self.db.get("my_email"):
                        self.db.set("my_email", email.lower())
                    if claims.get("name") and not self.db.get("my_name"):
                        self.db.set("my_name", claims["name"])
                    with self._lock:
                        self._flow_state = {"status": "done", "account": email}
                else:
                    with self._lock:
                        self._flow_state = {"status": "error",
                                            "error": res.get("error_description") or res.get("error")}
            except Exception as e:  # pragma: no cover
                with self._lock:
                    self._flow_state = {"status": "error", "error": str(e)}

        threading.Thread(target=wait, daemon=True).start()
        return dict(self._flow_state)

    def login_status(self):
        with self._lock:
            return dict(self._flow_state)

    def sign_out(self):
        p = paths.token_cache_path()
        if p.exists():
            p.unlink()
        self._flow_state = {"status": "idle"}

    # ---------- HTTP ----------
    def _req(self, method, path_or_url, **kw):
        url = path_or_url if path_or_url.startswith("http") else f"{self.hosts[1]}/v1.0{path_or_url}"
        headers = kw.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.token()}"
        for attempt in range(4):
            r = requests.request(method, url, headers=headers, timeout=60, **kw)
            if r.status_code in (429, 503, 504) and attempt < 3:
                import time
                time.sleep(min(int(r.headers.get("Retry-After", "5")), 30))
                continue
            break
        if r.status_code >= 400:
            try:
                msg = r.json().get("error", {}).get("message", r.text)
            except Exception:
                msg = r.text
            raise GraphError(f"Microsoft Graph {r.status_code}: {msg[:300]}")
        return r.json() if r.content else {}

    def me(self):
        return self._req("GET", "/me?$select=displayName,mail,userPrincipalName,proxyAddresses")

    def find_folder(self, name):
        """Find a mail folder by display name (top level, or directly under Inbox)."""
        if not name:
            return None
        safe = name.replace("'", "''")
        res = self._req("GET", f"/me/mailFolders?$filter=displayName eq '{safe}'&$select=id,displayName")
        if res.get("value"):
            return res["value"][0]["id"]
        res = self._req("GET", f"/me/mailFolders/inbox/childFolders?$filter=displayName eq '{safe}'&$select=id")
        if res.get("value"):
            return res["value"][0]["id"]
        return None

    def list_messages(self, folder, since: datetime, limit=1000):
        """folder: 'inbox', 'sentitems' or a folder id. Yields normalized message dicts."""
        field = "sentDateTime" if folder == "sentitems" else "receivedDateTime"
        since_s = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        url = (f"/me/mailFolders/{folder}/messages?$select={SELECT}&$top=50"
               f"&$filter={field} ge {since_s}&$orderby={field} asc")
        n = 0
        while url and n < limit:
            res = self._req("GET", url, headers={"Prefer": 'outlook.body-content-type="text"'})
            for m in res.get("value", []):
                if m.get("isDraft"):
                    continue
                n += 1
                yield normalize(m)
            url = res.get("@odata.nextLink")

    def send(self, to, subject, body, cc=None, html=False):
        """Create a draft then send it, so we know the internetMessageId to dedupe against Sent Items."""
        def rcpts(lst):
            return [{"emailAddress": {"address": a}} for a in lst if a]
        msg = {"subject": subject,
               "body": {"contentType": "HTML" if html else "Text", "content": body},
               "toRecipients": rcpts(to), "ccRecipients": rcpts(cc or [])}
        draft = self._req("POST", "/me/messages", json=msg)
        self._req("POST", f"/me/messages/{draft['id']}/send")
        return {"internet_message_id": draft.get("internetMessageId"),
                "conversation_id": draft.get("conversationId")}


def _addr(x):
    ea = (x or {}).get("emailAddress") or {}
    return ((ea.get("address") or "").strip().lower(), (ea.get("name") or "").strip())


def normalize(m):
    """Graph message -> the plain dict the sync engine understands."""
    body = ((m.get("body") or {}).get("content") or "")
    if (m.get("body") or {}).get("contentType", "").lower() == "html":
        body = re.sub(r"<[^>]+>", " ", body)
    return {
        "id": m.get("internetMessageId") or m.get("id"),
        "conversation_id": m.get("conversationId"),
        "subject": m.get("subject") or "(no subject)",
        "preview": m.get("bodyPreview") or "",
        "body": body[:20000],
        "from": _addr(m.get("from") or m.get("sender")),
        "to": [_addr(x) for x in m.get("toRecipients") or []],
        "cc": [_addr(x) for x in m.get("ccRecipients") or []],
        "bcc": [_addr(x) for x in m.get("bccRecipients") or []],
        "date": m.get("sentDateTime") or m.get("receivedDateTime"),
    }
