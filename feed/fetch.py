#!/usr/bin/env python3
"""Daily job feed for Sundesh's Apply Desk.

Reads the public job boards of the companies in feed/companies.json (Greenhouse, Lever, Ashby,
SmartRecruiters, Workable, Recruitee, Workday, Oracle HCM, Eightfold, Amazon, Google, Uber,
Atlassian) plus the monthly Hacker News "Who is hiring" thread, keeps senior software roles in
the target countries, scores them against Sundesh's profile and writes:
  out/jobs.json    - current matching jobs (the desk imports from here)
  out/status.json  - per-company result, so broken boards are easy to spot
  out/summary.md   - human-readable counts
Standard library only.
"""
import concurrent.futures as cf
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FEED = os.path.join(ROOT, "feed")
OUT = os.path.join(ROOT, "out")
UA = "Mozilla/5.0 (job-feed; personal job search; +https://github.com/sundeshchawla/job-feed)"
NOW = dt.datetime.now(dt.timezone.utc)
MAX_AGE_DAYS = 30
DISCOVERY_RETRY_DAYS = 14
PER_COMPANY_COUNTRY = 12
PER_COUNTRY = 250
MIN_FIT = 55

COUNTRIES = {
    "us": {"name": "United States", "iso3": "USA", "iso2": "US", "cities": ["Seattle", "New York", "San Francisco"]},
    "ca": {"name": "Canada", "iso3": "CAN", "iso2": "CA", "cities": ["Toronto", "Vancouver"]},
    "ie": {"name": "Ireland", "iso3": "IRL", "iso2": "IE", "cities": ["Dublin"]},
    "uk": {"name": "United Kingdom", "iso3": "GBR", "iso2": "GB", "cities": ["London"]},
    "au": {"name": "Australia", "iso3": "AUS", "iso2": "AU", "cities": ["Sydney", "Melbourne"]},
    "ch": {"name": "Switzerland", "iso3": "CHE", "iso2": "CH", "cities": ["Zurich"]},
    "ae": {"name": "United Arab Emirates", "iso3": "ARE", "iso2": "AE", "cities": ["Dubai", "Abu Dhabi"]},
    "lu": {"name": "Luxembourg", "iso3": "LUX", "iso2": "LU", "cities": ["Luxembourg"]},
    "nl": {"name": "Netherlands", "iso3": "NLD", "iso2": "NL", "cities": ["Amsterdam"]},
    "in": {"name": "India", "iso3": "IND", "iso2": "IN", "cities": ["Bengaluru", "Hyderabad", "Pune"]},
    "sg": {"name": "Singapore", "iso3": "SGP", "iso2": "SG", "cities": ["Singapore"]},
}

# Boards that discovery can't guess. Unverified entries simply fail and show up in status.json.
KNOWN = {
    "amazon": [{"type": "amazon"}],

    "google": [{"type": "google"}],

    "atlassian": [{"type": "atlassian"}],
    "jpmorgan chase": [{"type": "oracle", "host": "jpmc.fa.oraclecloud.com", "site": "CX_1001"}],
    "oracle": [{"type": "oracle", "host": "eeho.fa.us2.oraclecloud.com", "site": "CX_45001"}],
    "nvidia": [{"type": "workday", "tenant": "nvidia", "wd": "wd5", "site": "NVIDIAExternalCareerSite"}],
    "salesforce": [{"type": "workday", "tenant": "salesforce", "wd": "wd12", "site": "External_Career_Site"}],

    "workday": [{"type": "workday", "tenant": "workday", "wd": "wd5", "site": "Workday"}],
    "adobe": [{"type": "workday", "tenant": "adobe", "wd": "wd5", "site": "external_experienced"}],
    "mastercard": [{"type": "workday", "tenant": "mastercard", "wd": "wd1", "site": "CorporateCareers"}],
    "paypal": [{"type": "workday", "tenant": "paypal", "wd": "wd1", "site": "jobs"}],
    "state street": [{"type": "workday", "tenant": "statestreet", "wd": "wd1", "site": "Global"}],
    "deutsche bank": [{"type": "workday", "tenant": "db", "wd": "wd3", "site": "DBWebSite"}],
    "fidelity technology group": [{"type": "workday", "tenant": "fmr", "wd": "wd1", "site": "FidelityCareers"}],

    "accenture": [{"type": "workday", "tenant": "accenture", "wd": "wd103", "site": "AccentureCareers"}],
    "citi": [{"type": "workday", "tenant": "citi", "wd": "wd5", "site": "2"}],
    "barclays": [{"type": "workday", "tenant": "barclays", "wd": "wd3", "site": "External_Career_Site_Barclays"}],

    "visa": [{"type": "smartrecruiters", "slug": "Visa"}],
    "servicenow": [{"type": "smartrecruiters", "slug": "ServiceNow"}],
    "susquehanna": [],
    "wise": [],
    "block": [{"type": "greenhouse", "slug": "block"}],
    "afterpay": [{"type": "greenhouse", "slug": "block"}],
    "imc": [{"type": "greenhouse", "slug": "imc"}],
    "imc trading": [{"type": "greenhouse", "slug": "imc"}],
    "tower research": [{"type": "greenhouse", "slug": "towerresearchcapital"}],
    "just eat takeaway": [{"type": "smartrecruiters", "slug": "JustEatTakeaway"}],
    # wrong-company matches found by discovery - no public board for these
    "meta": [], "ey": [], "tcs": [], "jump": [], "zip": [], "glean": [], "picnic": [], "linkedin": [], "personio": [], "mantel": [],
    "bytedance": [], "ses": [], "booking.com": [], "uber": [], "microsoft": [], "morgan stanley": [], "walmart": [], "walmart global tech": [],
    "hsbc": [], "fidelity": [],
}

