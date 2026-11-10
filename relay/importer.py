"""Import contacts from CSV or Excel with automatic column matching."""
import csv
import io
import re

from . import classify
from .db import now_iso

FIELDS = {
    "first_name": ["first name", "first", "firstname", "given name", "fname"],
    "last_name": ["last name", "last", "lastname", "surname", "family name", "lname"],
    "full_name": ["name", "full name", "contact", "contact name", "display name"],
    "email": ["email", "e-mail", "email address", "e-mail address", "email 1", "primary email", "work email"],
    "phone": ["phone", "work phone", "business phone", "phone number", "office phone", "telephone"],
    "mobile": ["mobile", "cell", "mobile phone", "cell phone"],
    "title": ["title", "job title", "position", "role"],
    "org": ["company", "organization", "organisation", "agency", "account", "department", "company name"],
    "tags": ["tags", "category", "categories", "group", "groups"],
    "notes": ["notes", "note", "comments", "description"],
    "lifecycle": ["status", "stage", "lifecycle", "type"],
}
LABELS = {"first_name": "First name", "last_name": "Last name", "full_name": "Full name", "email": "Email",
          "phone": "Phone", "mobile": "Mobile", "title": "Title", "org": "Organization", "tags": "Tags",
          "notes": "Notes", "lifecycle": "Status", "": "(ignore)"}


def _norm(h):
    return re.sub(r"[^a-z0-9 ]", "", (h or "").lower().replace("_", " ")).strip()


def read_rows(filename, data: bytes):
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.active
        rows = [["" if v is None else str(v).strip() for v in r] for r in ws.iter_rows(values_only=True)]
        rows = [r for r in rows if any(r)]
    elif name.endswith(".csv") or name.endswith(".txt"):
        text = None
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        rows = [[c.strip() for c in r] for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    else:
        raise ValueError("Please upload a .csv or .xlsx file. (Old .xls files: open in Excel and Save As .xlsx.)")
    if not rows:
        raise ValueError("That file looks empty.")
    headers, body = rows[0], rows[1:]
    width = len(headers)
    body = [(r + [""] * width)[:width] for r in body]
    return headers, body


def guess_mapping(headers):
    mapping, used = [], set()
    for h in headers:
        n = _norm(h)
        hit = ""
        for field, names in FIELDS.items():
            if field in used:
                continue
            if n in names or n == field.replace("_", " "):
                hit = field
                break
        if not hit and "mail" in n and "email" not in used:
            hit = "email"
        if hit:
            used.add(hit)
        mapping.append(hit)
    return mapping


def run_import(db, headers, rows, mapping, update_existing=True, tag=""):
    from .sync import Engine
    eng = Engine(db)
    res = {"created": 0, "updated": 0, "skipped": 0, "errors": []}
    for i, r in enumerate(rows, start=2):
        rec = {}
        for col, field in enumerate(mapping):
            if field and col < len(r) and r[col]:
                rec[field] = r[col].strip()
        email = (rec.get("email") or "").lower().strip()
        m = re.search(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+", email)
        email = m.group(0) if m else ""
        if not email and not (rec.get("first_name") or rec.get("last_name") or rec.get("full_name")):
            res["skipped"] += 1
            continue
        if rec.get("full_name") and not (rec.get("first_name") or rec.get("last_name")):
            rec["first_name"], rec["last_name"] = classify.name_from_email(email, rec["full_name"])
        org_id = None
        if rec.get("org"):
            o = db.one("SELECT id FROM orgs WHERE lower(name)=lower(?)", (rec["org"],))
            if o:
                org_id = o["id"]
            else:
                dom = classify.base_domain(classify.split_email(email)[1]) if email else None
                if dom and dom not in classify.FREE_MAIL:
                    o = db.one("SELECT id FROM orgs WHERE domain=?", (dom,))
                    if o:
                        org_id = o["id"]
                if not org_id:
                    org_id = db.ex("INSERT INTO orgs(name, domain, org_type, created_at) VALUES(?,?,?,?)",
                                   (rec["org"], dom if dom and dom not in classify.FREE_MAIL else None,
                                    classify.classify_domain(dom) if dom else "Other", now_iso()))
        elif email:
            org_id = eng.get_or_create_org(classify.split_email(email)[1])
        tags = ",".join(t for t in [rec.get("tags", ""), tag] if t)
        life = rec.get("lifecycle") if rec.get("lifecycle") in ("Lead", "Prospect", "Customer", "Partner", "Inactive") else None
        existing = db.one("SELECT * FROM contacts WHERE email=?", (email,)) if email else None
        try:
            if existing:
                if not update_existing:
                    res["skipped"] += 1
                    continue
                vals = {k: rec.get(k) for k in ("first_name", "last_name", "phone", "mobile", "title", "notes") if rec.get(k)}
                if org_id:
                    vals["org_id"] = org_id
                if life:
                    vals["lifecycle"] = life
                if tags:
                    merged = sorted({t.strip() for t in (existing["tags"] or "").split(",") + tags.split(",") if t.strip()})
                    vals["tags"] = ",".join(merged)
                if vals:
                    db.ex(f"UPDATE contacts SET {', '.join(k + '=?' for k in vals)} WHERE id=?", (*vals.values(), existing["id"]))
                res["updated"] += 1
            else:
                db.ex("INSERT INTO contacts(first_name,last_name,email,phone,mobile,title,org_id,lifecycle,tags,notes,source,created_at)"
                      " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      (rec.get("first_name", ""), rec.get("last_name", ""), email or None, rec.get("phone"),
                       rec.get("mobile"), rec.get("title"), org_id, life or "Lead", tags, rec.get("notes", ""),
                       "import", now_iso()))
                res["created"] += 1
        except Exception as e:
            res["errors"].append(f"Row {i}: {e}")
    return res
