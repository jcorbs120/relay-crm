"""Small helpers shared by the view modules."""
import csv
import io
import re

from flask import Response, g, request

from . import auth, db


def display_name(c):
    if not c:
        return ""
    n = f"{c['first_name'] or ''} {c['last_name'] or ''}".strip()
    return n or (c["email"] or "(no name)")


def me():
    return g.user


def my_id():
    return g.user["id"] if getattr(g, "user", None) is not None else None


def audit(action, entity="", entity_id=None, detail=""):
    db.audit(getattr(g, "user", None), action, entity, entity_id, detail, auth.client_ip())


def safe_next(url, fallback="/"):
    """Only allow same-site relative redirects."""
    if url and url.startswith("/") and not url.startswith("//") and "\\" not in url:
        return url
    return fallback


def csv_response(name, header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(list(header))
    for r in rows:
        # neutralise spreadsheet formula injection
        w.writerow([("'" + v) if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") else v for v in r])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


def split_addresses(s):
    return [a.strip().lower() for a in re.split(r"[;,\s]+", s or "") if "@" in a]


# ------------------------------------------------------------------ ownership filters
def scope(default=None):
    """Read ?owner= and return (filter_value, selected_key).

    filter_value: None (everyone) | "none" (unassigned) | int user id
    selected_key: "all" | "mine" | "none" | "<id>"
    """
    v = request.args.get("owner")
    if v is None or v == "":
        v = default or (g.user["default_scope"] if g.user and g.user["default_scope"] in ("mine", "team") else "mine")
        v = "mine" if v == "mine" else "all"
    if v == "mine":
        return g.user["id"], "mine"
    if v == "none":
        return "none", "none"
    if v.isdigit():
        return int(v), v
    return None, "all"


def owner_sql(col, value):
    if value is None:
        return "", []
    if value == "none":
        return f" AND {col} IS NULL", []
    return f" AND {col} = ?", [value]


def team_members(include_viewers=False):
    rows = db.users(active_only=True)
    return [u for u in rows if include_viewers or u["role"] != "viewer"]


def user_names():
    if not hasattr(g, "_unames"):
        g._unames = {u["id"]: (u["name"] or u["email"]) for u in db.users(active_only=False)}
    return g._unames


def uname(uid):
    if not uid:
        return ""
    return user_names().get(uid, "former user")


def form_owner(f, key="owner_id", default=None):
    """Owner chosen in a form; must be an active team member."""
    v = f.get(key, "")
    if v == "":
        return default
    if v == "none":
        return None
    try:
        uid = int(v)
    except ValueError:
        return default
    u = db.user(uid)
    return uid if u and u["active"] else default