# ----------------------------------------------------------------------------------------------
# HTTP

def http(url, method="GET", body=None, headers=None, timeout=20):
    h = {"User-Agent": UA, "Accept": "application/json, text/html;q=0.8, */*;q=0.5"}
    if headers:
        h.update(headers)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return r.status, raw.decode("utf-8", "replace")


def get_json(url, **kw):
    status, txt = http(url, **kw)
    return json.loads(txt)


# ----------------------------------------------------------------------------------------------
# text helpers

def html_to_text(s):
    if not s:
        return ""
    s = html.unescape(str(s))
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?i)<li[^>]*>", "\n- ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</h\d>|</li>|<h\d[^>]*>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t ]+", " ", s)
    s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s)
    return s.strip()


def fnv_id(url):
    h = 2166136261
    for ch in url:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        h, r = divmod(h, 36)
        out = digits[r] + out
        if h == 0:
            break
    return "j" + out


TRACK = {"utm_source", "utm_medium", "utm_campaign", "refId", "trackingId", "trk", "src"}


def norm_url(u):
    u = (u or "").strip()
    try:
        p = urllib.parse.urlsplit(u)
        q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True) if k not in TRACK]
        path = p.path or "/"
        return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), path, urllib.parse.urlencode(q), ""))
    except Exception:
        return u


def parse_date(v):
    if v in (None, ""):
        return None
    try:
        if isinstance(v, (int, float)):
            return dt.datetime.fromtimestamp(v / 1000 if v > 1e11 else v, dt.timezone.utc)
        s = str(v).strip()
        m = re.match(r"Posted (\d+)\+? Days? Ago", s, re.I)
        if m:
            return NOW - dt.timedelta(days=int(m.group(1)))
        if re.match(r"Posted (Today|Yesterday)", s, re.I):
            return NOW - dt.timedelta(days=0 if "today" in s.lower() else 1)
        for fmt in ("%B %d, %Y", "%d %B %Y", "%Y-%m-%d"):
            try:
                return dt.datetime.strptime(s[:30], fmt).replace(tzinfo=dt.timezone.utc)
            except ValueError:
                pass
        s = s.replace("Z", "+00:00")
        d = dt.datetime.fromisoformat(s[:32])
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None


# ----------------------------------------------------------------------------------------------
# location -> country

COUNTRY_WORDS = [
    (r"\bunited states\b|\busa\b|\bu\.s\.\b", "us"), (r"\bcanada\b", "ca"), (r"\bireland\b", "ie"),
    (r"\bunited kingdom\b|\bengland\b|\bscotland\b|\bwales\b|\bgreat britain\b|,\s*uk\b|^uk\b", "uk"),
    (r"\baustralia\b", "au"), (r"\bswitzerland\b|\bschweiz\b|\bsuisse\b", "ch"),
    (r"\bunited arab emirates\b|\buae\b", "ae"), (r"\bluxembourg\b", "lu"),
    (r"\bnetherlands\b|\bnederland\b|\bholland\b", "nl"), (r"\bindia\b", "in"), (r"\bsingapore\b", "sg"),
]
CITY_WORDS = [
    (r"\b(seattle|bellevue|redmond|kirkland|new york|nyc|jersey city|san francisco|bay area|mountain view|sunnyvale|palo alto|san jose|menlo park)\b", "us"),
    (r"\b(toronto|vancouver|montr[eé]al|waterloo|ottawa|calgary|mississauga)\b", "ca"),
    (r"\b(dublin|cork|galway|limerick)\b", "ie"),
    (r"\b(london|edinburgh|glasgow|manchester|cambridge|bristol|leeds|belfast|oxford|reading)\b", "uk"),
    (r"\b(sydney|melbourne|brisbane|perth|canberra|adelaide)\b", "au"),
    (r"\b(z[uü]rich|geneva|gen[eè]ve|lausanne|basel|zug|bern|baden)\b", "ch"),
    (r"\b(dubai|abu dhabi)\b", "ae"),
    (r"\b(luxembourg)\b", "lu"),
    (r"\b(amsterdam|rotterdam|utrecht|eindhoven|the hague|den haag|amstelveen|veldhoven|leiden|delft)\b", "nl"),
    (r"\b(bengaluru|bangalore|hyderabad|pune|mumbai|gurugram|gurgaon|noida|chennai|delhi)\b", "in"),
    (r"\bsingapore\b", "sg"),
]
US_STATE = re.compile(r",\s*(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)\b")
US_HUB = re.compile(r"\b(seattle|bellevue|redmond|kirkland|new york|nyc|jersey city|san francisco|bay area|mountain view|sunnyvale|palo alto|san jose|menlo park)\b", re.I)


def countries_of(loc):
    """Set of target-country ids a location string refers to. US only counts in the main tech hubs."""
    out = set()
    for part in re.split(r"\s*[;|]\s*|\s+or\s+|\n", str(loc or "")):
        p = part.strip()
        if not p:
            continue
        low = p.lower()
        hit = None
        if US_STATE.search(p) or re.search(r"\bunited states\b|\busa\b", low):
            hit = "us" if US_HUB.search(p) else None
            if hit:
                out.add(hit)
            continue
        for rx, cid in COUNTRY_WORDS:
            if re.search(rx, low):
                hit = cid
                break
        if not hit:
            for rx, cid in CITY_WORDS:
                if re.search(rx, low):
                    hit = cid
                    break
        if hit == "us" and not US_HUB.search(p):
            hit = None
        if hit:
            out.add(hit)
    return out


