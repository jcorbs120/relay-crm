"""CSV / Excel contact import with automatic column mapping.
Works with exports from ACT!, Outlook, LinkedIn, Salesforce, HubSpot, Google."""
import csv
import io
import os
import re

from . import db
from .sync import get_or_create_org, domain_of

FIELDS = {
    "first_name": "First name",
    "last_name": "Last name",
    "full_name": "Full name (will be split)",
    "email": "Email",
    "title": "Job title",
    "phone": "Phone",
    "mobile": "Mobile",
    "organization": "Organization / Company",
    "org_type": "Organization type",
    "status": "Status",
    "tags": "Tags",
    "notes": "Notes",
}

ALIASES = {
    "first_name": ["first name", "firstname", "given name", "first", "contact first name"],
    "last_name": ["last name", "lastname", "surname", "family name", "last", "contact last name"],
    "full_name": ["name", "full name", "contact", "contact name", "display name"],
    "email": ["email", "e-mail", "email address", "e-mail address", "e-mail 1 - value", "email 1",
              "primary email", "work email", "e-mail 1", "emailaddress"],
    "title": ["title", "job title", "position", "role", "jobtitle"],
    "phone": ["phone", "business phone", "work phone", "phone number", "telephone", "phone 1 - value", "office phone"],
    "mobile": ["mobile", "mobile phone", "cell", "cell phone"],
    "organization": ["company", "organization", "organisation", "agency", "account", "account name",
                     "company name", "employer", "department"],
    "org_type": ["organization type", "org type", "agency type", "sector", "type"],
    "status": ["status", "lead status", "id/status", "contact status"],
    "tags": ["tags", "categories", "category", "labels", "groups"],
    "notes": ["notes", "note", "comments", "description"],
}


def _norm(h):
    return re.sub(r"\s+", " ", (h or "").strip().lower().replace("_", " "))


def read_file(path, filename):
    """Return (headers, rows[list of dict])."""
    ext = os.path.splitext(filename.lower())[1]
    if ext in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
        ws = wb.worksheets[0]
        it = ws.iter_rows(values_only=True)
        headers = [str(h).strip() if h is not None else f"Column {i+1}" for i, h in enumerate(next(it, []))]
        rows = []
        for r in it:
            if r is None or all(v in (None, "") for v in r):
                continue
            rows.append({headers[i]: ("" if v is None else str(v).strip()) for i, v in enumerate(r) if i < len(headers)})
        return headers, rows
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    headers = [h.strip() for h in (reader.fieldnames or [])]
    rows = [{(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in reader]
    return headers, rows


def auto_map(headers):
    mapping = {}
    normed = {_norm(h): h for h in headers}
    for field, names in ALIASES.items():
        for n in names:
            if n in normed and normed[n] not in mapping.values():
                mapping[field] = normed[n]
                break
    return mapping


def run_import(rows, mapping, update_existing=True, default_status="Lead", tag="", owner_id=None, created_by=None):
    """Import contacts. New contacts/organisations are owned by ``owner_id``; colleagues' addresses are skipped."""
    from .sync import team_directory
    team = team_directory()
    created = updated = skipped = 0
    errors = []
    now = db.now_iso()
    statuses = db.setting_list("contact_statuses")
    for i, r in enumerate(rows, start=2):
        get = lambda f: (r.get(mapping.get(f, ""), "") or "").strip() if mapping.get(f) else ""
        email = get("email").lower()
        first, last = get("first_name"), get("last_name")
        if not (first or last) and get("full_name"):
            parts = get("full_name").replace(",", " ").split()
            if "," in get("full_name"):
                last, first = get("full_name").split(",", 1)
                last, first = last.strip(), first.strip()
            elif parts:
                first, last = parts[0], " ".join(parts[1:])
        if not email and not (first or last):
            skipped += 1
            continue
        if email and not re.match(r"[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            errors.append(f"Row {i}: invalid email '{email}'")
            skipped += 1
            continue
        if email and email in team:
            errors.append(f"Row {i}: {email} is a team member - skipped")
            skipped += 1
            continue
        org_id = None
        org_name = get("organization")
        if org_name:
            row = db.query("SELECT id FROM organizations WHERE lower(name)=?", (org_name.lower(),), one=True)
            if row:
                org_id = row["id"]
            else:
                dom = domain_of(email) if email else None
                existing = db.query("SELECT id FROM organizations WHERE domain=?", (dom,), one=True) if dom else None
                if existing:
                    org_id = existing["id"]
                else:
                    from .sync import PUBLIC_DOMAINS
                    org_id = db.execute("INSERT INTO organizations(name, domain, org_type, owner_id, created_at) "
                                        "VALUES (?,?,?,?,?)",
                                        (org_name, dom if dom and dom not in PUBLIC_DOMAINS else None,
                                         get("org_type"), owner_id, now))
        elif email:
            org_id = get_or_create_org(domain_of(email), owner_id)

        status = get("status")
        status = next((s for s in statuses if s.lower() == status.lower()), default_status)
        tags = ",".join(t for t in [get("tags"), tag] if t)

        existing = db.query("SELECT * FROM contacts WHERE lower(email)=?", (email,), one=True) if email else None
        if existing:
            if not update_existing:
                skipped += 1
                continue
            vals = {"first_name": first, "last_name": last, "title": get("title"), "phone": get("phone"),
                    "mobile": get("mobile"), "notes": get("notes")}
            sets = {k: v for k, v in vals.items() if v}
            if org_id and not existing["org_id"]:
                sets["org_id"] = org_id
            if tags:
                merged = sorted(set(filter(None, (existing["tags"] or "").split(",") + tags.split(","))))
                sets["tags"] = ",".join(merged)
            if sets:
                sets["updated_at"] = now
                db.execute(f"UPDATE contacts SET {', '.join(k + '=?' for k in sets)} WHERE id=?",
                           (*sets.values(), existing["id"]))
            updated += 1
        else:
            db.execute("""INSERT INTO contacts(first_name,last_name,email,title,phone,mobile,org_id,status,source,
                          tags,notes,owner_id,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (first, last, email or None, get("title"), get("phone"), get("mobile"), org_id, status,
                        "Import", tags, get("notes"), owner_id, created_by, now, now))
            created += 1
    return {"created": created, "updated": updated, "skipped": skipped, "errors": errors[:50]}
