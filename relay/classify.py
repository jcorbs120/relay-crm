"""Turn an email address into an organization name + public-sector type."""
import re

FREE_MAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com", "live.com", "msn.com",
    "aol.com", "icloud.com", "me.com", "mac.com", "protonmail.com", "proton.me", "gmx.com", "zoho.com",
    "comcast.net", "verizon.net", "att.net", "sbcglobal.net", "ymail.com", "mail.com",
}

# Addresses that are never real people.
SKIP_LOCAL = re.compile(
    r"^(no-?reply|do-?not-?reply|donotreply|mailer-daemon|postmaster|bounce[s]?|notifications?|"
    r"newsletter|news|alerts?|marketing|info-?noreply|automated|system|calendar|unsubscribe|"
    r"updates|digest|support-noreply|billing-noreply)([+._-].*)?$",
    re.I,
)
SKIP_DOMAIN_HINTS = ("mailchimp", "sendgrid", "constantcontact", "mktomail", "hubspotemail",
                     "list-manage", "govdelivery", "salesforce.com", "amazonses", "mailgun")

FEDERAL_GOV = {
    "dhs", "cisa", "fema", "gsa", "va", "hhs", "cdc", "nih", "cms", "fda", "dot", "faa", "doe", "energy",
    "doj", "justice", "fbi", "usdoj", "treasury", "irs", "state", "usda", "doi", "interior", "epa", "nasa",
    "nsf", "opm", "sba", "ssa", "hud", "ed", "dol", "commerce", "census", "noaa", "nist", "uspto", "usps",
    "usaid", "fdic", "sec", "ftc", "fcc", "nrc", "dhs", "tsa", "cbp", "ice", "uscis", "usss", "secretservice",
    "whitehouse", "omb", "senate", "house", "congress", "gao", "loc", "archives", "nara", "usmint", "bls",
    "usgs", "nps", "fws", "blm", "uscourts", "dodiis", "disa", "defense", "usace", "army", "navy", "af",
    "usmc", "uscg", "nga", "nro", "dni", "cia", "nsa", "osti", "ornl", "anl", "lanl", "llnl", "pnnl",
    "cbo", "fhfa", "cfpb", "consumerfinance", "occ", "federalreserve", "frb", "eeoc", "nlrb", "osha",
    "msha", "ahrq", "hrsa", "samhsa", "acf", "ihs", "aoc", "gpo", "usa", "login", "sam", "fedramp",
    "digital", "cio", "performance", "data", "challenge", "ustr", "exim", "peacecorps", "amtrak",
    "tva", "fhwa", "fta", "nhtsa", "fra", "phmsa", "marad", "atf", "dea", "usmarshals", "bop",
}

STATES = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas", "ca": "california",
    "co": "colorado", "ct": "connecticut", "de": "delaware", "fl": "florida", "ga": "georgia",
    "hi": "hawaii", "id": "idaho", "il": "illinois", "in": "indiana", "ia": "iowa", "ks": "kansas",
    "ky": "kentucky", "la": "louisiana", "me": "maine", "md": "maryland", "ma": "massachusetts",
    "mi": "michigan", "mn": "minnesota", "ms": "mississippi", "mo": "missouri", "mt": "montana",
    "ne": "nebraska", "nv": "nevada", "nh": "newhampshire", "nj": "newjersey", "nm": "newmexico",
    "ny": "newyork", "nc": "northcarolina", "nd": "northdakota", "oh": "ohio", "ok": "oklahoma",
    "or": "oregon", "pa": "pennsylvania", "ri": "rhodeisland", "sc": "southcarolina",
    "sd": "southdakota", "tn": "tennessee", "tx": "texas", "ut": "utah", "vt": "vermont",
    "va": "virginia", "wa": "washington", "wv": "westvirginia", "wi": "wisconsin", "wy": "wyoming",
    "dc": "dc", "pr": "puertorico", "gu": "guam", "vi": "virginislands",
}
STATE_NAMES = set(STATES.values()) | {"mass", "penn", "calif", "wisc"}
# Big-city / county .gov sites that would otherwise look federal or state.
KNOWN_LOCAL = {"nyc.gov", "lacity.org", "chicago.gov", "houstontx.gov", "phila.gov", "seattle.gov",
               "sfgov.org", "boston.gov", "denvergov.org", "austintexas.gov", "dc.gov", "lacounty.gov",
               "miamidade.gov", "cookcountyil.gov", "kingcounty.gov", "baltimorecity.gov",
               "sandiego.gov", "portland.gov", "atlantaga.gov", "detroitmi.gov"}