# ----------------------------------------------------------------------------------------------
# scoring (mirrors the desk's title rules, plus description signals)

EXCLUDE = [
    r"\b(intern|internship|graduate|grad|junior|jr\.?|apprentice|student|trainee|werkstudent|entry[- ]level|early career)\b",
    r"^(?!.*\bsenior\b).*\b(software (development )?engineer|sde|engineer|developer)\s+i\b(?!i)",
    r"\b(engineering manager|manager,? software|software development manager|director|head of|vp of|chief|cto)\b",
    r"\b(data scientist|machine learning|ml engineer|mlops|ai researcher|research (scientist|engineer)|applied scientist)\b",
    r"\b(android|ios|mobile|embedded|firmware|hardware|fpga|asic|silicon|verification|analog|rf |mechanical|electrical|robotics|automotive)\b",
    r"\b(test engineer|qa|quality|sdet|automation tester|tester)\b",
    r"\b(support engineer|solutions? (engineer|architect)|sales|pre-?sales|customer|consultant|developer advocate|technical writer|implementation)\b",
    r"\b(salesforce developer|sap|dynamics|servicenow developer|sharepoint|power ?bi|bi developer|tableau developer|guidewire|pega|mainframe|cobol)\b",
    r"\b(game|unity developer|unreal|graphics|network engineer|data engineer|data analyst|analytics engineer|business analyst|product (manager|owner)|designer|scrum|project manager|delivery manager|recruit)\b",
    r"\b(security (analyst|engineer)|penetration|soc |cyber)\b",
    r"\b(php|wordpress|ruby|rails|golang|scala|kotlin|swift|flutter|react native|c\+\+|rust|elixir|haskell|perl)\b",
    r"\b(counsel|legal|recruit\w*|payroll|compliance|analyst|marketing|gtm|enablement|accountant|finance|program manager|specialist|coordinator|advisor|representative|privacy engineer)\b",
]


def title_excluded(t):
    if not re.search(r"\b(engineer|developer|sde|swe|programmer|architect|tech lead|technical lead)\b", t, re.I):
        return "not a software engineering title"
    for rx in EXCLUDE:
        m = re.search(rx, t, re.I)
        if m:
            if re.search(r"\.net|c#|dotnet", t, re.I) and re.search(r"java|python|react", t, re.I):
                continue
            return "title: " + m.group(0)
    if re.search(r"\bjava\b", t, re.I) and not re.search(r"\.net|c#|full ?stack", t, re.I):
        return "Java role"
    if re.search(r"\bpython\b", t, re.I) and not re.search(r"\.net|c#|full ?stack", t, re.I):
        return "Python role"
    return ""


def recipe_for(t):
    if re.search(r"\b(lead|staff|principal|architect)\b", t, re.I) and not re.search(r"\bsenior\b", re.sub("lead", "", t, flags=re.I), re.I):
        return "lead"
    if re.search(r"platform|devops|sre|site reliability|infrastructure|cloud engineer", t, re.I):
        return "platform"
    if re.search(r"\.net|c#|dotnet|back[- ]?end|backend", t, re.I):
        return "backend"
    return "fullstack"


POS = [
    (r"\bc#|\.net\b|dotnet|asp\.net", 14, ".NET/C#"), (r"\baws\b|amazon web services", 5, "AWS"),
    (r"kubernetes|\bk8s\b|\beks\b", 4, "Kubernetes"), (r"terraform", 3, "Terraform"),
    (r"microservice", 3, "microservices"), (r"distributed system", 4, "distributed systems"),
    (r"typescript", 3, "TypeScript"), (r"angular", 4, "Angular"), (r"sql server|postgres", 3, "SQL"),
    (r"\bredis\b", 2, "Redis"), (r"oauth|openid|\boidc\b|identity|authentication|\bsso\b", 3, "identity/auth"),
    (r"payment|fintech|banking|trading|financial services", 4, "fintech/payments"),
    (r"ci/cd|continuous (integration|delivery)|platform engineering", 2, "CI/CD"),
    (r"event[- ]driven|kafka|\bsqs\b|rabbitmq|message queue", 3, "event-driven"),
    (r"\bllm\b|genai|generative ai", 2, "LLM"), (r"full[- ]?stack", 3, "full-stack"),
]
NEG_SPONSOR = re.compile(r"unable to (offer|provide)? ?(visa )?sponsor|(cannot|can't|can not|do not|does not|don't|will not|won't|not able to|unable to) (offer |provide |support )?(visa |work permit )?sponsor|no (visa )?sponsorship|without (the need for )?(visa )?sponsorship|must (already )?have (the )?(full |existing )?right to work|existing right to work|must be (legally )?(eligible|authori[sz]ed) to work|permanent resident|citizens? only|citizenship (is )?required|security clearance|\bsc clearance|\bnv1\b|baseline clearance|\bus person", re.I)
POS_SPONSOR = re.compile(r"visa sponsorship (is )?(available|provided|offered)|(we|will) (can )?sponsor|sponsorship (is )?available|relocation (support|package|assistance)|support (with )?(your )?(visa|work permit)|visa support", re.I)
LANG_REQ = re.compile(r"(fluent|native|business[- ]level|proficient|excellent)[^.\n]{0,40}\b(german|dutch|french|arabic|mandarin|japanese)\b|\b(german|dutch|french|arabic)\b[^.\n]{0,25}\b(c1|c2|b2|fluent|native|required|mandatory|essential)\b", re.I)


def score(job, comp_meta):
    t = job["title"]
    why = title_excluded(t)
    if why:
        return None, why
    jd = job.get("jd") or ""
    low = jd.lower()
    s = 46
    notes = []
    if re.search(r"\b(senior|sr\.?|lead|tech lead|iii|ii|level (3|4)|l5|e5|sde ?2|sde ?3)\b", t, re.I):
        s += 12
    elif re.search(r"\b(staff|principal)\b", t, re.I):
        s += 6
    if re.search(r"\.net|c#|dotnet|asp\.net", t, re.I):
        s += 12
    if re.search(r"full[- ]?stack", t, re.I):
        s += 6
    if re.search(r"back[- ]?end|backend|platform|api|cloud|distributed|microservice", t, re.I):
        s += 5
    if re.search(r"front[- ]?end", t, re.I) and not re.search(r"full", t, re.I):
        s -= 6
    pts, hits = 0, []
    for rx, w, label in POS:
        if re.search(rx, low):
            pts += w
            hits.append(label)
    s += min(pts, 30)
    gaps = []
    if re.search(r"\bjava\b", low) and not re.search(r"\bc#|\.net\b|dotnet", low):
        s -= 8; gaps.append("Java")
    if re.search(r"\bspring\b", low) and not re.search(r"\bc#|\.net\b", low):
        s -= 3
    if re.search(r"golang|\bgo (developer|engineer|programming|services)|\bin go\b|\bgo,", low):
        s -= 6; gaps.append("Go")
    if re.search(r"\bscala\b", low):
        s -= 8; gaps.append("Scala")
    if re.search(r"\bruby\b|\brails\b", low):
        s -= 8; gaps.append("Ruby")
    if re.search(r"\bphp\b", low):
        s -= 10; gaps.append("PHP")
    if re.search(r"\bkotlin\b", low):
        s -= 5; gaps.append("Kotlin")
    if re.search(r"\breact\b", low) and not re.search(r"angular", low):
        s -= 2; gaps.append("React")
    if low.count("python") >= 3 and not re.search(r"\bc#|\.net\b", low):
        s -= 4; gaps.append("Python-heavy")
    if low.count("c++") >= 2:
        s -= 6; gaps.append("C++")
    sponsorship = "unclear"
    if NEG_SPONSOR.search(jd):
        sponsorship = "no"
    elif POS_SPONSOR.search(jd):
        sponsorship = "yes"; s += 6; notes.append("mentions visa/relocation support")
    if LANG_REQ.search(jd):
        s -= 15; gaps.append("local language required")
    yrs = [int(x) for x in re.findall(r"(\d{1,2})\+?\s*(?:-\s*\d{1,2}\s*)?years", low) if 0 < int(x) < 30]
    if yrs and max(yrs) >= 12:
        s -= 6; notes.append("asks for 12+ years")
    if comp_meta:
        rank, kind = comp_meta.get("rank", 99), comp_meta.get("kind", "bigtech")
        if kind == "services":
            s -= 8
        elif rank <= 10:
            s += 8
        elif rank <= 20:
            s += 4
    if job["country"] == "us" and sponsorship != "yes":
        s -= 4
    if job["country"] == "in" and (not comp_meta or comp_meta.get("kind") in ("bank", "services") or comp_meta.get("rank", 99) >= 60):
        s -= 10; notes.append("India role outside the FAANG/product target list - check pay reaches 60 LPA")
    s = max(0, min(96, s))
    return {
        "fit": s,
        "tier": "A" if s >= 75 and comp_meta and comp_meta.get("rank", 99) <= 20 else ("B" if s >= 64 else "C"),
        "recipe": recipe_for(t),
        "sponsorship": sponsorship,
        "fitWhy": (("Hacker News Who-is-hiring post (VISA). " if job.get("_hn") else "Company board. ") + "Matches: " + (", ".join(hits[:6]) or "general backend/full-stack") + "."
                   + (" Gaps: " + ", ".join(gaps) + "." if gaps else "")
                   + (" " + "; ".join(notes) + "." if notes else "")),
    }, ""


# ----------------------------------------------------------------------------------------------
# adapters: each returns a list of raw jobs {title, url, location, jd, postedAt, jobRef}
# For list-only APIs a `detail` callable fills jd lazily after title/location filtering.

def a_greenhouse(slug, **_):
    d = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    out = []
    for j in d.get("jobs", []):
        locs = [((j.get("location") or {}).get("name") or "")] + [o.get("location") or o.get("name") or "" for o in j.get("offices") or []]
        out.append({"title": j.get("title", ""), "url": j.get("absolute_url", ""), "location": " | ".join(x for x in locs if x),
                    "jd": html_to_text(j.get("content")), "postedAt": j.get("first_published") or j.get("updated_at"), "jobRef": str(j.get("requisition_id") or "")})
    return out


def gh_name(slug):
    try:
        return get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}").get("name", "")
    except Exception:
        return ""


def a_lever(slug, eu=False, **_):
    host = "api.eu.lever.co" if eu else "api.lever.co"
    d = get_json(f"https://{host}/v0/postings/{slug}?mode=json")
    out = []
    for j in d if isinstance(d, list) else []:
        cat = j.get("categories") or {}
        locs = [cat.get("location") or ""] + list(cat.get("allLocations") or [])
        jd = (j.get("descriptionPlain") or "") + "\n\n" + "\n\n".join((l.get("text") or "") + "\n" + html_to_text(l.get("content")) for l in j.get("lists") or []) + "\n\n" + (j.get("additionalPlain") or "")
        out.append({"title": j.get("text", ""), "url": j.get("hostedUrl", ""), "location": " | ".join(x for x in locs if x), "jd": jd.strip(), "postedAt": j.get("createdAt"), "jobRef": ""})
    return out