LOCAL_HINT = re.compile(r"(city|county|cnty|town|village|township|twp|borough|parish|metro|cityof|countyof|"
                        r"municipal|sheriff|police|fire|water|transit|schools?|isd|usd)", re.I)
EDU_HINT = re.compile(r"(k12|isd|usd|schools?|district)", re.I)

PRETTY = {
    "dhs": "Department of Homeland Security", "cisa": "Cybersecurity & Infrastructure Security Agency",
    "gsa": "General Services Administration", "va": "Department of Veterans Affairs",
    "hhs": "Department of Health & Human Services", "doe": "Department of Energy",
    "energy": "Department of Energy", "nasa": "NASA", "epa": "Environmental Protection Agency",
    "usda": "Department of Agriculture", "dot": "Department of Transportation",
    "treasury": "Department of the Treasury", "irs": "Internal Revenue Service",
    "doj": "Department of Justice", "usdoj": "Department of Justice", "state": "Department of State",
    "army": "U.S. Army", "navy": "U.S. Navy", "af": "U.S. Air Force", "disa": "Defense Information Systems Agency",
    "fema": "FEMA", "cdc": "CDC", "nih": "NIH", "cms": "Centers for Medicare & Medicaid Services",
    "opm": "Office of Personnel Management", "sba": "Small Business Administration",
    "ssa": "Social Security Administration", "census": "U.S. Census Bureau", "noaa": "NOAA",
}


def split_email(addr: str):
    addr = (addr or "").strip().lower()
    if "@" not in addr:
        return addr, ""
    local, domain = addr.rsplit("@", 1)
    return local, domain


def is_automated(addr: str) -> bool:
    local, domain = split_email(addr)
    if not domain:
        return True
    if SKIP_LOCAL.match(local):
        return True
    return any(h in domain for h in SKIP_DOMAIN_HINTS)