def a_ashby(slug, **_):
    d = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true")
    out = []
    for j in d.get("jobs", []):
        locs = [j.get("location") or ""] + [s.get("location") or "" for s in j.get("secondaryLocations") or []]
        addr = ((j.get("address") or {}).get("postalAddress") or {})
        if addr.get("addressCountry"):
            locs.append(", ".join(x for x in [addr.get("addressLocality"), addr.get("addressCountry")] if x))
        out.append({"title": j.get("title", ""), "url": j.get("jobUrl", ""), "location": " | ".join(x for x in locs if x),
                    "jd": j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")), "postedAt": j.get("publishedAt"), "jobRef": ""})
    return out


def a_smartrecruiters(slug, **_):
    out, offset = [], 0
    while offset < 500:
        d = get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&offset={offset}")
        items = d.get("content", [])
        for j in items:
            loc = j.get("location") or {}
            pid = j.get("id")
            out.append({"title": j.get("name", ""), "url": f"https://jobs.smartrecruiters.com/{slug}/{pid}",
                        "location": ", ".join(x for x in [loc.get("city"), loc.get("region"), loc.get("country")] if x) + (" (remote)" if loc.get("remote") else ""),
                        "jd": "", "postedAt": j.get("releasedDate"), "jobRef": j.get("refNumber") or "",
                        "_detail": ("sr", slug, pid), "_company": (j.get("company") or {}).get("name", "")})
        offset += 100
        if offset >= d.get("totalFound", 0) or not items:
            break
    return out


def d_smartrecruiters(slug, pid):
    d = get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{pid}")
    sec = ((d.get("jobAd") or {}).get("sections") or {})
    return "\n\n".join(html_to_text((sec.get(k) or {}).get("text")) for k in ("jobDescription", "qualifications", "additionalInformation"))


def a_workable(slug, **_):
    d = get_json(f"https://apply.workable.com/api/v3/accounts/{slug}/jobs", method="POST", body={"query": "", "location": [], "department": [], "worktype": [], "remote": []})
    out = []
    for j in d.get("results", []):
        loc = j.get("location") or {}
        sc = j.get("shortcode")
        out.append({"title": j.get("title", ""), "url": f"https://apply.workable.com/{slug}/j/{sc}/",
                    "location": ", ".join(x for x in [loc.get("city"), loc.get("region"), loc.get("country")] if x),
                    "jd": "", "postedAt": j.get("published"), "jobRef": "", "_detail": ("wk", slug, sc)})
    return out


def d_workable(slug, sc):
    d = get_json(f"https://apply.workable.com/api/v2/accounts/{slug}/jobs/{sc}")
    return "\n\n".join(html_to_text(d.get(k)) for k in ("description", "requirements", "benefits"))


def a_recruitee(slug, **_):
    d = get_json(f"https://{slug}.recruitee.com/api/offers/")
    out = []
    for j in d.get("offers", []):
        out.append({"title": j.get("title", ""), "url": j.get("careers_url", ""), "location": ", ".join(x for x in [j.get("location"), j.get("city"), j.get("country")] if x),
                    "jd": html_to_text((j.get("description") or "") + (j.get("requirements") or "")), "postedAt": j.get("published_at"), "jobRef": ""})
    return out


def a_workday(tenant, wd, site, **_):
    base = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
    out, seen = [], set()
    for q in ("software engineer", "software developer", ".net"):
        for off in range(0, 200, 20):
            d = get_json(base + "/jobs", method="POST", body={"appliedFacets": {}, "limit": 20, "offset": off, "searchText": q})
            posts = d.get("jobPostings", [])
            for j in posts:
                path = j.get("externalPath", "")
                if not path or path in seen:
                    continue
                seen.add(path)
                out.append({"title": j.get("title", ""), "url": f"https://{tenant}.{wd}.myworkdayjobs.com/en-US/{site}{path}", "location": j.get("locationsText", ""),
                            "jd": "", "postedAt": j.get("postedOn"), "jobRef": (j.get("bulletFields") or [""])[0], "_detail": ("wd", base, path)})
            if len(posts) < 20:
                break
    return out


def d_workday(base, path):
    d = get_json(base + path).get("jobPostingInfo", {})
    return html_to_text(d.get("jobDescription")), " | ".join([d.get("location") or ""] + list(d.get("additionalLocations") or []))


def a_oracle(host, site, **_):
    out = []
    for off in range(0, 300, 25):
        url = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList.secondaryLocations"
               f"&finder=findReqs;siteNumber={site},limit=25,offset={off},keyword=%22software%20engineer%22,sortBy=POSTING_DATES_DESC")
        d = get_json(url)
        reqs = ((d.get("items") or [{}])[0].get("requisitionList") or [])
        for j in reqs:
            locs = [j.get("PrimaryLocation") or ""] + [s.get("Name") or "" for s in j.get("secondaryLocations") or []]
            out.append({"title": j.get("Title", ""), "url": f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{j.get('Id')}",
                        "location": " | ".join(x for x in locs if x), "jd": "", "postedAt": j.get("PostedDate"), "jobRef": str(j.get("Id")),
                        "_detail": ("or", host, site, j.get("Id"))})
        if len(reqs) < 25:
            break
    return out


def d_oracle(host, site, rid):
    url = f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?expand=all&onlyData=true&finder=ById;Id=%22{rid}%22,siteNumber={site}"
    it = (get_json(url).get("items") or [{}])[0]
    return "\n\n".join(html_to_text(it.get(k)) for k in ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr"))


def a_eightfold(host, domain, **_):
    out, seen = [], set()
    for cid, c in COUNTRIES.items():
        for city in c["cities"][:1]:
            url = f"https://{host}/api/apply/v2/jobs?domain={domain}&start=0&num=50&location={urllib.parse.quote(city + ', ' + c['name'])}&query=software%20engineer&sort_by=timestamp"
            try:
                d = get_json(url)
            except Exception:
                continue
            for j in d.get("positions", []):
                if j.get("id") in seen:
                    continue
                seen.add(j.get("id"))
                out.append({"title": j.get("name", ""), "url": j.get("canonicalPositionUrl") or f"https://{host}/careers?pid={j.get('id')}",
                            "location": " | ".join(j.get("locations") or [j.get("location") or ""]), "jd": html_to_text(j.get("job_description")),
                            "postedAt": j.get("t_create"), "jobRef": str(j.get("display_job_id") or j.get("ats_job_id") or "")})
    if not out:
        raise RuntimeError("no positions")
    return out


def a_amazon(**_):
    out, seen = [], set()
    for cid, c in COUNTRIES.items():
        urls = ["https://www.amazon.jobs/en/search.json?base_query=software%20development%20engineer&result_limit=100&sort=recent&offset=0"
                f"&normalized_country_code%5B%5D={c['iso3']}",
                "https://www.amazon.jobs/en/search.json?base_query=software%20development%20engineer&result_limit=100&sort=recent&offset=0"
                f"&loc_query={urllib.parse.quote(c['cities'][0] + ', ' + c['name'])}"]
        jobs = []
        for u in urls:
            try:
                jobs += get_json(u).get("jobs", [])
            except Exception:
                pass
        for j in jobs:
            if j.get("job_path") in seen:
                continue
            seen.add(j.get("job_path"))
            jd = html_to_text(j.get("description")) + "\n\nBasic qualifications:\n" + html_to_text(j.get("basic_qualifications")) + "\n\nPreferred qualifications:\n" + html_to_text(j.get("preferred_qualifications"))
            out.append({"title": j.get("title", ""), "url": "https://www.amazon.jobs" + (j.get("job_path") or ""), "location": j.get("normalized_location") or j.get("location") or "",
                        "jd": jd, "postedAt": j.get("posted_date"), "jobRef": str(j.get("id_icims") or "")})
    if not out:
        raise RuntimeError("no jobs returned")
    return out


def a_google(**_):
    out = []
    for cid, c in COUNTRIES.items():
        for city in c["cities"][:2]:
            url = "https://www.google.com/about/careers/applications/jobs/results/?" + urllib.parse.urlencode({"q": "software engineer", "location": city + ", " + c["name"], "sort_by": "date"})
            status, page = http(url, headers={"Accept": "text/html"})
            for m in re.finditer(r'href="jobs/results/(\d+)-([a-z0-9-]+)[^"]*"[^>]*>', page):
                jid, slug = m.group(1), m.group(2)
                title = re.sub(r"\b(Iii|Ii|Iv)\b", lambda m: m.group(1).upper(), slug.replace("-", " ").title())
                out.append({"title": title, "url": f"https://www.google.com/about/careers/applications/jobs/results/{jid}-{slug}", "location": city + ", " + c["name"],
                            "jd": "", "postedAt": None, "jobRef": jid})
    if not out:
        raise RuntimeError("no results parsed")
    return out


def a_uber(**_):
    out = []
    for cid, c in COUNTRIES.items():
        body = {"limit": 50, "page": 0, "params": {"location": [{"country": c["iso3"]}], "department": ["Engineering"]}}
        d = get_json("https://www.uber.com/api/loadSearchJobsResults?localeCode=en", method="POST", body=body, headers={"x-csrf-token": "x"})
        for j in ((d.get("data") or {}).get("results") or []):
            locs = [", ".join(x for x in [(l or {}).get("city"), (l or {}).get("countryName")] if x) for l in (j.get("allLocations") or [j.get("location") or {}])]
            out.append({"title": j.get("title", ""), "url": f"https://www.uber.com/global/en/careers/list/{j.get('id')}/", "location": " | ".join(locs),
                        "jd": html_to_text(j.get("description")), "postedAt": j.get("creationDate"), "jobRef": str(j.get("id"))})
    return out


def a_atlassian(**_):
    d = get_json("https://www.atlassian.com/endpoint/careers/listings")
    out = []
    for j in d if isinstance(d, list) else []:
        jd = "\n\n".join(html_to_text(j.get(k)) for k in ("overview", "responsibilities", "qualifications"))
        url = ((j.get("portalJobPost") or {}).get("portalUrl")) or f"https://www.atlassian.com/company/careers/details/{j.get('id')}"
        out.append({"title": j.get("title", ""), "url": url, "location": " | ".join(j.get("locations") or []), "jd": jd, "postedAt": j.get("updatedDate"), "jobRef": str(j.get("id"))})
    return out


ADAPTERS = {"greenhouse": a_greenhouse, "lever": a_lever, "ashby": a_ashby, "smartrecruiters": a_smartrecruiters, "workable": a_workable,
            "recruitee": a_recruitee, "workday": a_workday, "oracle": a_oracle, "eightfold": a_eightfold, "amazon": a_amazon,
            "google": a_google, "uber": a_uber, "atlassian": a_atlassian}


def fill_detail(job):
    det = job.pop("_detail", None)
    if not det:
        return
    kind = det[0]
    if kind == "sr":
        job["jd"] = d_smartrecruiters(det[1], det[2])
    elif kind == "wk":
        job["jd"] = d_workable(det[1], det[2])
    elif kind == "wd":
        jd, locs = d_workday(det[1], det[2])
        job["jd"] = jd
        if locs.strip(" |"):
            job["location"] = locs
    elif kind == "or":
        job["jd"] = d_oracle(det[1], det[2], det[3])


# ----------------------------------------------------------------------------------------------
# discovery

def slugs_for(name):
    base = re.sub(r"[^a-z0-9 ]", "", name.lower().replace("&", "and")).strip()
    words = base.split()
    cands = []
    for s in ["".join(words), "-".join(words), words[0] if words else ""]:
        if s and s not in cands:
            cands.append(s)
    return cands


def name_ok(company, board_name):
    a = re.sub(r"[^a-z0-9]", "", company.lower())
    b = re.sub(r"[^a-z0-9]", "", (board_name or "").lower())
    if not b:
        return False
    first = re.sub(r"[^a-z0-9]", "", company.lower().split()[0]) if company.split() else a
    return a in b or b in a or (len(first) > 3 and b.startswith(first))


def discover(company):
    name = company["name"]
    full = re.sub(r"[^a-z0-9]", "", name.lower())
    tried = []
    for slug in slugs_for(name):
        exact = re.sub(r"[^a-z0-9]", "", slug) == full
        # Greenhouse: validate with the board's own name
        try:
            jobs = a_greenhouse(slug)
            if jobs and (exact or name_ok(name, gh_name(slug))):
                return {"type": "greenhouse", "slug": slug}
        except Exception:
            pass
        tried.append("greenhouse:" + slug)
        if not exact:
            continue  # other ATSs have no name endpoint - only trust an exact slug
        for t, fn in (("lever", lambda: a_lever(slug)), ("lever-eu", lambda: a_lever(slug, eu=True)), ("ashby", lambda: a_ashby(slug)),
                      ("recruitee", lambda: a_recruitee(slug))):
            try:
                if fn():
                    return {"type": "lever", "slug": slug, "eu": t == "lever-eu"} if t.startswith("lever") else {"type": t, "slug": slug}
            except Exception:
                pass
            tried.append(t + ":" + slug)
        for sr in {slug, name.replace(" ", ""), name.replace(" ", "").capitalize()}:
            try:
                jobs = a_smartrecruiters(sr)
                if jobs and name_ok(name, jobs[0].get("_company") or sr):
                    return {"type": "smartrecruiters", "slug": sr}
            except Exception:
                pass
        try:
            if a_workable(slug):
                return {"type": "workable", "slug": slug}
        except Exception:
            pass
    return None


# ----------------------------------------------------------------------------------------------

def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def main():
    t0 = time.time()
    companies = load_json(os.path.join(FEED, "companies.json"), [])
    cache = load_json(os.path.join(FEED, "ats_cache.json"), {})
    state = load_json(os.path.join(FEED, "state.json"), {"firstSeen": {}})
    first_seen = state.get("firstSeen", {})
    only = set(a.lower() for a in sys.argv[1:])
    if only:
        companies = [c for c in companies if c["name"].lower() in only]

    # 1. resolve a board for every company
    def resolve(c):
        key = c["name"].lower()
        if key in KNOWN:
            return c, KNOWN[key], "known"
        hit = cache.get(key)
        if hit and hit.get("board"):
            return c, [hit["board"]], "cache"
        if hit and not hit.get("board"):
            last = parse_date(hit.get("checkedAt"))
            if last and (NOW - last).days < DISCOVERY_RETRY_DAYS:
                return c, [], "cached-miss"
        b = discover(c)
        cache[key] = {"board": b, "checkedAt": NOW.isoformat()}
        return c, ([b] if b else []), "discovered" if b else "not-found"

    with cf.ThreadPoolExecutor(16) as ex:
        resolved = list(ex.map(resolve, companies))

    status = {}
    raw = []

    def run(item):
        c, boards, how = item
        res = {"company": c["name"], "how": how, "boards": boards, "ok": False, "total": 0, "inTarget": 0, "error": ""}
        if not boards:
            res["error"] = "no public board found"
            return res, []
        errs = []
        for b in boards:
            try:
                params = {k: v for k, v in b.items() if k != "type"}
                jobs = ADAPTERS[b["type"]](**params)
                res.update(ok=True, total=len(jobs), used=b)
                kept = []
                for j in jobs:
                    j["company"] = c["name"]
                    j["_meta"] = c["countries"]
                    kept.append(j)
                return res, kept
            except Exception as e:
                errs.append(f"{b['type']}: {type(e).__name__}: {str(e)[:120]}")
        res["error"] = "; ".join(errs)
        return res, []

    with cf.ThreadPoolExecutor(12) as ex:
        for res, jobs in ex.map(run, resolved):
            status[res["company"]] = res
            raw.extend(jobs)

    # 2. HN Who's hiring (VISA-tagged)
    try:
        raw.extend(hn_jobs())
        status["Hacker News: Who is hiring"] = {"company": "HN", "ok": True}
    except Exception as e:
        status["Hacker News: Who is hiring"] = {"company": "HN", "ok": False, "error": str(e)[:200]}

    # 3. title + location filter before fetching details
    cand = []
    for j in raw:
        if title_excluded(j.get("title", "")):
            continue
        cs = countries_of(j.get("location", ""))
        if not cs and j.get("_detail", ("",))[0] == "wd" and re.search(r"\d+ locations", j.get("location", ""), re.I):
            cs = {"?"}
        if not cs:
            continue
        d = parse_date(j.get("postedAt"))
        if d and (NOW - d).days > MAX_AGE_DAYS:
            continue
        j["_countries"] = cs
        cand.append(j)

    def detail(j):
        try:
            fill_detail(j)
        except Exception as e:
            j["_detailError"] = str(e)[:100]
        if "?" in j["_countries"]:
            j["_countries"] = countries_of(j.get("location", ""))
        return j

    with cf.ThreadPoolExecutor(12) as ex:
        cand = list(ex.map(detail, cand))

    # 4. score, one entry per (job, country)
    jobs = []
    for j in cand:
        meta_all = j.pop("_meta", {}) or {}
        for cid in sorted(j["_countries"]):
            if cid == "?":
                continue
            url = norm_url(j["url"])
            if not url:
                continue
            item = {"country": cid, "company": j["company"], "title": j["title"].strip(), "location": j.get("location", "")[:200], "url": url,
                    "jd": (j.get("jd") or "")[:6000], "jobRef": j.get("jobRef") or "", "postedAt": (parse_date(j.get("postedAt")) or NOW).date().isoformat() if j.get("postedAt") else ""}
            item["_hn"] = j.get("_source") == "hn"
            sc, why = score(item, meta_all.get(cid) or (meta_all and {"rank": 60, "kind": "bigtech"}) or None)
            item.pop("_hn", None)
            if not sc or sc["fit"] < MIN_FIT or (sc["sponsorship"] == "no" and cid != "in"):
                continue
            item.update(sc)
            item["source"] = "feed: " + j.get("_source", (status.get(j["company"], {}).get("used") or {}).get("type", "board"))
            # one id per country so the same multi-country posting can sit in two queues
            item["id"] = fnv_id(url) if len(j["_countries"]) == 1 else fnv_id(url + "#" + cid)
            item["firstSeen"] = first_seen.setdefault(item["id"], NOW.date().isoformat())
            jobs.append(item)

    # 5. caps
    jobs.sort(key=lambda x: x["firstSeen"], reverse=True)
    jobs.sort(key=lambda x: -x["fit"])
    per_cc, per_c, final, seen_ids = {}, {}, [], set()
    for j in jobs:
        if j["id"] in seen_ids:
            continue
        k = (j["company"], j["country"])
        if per_cc.get(k, 0) >= PER_COMPANY_COUNTRY or per_c.get(j["country"], 0) >= PER_COUNTRY:
            continue
        per_cc[k] = per_cc.get(k, 0) + 1
        per_c[j["country"]] = per_c.get(j["country"], 0) + 1
        seen_ids.add(j["id"])
        final.append(j)

    # prune firstSeen of ids gone for 45 days
    live = {j["id"] for j in final}
    cutoff = (NOW - dt.timedelta(days=45)).date().isoformat()
    state["firstSeen"] = {k: v for k, v in first_seen.items() if k in live or v >= cutoff}

    for c, res in status.items():
        if res.get("ok") and res.get("company") != "HN":
            res["inTarget"] = sum(1 for j in final if j["company"] == c)

    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "jobs.json"), "w") as f:
        json.dump({"generatedAt": NOW.isoformat(), "count": len(final), "byCountry": per_c, "jobs": final}, f, ensure_ascii=False)
    with open(os.path.join(OUT, "status.json"), "w") as f:
        json.dump({"generatedAt": NOW.isoformat(), "seconds": round(time.time() - t0), "rawJobs": len(raw), "candidates": len(cand), "kept": len(final), "companies": status}, f, ensure_ascii=False, indent=1)
    if not only:
        with open(os.path.join(FEED, "ats_cache.json"), "w") as f:
            json.dump(cache, f, indent=1, sort_keys=True)
        with open(os.path.join(FEED, "state.json"), "w") as f:
            json.dump(state, f)
    ok = sum(1 for r in status.values() if r.get("ok"))
    lines = [f"# Job feed - {NOW:%Y-%m-%d %H:%M} UTC", "", f"Boards read: {ok} of {len(status)} - raw jobs {len(raw)} - after title/location/date filters {len(cand)} - kept {len(final)}", "",
             "| Country | Jobs |", "|---|---|"] + [f"| {c} | {n} |" for c, n in sorted(per_c.items())]
    lines += ["", "## Boards with jobs kept", ""] + [f"- {r['company']} ({(r.get('used') or {}).get('type', '')}): {r.get('inTarget', 0)} of {r.get('total', 0)}" for r in sorted(status.values(), key=lambda r: -r.get("inTarget", 0)) if r.get("inTarget")]
    lines += ["", "## Not reachable", ""] + [f"- {r['company']}: {r.get('error', '')}" for r in status.values() if not r.get("ok")]
    with open(os.path.join(OUT, "summary.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines[:20]))


def hn_jobs():
    d = get_json("https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&hitsPerPage=6")
    story = next((h for h in d.get("hits", []) if "who is hiring" in (h.get("title") or "").lower()), None)
    if not story:
        return []
    item = get_json(f"https://hn.algolia.com/api/v1/items/{story['objectID']}")
    out = []
    for c in item.get("children", []):
        txt = html_to_text(c.get("text") or "")
        if not txt or not re.search(r"\bVISA\b", txt):
            continue
        first = txt.split("\n", 1)[0]
        parts = [p.strip() for p in first.split("|")]
        if len(parts) < 3:
            continue
        company = parts[0][:80]
        role = next((p for p in parts[1:] if re.search(r"engineer|developer|sde|swe", p, re.I)), parts[1])
        loc = " | ".join(p for p in parts[1:] if countries_of(p)) or first
        out.append({"title": role[:120], "url": f"https://news.ycombinator.com/item?id={c.get('id')}", "location": loc, "jd": txt[:6000],
                    "postedAt": c.get("created_at"), "jobRef": "", "company": company, "_meta": {}, "_source": "hn"})
    return out


if __name__ == "__main__":
    main()