def base_domain(domain: str) -> str:
    """mail.cisa.dhs.gov -> dhs.gov? No: keep agency-level for .gov (cisa.dhs.gov) but strip mail hosts."""
    parts = domain.lower().split(".")
    while len(parts) > 2 and parts[0] in ("mail", "email", "smtp", "exchange", "mx", "o365", "us"):
        parts = parts[1:]
    if parts[-1] in ("gov", "mil", "edu"):
        return ".".join(parts[-3:]) if len(parts) >= 3 else ".".join(parts)
    if parts[-1] == "us":
        return ".".join(parts)
    # two-level country TLDs like co.uk
    if len(parts) >= 3 and parts[-2] in ("co", "com", "gov", "ac", "org", "net"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def classify_domain(domain: str) -> str:
    d = (domain or "").lower().strip()
    if not d:
        return "Other"
    if d in FREE_MAIL:
        return "Individual"
    if d in KNOWN_LOCAL:
        return "Local"
    parts = d.split(".")
    tld = parts[-1]
    if tld == "mil":
        return "Federal"
    if tld == "edu":
        return "Education"
    if tld == "gov":
        labels = parts[:-1]
        sld = labels[-1]
        if sld in FEDERAL_GOV:            # checked first: va.gov is Veterans Affairs, not Virginia
            return "Federal"
        if EDU_HINT.search(sld):
            return "Education"
        if sld in STATES or sld in STATE_NAMES:   # ca.gov, dmv.ca.gov, michigan.gov, mass.gov
            return "State"
        if any(LOCAL_HINT.search(lbl) for lbl in labels):
            return "Local"
        if any(len(sld) > len(n) + 2 and sld.endswith(n) for n in STATE_NAMES):
            return "Local"                # austintexas.gov, raleighnc? (name glued to a state)
        return "Federal"
    if tld == "us":
        # state.tx.us -> State; ci.austin.tx.us / co.travis.tx.us -> Local; *.k12.xx.us -> Education
        if "k12" in parts:
            return "Education"
        if parts[0] in ("ci", "co", "city", "county", "town", "vil", "twp") or LOCAL_HINT.search(parts[0]):
            return "Local"
        if len(parts) >= 3 and parts[-2] in STATES:
            return "State"
        return "Other"
    if tld == "org":
        if LOCAL_HINT.search(parts[-2]):
            return "Local"
        return "Nonprofit"
    return "Commercial"


def org_name_for_domain(domain: str) -> str:
    d = base_domain(domain)
    parts = d.split(".")
    tld = parts[-1]
    if tld in ("gov", "mil") and len(parts) >= 2:
        key = parts[0] if parts[0] not in ("www",) else parts[1]
        if key in PRETTY:
            return PRETTY[key]
        if tld == "gov":
            sld = parts[-2]
            if sld in STATES:
                return f"State of {STATES[sld].title()}"
            if sld in STATE_NAMES and len(parts) == 2:
                return f"State of {sld.title()}"
        return _pretty(parts[-2]) if len(parts[-2]) > 4 else parts[-2].upper()
    if tld == "us" and len(parts) >= 3 and parts[-2] in STATES:
        st = parts[-2].upper()
        if "k12" in parts and parts[0] != "k12":
            return f"{_pretty(parts[0]) if len(parts[0]) > 4 else parts[0].upper()} ({st})"
        if parts[0] == "ci" and len(parts) >= 4:
            return f"City of {parts[1].title()}, {st}"
        if parts[0] == "co" and len(parts) >= 4:
            return f"{parts[1].title()} County, {st}"
        if parts[0] == "state":
            return f"State of {STATES[parts[-2]].title()}"
    core = parts[-2]
    return _pretty(core) if len(core) > 4 else core.upper()


def _pretty(label: str) -> str:
    """fairfaxcounty -> Fairfax County, cityofmadison -> City of Madison, austintexas -> Austin Texas."""
    s = label.replace("-", " ")
    s = re.sub(r"^(cityof|countyof|townof|villageof)", lambda m: m.group(1)[:-2] + " of ", s)
    s = re.sub(r"(?<=\w)(county|city|town|village|parish|schools|isd)$", r" \1", s)
    for n in sorted(STATE_NAMES, key=len, reverse=True):
        if len(s) > len(n) + 2 and s.endswith(n) and " " not in s:
            s = s[: -len(n)] + " " + n
            break
    return " ".join(w if w == "of" else w.title() for w in s.split())


def name_from_email(addr: str, display_name: str = ""):
    """Best-effort first/last name."""
    dn = (display_name or "").strip().strip('"').strip("'")
    if dn and "@" not in dn:
        if "," in dn:  # "Last, First" (common in .gov GAL)
            last, first = [x.strip() for x in dn.split(",", 1)]
            first = re.sub(r"\s*\(.*\)$", "", first).split(" ")[0]
            return first, last
        dn = re.sub(r"\s*\(.*?\)|\s*\[.*?\]", "", dn).strip()
        bits = dn.split()
        if len(bits) == 1:
            return bits[0], ""
        return bits[0], " ".join(bits[1:])
    local, _ = split_email(addr)
    local = re.sub(r"\d+$", "", local)
    bits = [b for b in re.split(r"[._-]+", local) if b]
    if len(bits) >= 2:
        return bits[0].title(), bits[-1].title()
    return (bits[0].title() if bits else ""), ""
