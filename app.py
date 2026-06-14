import os
import re
import json
import hashlib
import secrets
import threading
import csv
import io
import shutil
import sqlite3
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, jsonify, Response, redirect, url_for, session
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
import stripe
import db

load_dotenv()

_SOURCE_METADATA = None
_COUNTY_SCRAPER_MAP = None
OWNER_ADMIN_EMAILS = {"145brice@gmail.com"}
APP_BUILD = "admin-debug-2026-06-13-1"
DUVAL_TAX_CERTIFICATE_SALE_DATE = "2026-05-27"


def source_metadata():
    global _SOURCE_METADATA
    if _SOURCE_METADATA is None:
        from scrapers.source_registry import SOURCE_METADATA
        _SOURCE_METADATA = SOURCE_METADATA
    return _SOURCE_METADATA


def county_scraper_map():
    # Maps Flask county keys to scraper_runner county names. A single UI
    # selection may fan out into multiple scrapers.
    global _COUNTY_SCRAPER_MAP
    if _COUNTY_SCRAPER_MAP is None:
        from scrapers.source_registry import UI_COUNTY_SOURCES
        _COUNTY_SCRAPER_MAP = UI_COUNTY_SOURCES
    return _COUNTY_SCRAPER_MAP


def scraper_runtime():
    from scraper import scrape_sync
    from scraper_runner import run_scrapers
    from scrapers.base_scraper import request_kill, clear_kill, ScraperKilled
    return scrape_sync, run_scrapers, request_kill, clear_kill, ScraperKilled


def skiptrace_control():
    from scrapers import skiptrace_control as skiptrace_ctl
    return skiptrace_ctl


def property_records_to_listings(records: list[dict]) -> list[dict]:
    """Convert PropertyRecord dicts (from scrapers/) to the Flask listing format."""
    listings = []
    seen = set()
    for r in records:
        street   = r.get("property_address", "").strip()
        city     = r.get("city", "").strip()
        state    = r.get("state", "").strip()
        zip_code = r.get("zip_code", "").strip()
        parcel   = r.get("parcel_id", "").strip()

        if street:
            address = ", ".join(p for p in [street, city, state, zip_code] if p)
        elif parcel:
            # No street address; use APN so each parcel gets a unique listing
            address = ", ".join(p for p in [f"APN {parcel}", city, state, zip_code] if p)
        else:
            address = ", ".join(p for p in [city, state, zip_code] if p)

        if len(address) < 8:
            continue

        date_str = (r.get("sale_date") or r.get("scraped_date") or
                    datetime.now().strftime('%Y-%m-%d'))
        key = hashlib.md5(f"{address.lower().strip()}-{date_str}".encode()).hexdigest()[:8]
        if key in seen:
            continue
        seen.add(key)

        amount_str = r.get("amount_owed") or ""
        bid_digits = re.sub(r'[^\d]', '', amount_str)
        bid = int(bid_digits) if bid_digits else 0

        record_type = (r.get("record_type") or "").lower()
        if "delinquent" in record_type or "tax" in record_type:
            status = "Tax Lien"
        elif "foreclos" in record_type:
            status = "Pre-foreclosure"
        else:
            status = r.get("record_type") or "Tax Lien"

        county_raw = r.get("county") or ""
        listings.append({
            "id":           key,
            "address":      address,
            "street":       street,
            "city":         city,
            "state":        state,
            "zip":          zip_code,
            "owner":        r.get("owner_name", ""),
            "parcel_id":    r.get("parcel_id", ""),
            "case_number":  r.get("case_number", ""),
            "status":       status,
            "record_type":  r.get("record_type", ""),
            "price":        5,
            "bid":          bid,
            "amount_owed":  r.get("amount_owed", ""),
            "date":         date_str,
            "sale_date":    r.get("sale_date", ""),
            "scraped_date": r.get("scraped_date", ""),
            "county":       county_raw.lower(),
            "link":         r.get("source_url", ""),
            "source":    county_raw.title() + " Co.",
        })
    return listings

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "")
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")

# --- County subscriptions (tiered: Starter $99 / Professional $199 / Power $399) ----
# FULLY GATED: with ENABLE_SUBSCRIPTIONS unset the app behaves exactly as before.
# Flip the flag AND set the tier price IDs to activate.
ENABLE_SUBSCRIPTIONS = os.getenv("ENABLE_SUBSCRIPTIONS", "").lower() in ("1", "true", "yes", "on")
STRIPE_PRICE_TIER_STARTER = os.getenv("STRIPE_PRICE_TIER_STARTER", "")   # $99/mo
STRIPE_PRICE_TIER_PRO     = os.getenv("STRIPE_PRICE_TIER_PRO", "")       # $199/mo
STRIPE_PRICE_TIER_POWER   = os.getenv("STRIPE_PRICE_TIER_POWER", "")     # $399/mo
STRIPE_PRICE_TRACE_OVERAGE = os.getenv("STRIPE_PRICE_TRACE_OVERAGE", "") # $7/lead overage
STRIPE_PRICE_RAW_LEAD      = os.getenv("STRIPE_PRICE_RAW_LEAD", "")      # $2.50/raw lead

TIER_PRICES = {
    "starter":      STRIPE_PRICE_TIER_STARTER,
    "professional": STRIPE_PRICE_TIER_PRO,
    "power":        STRIPE_PRICE_TIER_POWER,
}
TIER_INCLUDED = {"starter": 8, "professional": 25, "power": 80}
TIER_LABELS   = {"starter": "Starter", "professional": "Professional", "power": "Power"}
TIER_AMOUNTS  = {"starter": 99, "professional": 199, "power": 399}

# Minimum untapped (un-skiptraced) leads a county must have to be offered for subscription.
# ~40 ensures a subscriber will get at least ~25 successful traces given a ~65% hit rate.
SUB_MIN_UNTAPPED = 40


def _subscriptions_ready():
    """True only when the flag is on, Stripe is keyed, and all three tier prices exist."""
    return bool(ENABLE_SUBSCRIPTIONS and stripe.api_key
                and STRIPE_PRICE_TIER_STARTER and STRIPE_PRICE_TIER_PRO and STRIPE_PRICE_TIER_POWER)

# ---- CSRF protection --------------------------------------------------------
# External callers that authenticate by other means (Stripe signs its webhook
# payloads) are exempt; everything else POSTed needs the session token, either
# as a csrf_token form field or an X-CSRF-Token header (for fetch calls).
CSRF_EXEMPT_PATHS = {"/webhook/stripe"}


def _csrf_token():
    if not app.secret_key:
        return ""
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


@app.context_processor
def inject_csrf_token():
    return {"csrf_token": _csrf_token}


@app.before_request
def csrf_protect():
    if request.method != "POST" or request.path in CSRF_EXEMPT_PATHS:
        return None
    if not app.secret_key:
        # Sessions (and therefore tokens) are unavailable; nothing session-
        # backed can be hijacked either, so let the request through.
        return None
    expected = session.get("_csrf_token") or ""
    supplied = (request.headers.get("X-CSRF-Token")
                or request.form.get("csrf_token") or "")
    if not expected or not secrets.compare_digest(supplied, expected):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Invalid or missing CSRF token. Refresh the page and try again."}), 403
        return "Invalid or missing CSRF token. Refresh the page and try again.", 403
    return None
scrape_status = {
    "running": False,
    "last": None,
    "count": 0,
    "started_at": None,
    "stopping": False,
    "updated_at": None,
    "current_step": None,
    "scraper_results": {},  # key -> {"count": N, "status": "ok"|"error"|"empty", "note": "..."}
}
scrape_control = {"thread": None, "stop_event": None}
listing_lock = threading.Lock()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
IS_VERCEL = bool(os.getenv("VERCEL") or os.getenv("VERCEL_ENV"))
IS_RAILWAY = bool(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PROJECT_ID")
                  or os.getenv("RAILWAY_GIT_COMMIT_SHA"))
# IS_HOSTED = running on a managed host (Vercel or Railway). Used to show a "this is the
# live site" note on the skip-trace page; the tracer itself is allowed to run on Railway.
IS_HOSTED = IS_VERCEL or IS_RAILWAY
STOREFRONT_ONLY = IS_VERCEL
# SQLite location. Explicit SQLITE_DB always wins. Otherwise, if a Railway volume is
# mounted (RAILWAY_VOLUME_MOUNT_PATH is set), put the DB on the volume so runtime writes
# (e.g. skip traces run on the hosted site) survive redeploys instead of being wiped and
# re-seeded from git listings.json. Falls back to a local file off the volume.
_RAILWAY_VOLUME = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
if os.getenv("SQLITE_DB"):
    SQLITE_DB = os.getenv("SQLITE_DB")
elif _RAILWAY_VOLUME:
    SQLITE_DB = os.path.join(_RAILWAY_VOLUME, "foreclosure.sqlite3")
else:
    SQLITE_DB = os.path.join(BASE_DIR, "foreclosure_local.sqlite3")
STOREFRONT_CSV = os.getenv("STOREFRONT_CSV", os.path.join(DATA_DIR, "storefront_listings.csv"))
STOREFRONT_FIELDS = [
    "id", "status", "county", "state", "city", "zip", "address", "street",
    "owner", "parcel_id", "case_number", "record_type", "price", "bid",
    "amount_owed", "date", "sale_date", "scraped_date", "link", "source",
]

def _sqlite_enabled():
    return not IS_VERCEL

def _sqlite_conn():
    os.makedirs(os.path.dirname(SQLITE_DB) or ".", exist_ok=True)
    conn = sqlite3.connect(SQLITE_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_json (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    return conn

def _sqlite_get(key, default):
    try:
        with _sqlite_conn() as conn:
            row = conn.execute("SELECT value FROM app_json WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default
    except Exception:
        return default

def _sqlite_set(key, data):
    with _sqlite_conn() as conn:
        conn.execute(
            """
            INSERT INTO app_json (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, json.dumps(data, ensure_ascii=False), datetime.now().isoformat(timespec="seconds")),
        )

def current_listings():
    return sort_storefront_listings(load_json(DATA_FILE, []))

def _runtime_data_file(filename):
    """Use /tmp on Vercel (writable), project files locally."""
    if not IS_VERCEL:
        return os.path.join(BASE_DIR, filename)
    runtime_dir = "/tmp/foreclosure-app"
    os.makedirs(runtime_dir, exist_ok=True)
    runtime_file = os.path.join(runtime_dir, filename)
    bundled_file = os.path.join(BASE_DIR, filename)
    if os.path.exists(bundled_file) and (
        not os.path.exists(runtime_file)
        or os.path.getsize(runtime_file) != os.path.getsize(bundled_file)
        or os.path.getmtime(runtime_file) < os.path.getmtime(bundled_file)
    ):
        shutil.copyfile(bundled_file, runtime_file)
    return runtime_file

DATA_FILE = _runtime_data_file('listings.json')
SETTINGS_FILE = _runtime_data_file('settings.json')
DEFAULT_SOURCES = {
    "include_tax_records": True,
    "include_hud": True,
    "include_homepath": True,
}

def _listing_sort_key(item):
    date_value = str(item.get("sale_date") or item.get("date") or item.get("scraped_date") or "")
    return (
        str(item.get("county") or "").lower(),
        str(item.get("status") or "").lower(),
        str(item.get("city") or "").lower(),
        date_value,
        str(item.get("owner") or "").lower(),
        str(item.get("parcel_id") or "").lower(),
        str(item.get("address") or "").lower(),
    )

def sort_storefront_listings(listings):
    return sorted((item for item in listings if isinstance(item, dict)), key=_listing_sort_key)

def _money_sort_value(value):
    cleaned = re.sub(r"[^0-9.-]+", "", str(value or ""))
    try:
        return float(cleaned) if cleaned else 0.0
    except ValueError:
        return 0.0

def _date_sort_value(value):
    text = str(value or "").strip()
    if not text:
        return (1, datetime.min, "")

    numeric_range = re.search(r"(\d{1,2})/(\d{1,2})\s*-\s*\d{1,2}/\d{1,2}/(\d{4})", text)
    if numeric_range:
        text = f"{numeric_range.group(1)}/{numeric_range.group(2)}/{numeric_range.group(3)}"

    month_range = re.search(r"([A-Za-z]+)\s+(\d{1,2})\s*-\s*[A-Za-z]+\s+\d{1,2},\s+(\d{4})", text)
    if month_range:
        text = f"{month_range.group(1)} {month_range.group(2)}, {month_range.group(3)}"

    ordinal = re.search(r"(\d{1,2})(?:st|nd|rd|th)\s+day\s+of\s+([A-Za-z]+),\s+(\d{4})", text, re.I)
    if ordinal:
        text = f"{ordinal.group(2)} {ordinal.group(1)}, {ordinal.group(3)}"

    embedded = re.search(r"[A-Za-z]+\s+\d{1,2},\s+\d{4}", text)
    if embedded:
        text = embedded.group(0)

    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return (0, datetime.strptime(text, fmt), "")
        except ValueError:
            pass
    return (1, datetime.min, text.lower())

_MONTH_MAP = {}
for _i, _name in enumerate(["january", "february", "march", "april", "may", "june", "july",
                            "august", "september", "october", "november", "december"], 1):
    _MONTH_MAP[_name] = _i
    _MONTH_MAP[_name[:3]] = _i
_MONTH_MAP["sept"] = 9


def _parse_sale_date(text, fallback_year=None):
    """Best-effort normalize a messy sale/record date to a sortable ISO string.
    Handles '2026-07-21', '7/21/2026', 'July 21, 2026', 'June, 2026',
    '28th day of June, 2026', 'July 21' (+fallback year). Returns '' if unparseable."""
    s = str(text or "").strip()
    if not s:
        return ""
    m = re.search(r"(\d{4})-(\d{2})(?:-(\d{2}))?", s)            # already ISO
    if m:
        return f"{m.group(1)}-{m.group(2)}" + (f"-{m.group(3)}" if m.group(3) else "")
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", s)        # m/d/y
    if m:
        mo, da, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        yr += 2000 if yr < 100 else 0
        return f"{yr:04d}-{mo:02d}-{da:02d}"
    monnum = next((_MONTH_MAP[t.lower()] for t in re.findall(r"[A-Za-z]+", s)
                   if t.lower() in _MONTH_MAP), None)                # month name
    if monnum:
        ym = re.search(r"\b(20\d{2})\b", s)
        yr = int(ym.group(1)) if ym else (int(fallback_year) if fallback_year else None)
        dm = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\b", s)
        da = int(dm.group(1)) if dm and 1 <= int(dm.group(1)) <= 31 else None
        if yr and da:
            return f"{yr:04d}-{monnum:02d}-{da:02d}"
        if yr:
            return f"{yr:04d}-{monnum:02d}"
    return ""


def _csv_sort_value(row, column):
    if column in {"amount_owed"}:
        return _money_sort_value(row.get("amount_owed") or row.get("bid") or "")
    if column in {"sale_date", "scraped_date"}:
        return _date_sort_value(
            row.get(column) or row.get("date") or row.get("scraped_date") or row.get("sale_date") or ""
        )
    value = row.get(column, "")
    if column == "owner_name":
        value = value or row.get("owner", "")
    elif column == "property_address":
        value = value or row.get("address", "")
    elif column == "source_url":
        value = value or row.get("link", "")
    return str(value or "").lower()

def _is_publishable_listing(item):
    if not isinstance(item, dict):
        return False
    # Keep real scraped/source rows even when a county source does not expose
    # owner, amount, or sale-date details. Only exclude known demos and blanks.
    if str(item.get("id") or "").strip() in {"1", "2", "3"}:
        return False
    if not str(item.get("county") or "").strip():
        return False
    if not (str(item.get("source") or "").strip() or str(item.get("link") or "").strip()):
        return False
    county = str(item.get("county") or "").strip().lower()
    status = str(item.get("status") or item.get("record_type") or "").strip().lower()
    source = str(item.get("source") or "").strip().lower()
    link = str(item.get("link") or "").strip().lower()
    parcel = str(item.get("parcel_id") or "").strip()
    owner = str(item.get("owner") or "").strip()
    address = str(item.get("address") or "").strip()
    if len(address) < 8:
        return False
    if county == "duval":
        is_duval_tax = "tax" in status and ("duval" in source or "re_tax" in link)
        if is_duval_tax:
            return bool(owner and parcel)
        if re.search(r"\b16-\d{4}-(?:ca|cc)-\d{6}\b", address, re.I):
            return False
        if re.match(r"^(?:apn\s+)?[0-9a-z-]{6,}(?:,\s*jacksonville,\s*fl)?$", address, re.I):
            return False
        if ("hud" in source or "homepath" in source) and not re.search(
            r"\b(?:jacksonville|jacksonville beach|atlantic beach|neptune beach|baldwin)\b",
            " ".join(str(item.get(key) or "") for key in ("address", "city")),
            re.I,
        ):
            return False
    return True

def publishable_storefront_listings(listings):
    return sort_storefront_listings([item for item in listings if _is_publishable_listing(item)])

def _storefront_owner(item):
    owner = str(item.get("owner") or "").strip()
    if owner:
        return owner
    source = str(item.get("source") or "").lower()
    status = str(item.get("status") or item.get("record_type") or "").lower()
    if "homepath" in source:
        return "Fannie Mae (HomePath)"
    if source == "hud" or "hud" in source:
        return "HUD"
    if "tax" in status:
        return "Owner not listed in tax record"
    if "pre-foreclosure" in status or "foreclosure" in status:
        return "Owner not listed in notice"
    if "auction" in status:
        return "Seller not listed"
    return ""


def _mask_public_owner(owner):
    owner = re.sub(r"\s+", " ", str(owner or "")).strip(" ,")
    if not owner:
        return ""
    generic = owner.lower()
    if generic.startswith("owner not listed"):
        return "Owner unavailable"
    if generic.startswith("seller not listed"):
        return "Seller unavailable"
    if generic in {"hud"}:
        return "HUD"

    cleaned = re.sub(r"\b(etux|et ux|aka|trustee|tc|j/t)\b", "", owner, flags=re.I)
    cleaned = re.sub(r"\b\d{1,3}-\d{1,3}\b", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,")
    tokens = re.findall(r"[A-Za-z]+", cleaned)
    stop_words = {
        "al", "and", "company", "co", "corp", "corporation", "estate", "heirs",
        "holdings", "inc", "investments", "llc", "partners", "properties", "trust",
    }
    initials = [token[0].upper() for token in tokens if token.lower() not in stop_words]
    if not initials:
        return "Owner masked"
    return f"{'.'.join(initials[:3])}. ****"

def _source_listing_id(item):
    link = str(item.get("link") or "").strip().rstrip("/")
    if not link:
        return ""
    tail = link.rsplit("/", 1)[-1]
    return tail if tail and tail not in {"hudhomestore.gov", "homepath.fanniemae.com"} else ""

def _storefront_display_row(item):
    public_item = _backfill_fields(dict(item))
    status = str(public_item.get("status") or public_item.get("record_type") or "").lower()
    source = str(public_item.get("source") or "").lower()
    county = str(public_item.get("county") or "").lower()
    link = str(public_item.get("link") or "").lower()
    source_id = _source_listing_id(public_item)
    parcel_display_address = False

    public_item["owner"] = _mask_public_owner(_storefront_owner(public_item))
    public_item["scraped_date"] = public_item.get("scraped_date") or public_item.get("date")

    if not public_item.get("amount_owed"):
        try:
            bid_value = int(float(str(public_item.get("bid") or "0").replace(",", "")))
        except ValueError:
            bid_value = 0
        if bid_value > 0:
            public_item["amount_owed"] = f"${bid_value:,}"

    if "homepath" in source or "hud" in source:
        public_item["parcel_id"] = public_item.get("parcel_id") or source_id or "REO listing"
        public_item["case_number"] = public_item.get("case_number") or ""
        public_item["sale_date"] = public_item.get("sale_date") or public_item.get("date") or public_item.get("scraped_date")
    elif "tax" in status:
        public_item["sale_date"] = public_item.get("sale_date") or public_item.get("date") or public_item.get("scraped_date")
        if county == "duval" and ("duval" in source or "re_tax" in link) and public_item.get("parcel_id"):
            public_item["address"] = f"Parcel {public_item['parcel_id']}, Jacksonville, FL"
            public_item["date"] = DUVAL_TAX_CERTIFICATE_SALE_DATE
            public_item["sale_date"] = DUVAL_TAX_CERTIFICATE_SALE_DATE
            parcel_display_address = True
    elif "foreclosure" in status:
        public_item["sale_date"] = public_item.get("sale_date") or public_item.get("date") or public_item.get("scraped_date")

    if public_item.get("address") and not parcel_display_address:
        public_item["address"] = obfuscate_address(str(public_item["address"]))
    public_item["street"] = ""

    # Privacy: show only coarse contact teasers. Full contact data never reaches
    # the page source.
    digits = re.sub(r"\D", "", str(public_item.get("primary_phone") or ""))
    if len(digits) >= 10:
        area = digits[-10:][:3]
        public_item["phone_prefix"] = area
        public_item["phone_display"] = f"({area})"
    else:
        public_item["phone_prefix"] = ""
        public_item["phone_display"] = ""
    email = str(public_item.get("email_1") or "").strip()
    if email and "@" in email:
        domain = email.split("@", 1)[1]
        public_item["email_display"] = f"@{domain}"
    else:
        public_item["email_display"] = ""
    for private_field in ("primary_phone", "phone_2", "email_1", "email_2",
                          "mailing_address", "skiptrace_notes", "skiptrace_source",
                          "skiptraced_at"):
        public_item.pop(private_field, None)
    return public_item

def _read_storefront_csv(default):
    if not os.path.exists(STOREFRONT_CSV):
        return default
    rows = []
    with open(STOREFRONT_CSV, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            item = {field: row.get(field, "") for field in STOREFRONT_FIELDS}
            for int_field in ("price", "bid"):
                try:
                    item[int_field] = int(float(str(item.get(int_field) or "0").replace(",", "")))
                except ValueError:
                    item[int_field] = 0
            rows.append(item)
    return sort_storefront_listings(rows)

def export_storefront_csv(listings):
    rows = publishable_storefront_listings(listings)
    os.makedirs(os.path.dirname(STOREFRONT_CSV), exist_ok=True)
    with open(STOREFRONT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=STOREFRONT_FIELDS)
        writer.writeheader()
        for item in rows:
            public_item = _storefront_display_row(item)
            writer.writerow({field: public_item.get(field, "") for field in STOREFRONT_FIELDS})
    return STOREFRONT_CSV

def load_json(file, default):
    if _sqlite_enabled() and os.path.abspath(file) == os.path.abspath(DATA_FILE):
        seed = default
        if os.path.exists(file):
            with open(file, 'r', encoding='utf-8-sig') as f:
                try:
                    seed = json.load(f)
                except json.JSONDecodeError:
                    seed = default
        return _sqlite_get("listings", seed)
    if _sqlite_enabled() and os.path.abspath(file) == os.path.abspath(SETTINGS_FILE):
        seed = default
        if os.path.exists(file):
            with open(file, 'r', encoding='utf-8-sig') as f:
                try:
                    seed = json.load(f)
                except json.JSONDecodeError:
                    seed = default
        return _sqlite_get("settings", seed)
    if os.path.exists(file):
        with open(file, 'r', encoding='utf-8-sig') as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return default
    return default

def save_json(file, data):
    if _sqlite_enabled() and os.path.abspath(file) == os.path.abspath(DATA_FILE):
        rows = sort_storefront_listings(data)
        _sqlite_set("listings", rows)
        export_storefront_csv(rows)
        return
    if _sqlite_enabled() and os.path.abspath(file) == os.path.abspath(SETTINGS_FILE):
        _sqlite_set("settings", data)
        return
    with open(file, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)


# --- Subscription store (county subscriptions + claims) ---------------------------
# Persisted in the app_json/SQLite store under "subscriptions" (so it survives on the
# Railway volume), or a flat file when SQLite is off. Each record:
#   {id, user_id, email, county, stripe_subscription_id, stripe_customer_id,
#    status, price_id, created_at, current_period_end, period_start, traces_used}
SUBS_FILE = _runtime_data_file('subscriptions.json')


def _load_subs():
    if _sqlite_enabled():
        return _sqlite_get("subscriptions", [])
    return load_json(SUBS_FILE, [])


def _save_subs(subs):
    if _sqlite_enabled():
        _sqlite_set("subscriptions", subs)
    else:
        save_json(SUBS_FILE, subs)


def _active_subs(subs=None):
    subs = _load_subs() if subs is None else subs
    return [s for s in subs if s.get("status") == "active"]


def _county_untapped_counts():
    """Return {county_name_lower: skippable_untapped_count}.
    Only counts leads that have an owner name (skippable) and no phone/email yet."""
    try:
        rows = current_listings()
    except Exception:
        rows = []
    totals, traced = {}, {}
    for r in rows:
        c = str(r.get("county") or "").strip().lower()
        if not c or not str(r.get("owner") or "").strip():
            continue  # no owner name = not skippable
        totals[c] = totals.get(c, 0) + 1
        if r.get("primary_phone") or r.get("email_1"):
            traced[c] = traced.get(c, 0) + 1
    return {c: totals[c] - traced.get(c, 0) for c in totals}


def _county_capacity(untapped: int) -> int:
    """Max subscriber slots = untapped // 40 (each slot needs ~40 leads to guarantee 25 traces)."""
    return max(1, untapped // 40)


def _county_sub_counts():
    """Return {county_lower: active_subscriber_count}."""
    counts: dict = {}
    for s in _active_subs():
        c = str(s.get("county") or "").lower()
        if c:
            counts[c] = counts.get(c, 0) + 1
    return counts


def _claimed_counties(exclude_user=None):
    """county(lowercase) -> user_id for counties that are AT full capacity.
    A county only appears here when active_subs >= _county_capacity(untapped).
    exclude_user: omit the county if the requesting user is already one of its subscribers
    (so they don't see their own county greyed out)."""
    untapped = _county_untapped_counts()
    sub_counts = _county_sub_counts()
    # county -> list of subscriber ids
    subs_by_county: dict = {}
    for s in _active_subs():
        c = str(s.get("county") or "").lower()
        if c:
            subs_by_county.setdefault(c, []).append(str(s.get("user_id")))
    out = {}
    for c, count in sub_counts.items():
        cap = _county_capacity(untapped.get(c, 0))
        if count >= cap:
            uid_list = subs_by_county.get(c, [])
            if exclude_user is None or str(exclude_user) not in uid_list:
                out[c] = uid_list[0] if uid_list else None
    return out


def _user_subs(user_id):
    return [s for s in _active_subs() if str(s.get("user_id")) == str(user_id)]


def _sub_price_for(tier: str) -> str:
    """Return the Stripe price ID for the given tier key (starter/professional/power)."""
    return TIER_PRICES.get(str(tier).lower(), STRIPE_PRICE_TIER_PRO)


def _tier_included(tier: str) -> int:
    """Included skip-traced leads per month for the given tier."""
    return TIER_INCLUDED.get(str(tier).lower(), TIER_INCLUDED["professional"])


def _upsert_sub(record):
    """Insert or update by stripe_subscription_id (fallback to id)."""
    subs = _load_subs()
    key = record.get("stripe_subscription_id") or record.get("id")
    for i, s in enumerate(subs):
        if (s.get("stripe_subscription_id") or s.get("id")) == key:
            subs[i] = {**s, **record}
            _save_subs(subs)
            return subs[i]
    subs.append(record)
    _save_subs(subs)
    return record

def merge_listings(existing, incoming):
    merged = list(existing)
    seen = {str(item.get("id")) for item in merged if item.get("id") is not None}
    by_parcel_date = {}
    for idx, item in enumerate(merged):
        parcel_key = _parcel_date_key(item)
        if parcel_key:
            by_parcel_date.setdefault(parcel_key, idx)
    added = 0
    for item in incoming:
        item_id = str(item.get("id"))
        existing_idx = None
        if item_id and item_id in seen:
            existing_idx = next((i for i, old in enumerate(merged) if str(old.get("id")) == item_id), None)
        parcel_key = _parcel_date_key(item)
        if existing_idx is None and parcel_key in by_parcel_date:
            existing_idx = by_parcel_date[parcel_key]

        if existing_idx is not None:
            _upgrade_listing(merged[existing_idx], item)
        elif item_id and item_id not in seen:
            merged.append(item)
            seen.add(item_id)
            if parcel_key:
                by_parcel_date.setdefault(parcel_key, len(merged) - 1)
            added += 1
    return merged, added

def merge_listings_only(existing, incoming):
    merged, _ = merge_listings(existing, incoming)
    return merged

def _parcel_date_key(item):
    parcel = re.sub(r"\D", "", str(item.get("parcel_id") or ""))
    county = str(item.get("county") or "").lower().strip()
    date_value = str(item.get("sale_date") or item.get("date") or "").strip()
    if not parcel or not county:
        return ""
    return f"{county}|{parcel}|{date_value}"

def _upgrade_listing(existing, incoming):
    """Keep existing lead identity, but fill in better data from newer scrapes."""
    for key, value in incoming.items():
        if value in (None, ""):
            continue
        old = existing.get(key)
        if old in (None, ""):
            existing[key] = value
            continue
        if key in {"address", "street", "city", "zip"}:
            old_text = str(old)
            new_text = str(value)
            if old_text.startswith("APN ") and not new_text.startswith("APN "):
                existing[key] = value
            elif key == "address" and len(new_text) > len(old_text) and "APN " not in new_text:
                existing[key] = value
    return existing

def source_result(source_key, raw_count, kept_count, new_count, status, note=""):
    meta = source_metadata().get(source_key, {})
    return {
        "count": new_count,
        "new": new_count,
        "kept": kept_count,
        "raw": raw_count,
        "duplicates": max(0, kept_count - new_count),
        "status": status,
        "note": note,
        "label": meta.get("label", source_key),
        "region": meta.get("region", ""),
        "source_type": meta.get("source_type", ""),
        "source_url": meta.get("source_url", ""),
    }

def prioritize_counties(counties, sources):
    if not sources.get("include_tax_records"):
        return counties
    tax_first = []
    return sorted(counties, key=lambda county: 0 if county in tax_first else 1)

def normalize_settings(settings):
    settings.setdefault("counties", ["chatham-ga", "glynn-ga", "camden-ga", "duval-fl", "stjohns-fl", "nassau-fl"])
    settings.setdefault("lookback_days", 30)
    settings["sources"] = {**DEFAULT_SOURCES, **settings.get("sources", {})}
    return settings

def obfuscate_address(address):
    text = re.sub(r"\s+", " ", str(address or "")).strip()
    if not text:
        return "Address hidden"
    if re.match(r"^\s*(?:parcel|apn)\b", text, re.I):
        return text

    parts = [part.strip() for part in text.split(",")]
    parts = [part for part in parts if not re.match(r"^(?:apt|unit|suite|ste|#)\s*[A-Za-z0-9-]+$", part, re.I)]
    street = parts[0] if parts else text
    city = parts[1] if len(parts) > 1 else ""
    state_zip = ", ".join(parts[2:]).strip() if len(parts) > 2 else ""

    street_without_unit = re.sub(
        r"\b(?:apt|unit|suite|ste|#)\s*[A-Za-z0-9-]+\b",
        "",
        street,
        flags=re.I,
    )
    street_without_unit = re.sub(r"^\s*(?:x+|\d+[A-Za-z]?)\s+", "", street_without_unit, flags=re.I)
    street_tokens = re.findall(r"[A-Za-z][A-Za-z0-9.'-]*", street_without_unit)
    road_types = {
        "aly", "alley", "ave", "avenue", "blvd", "boulevard", "cir", "circle",
        "ct", "court", "cv", "cove", "dr", "drive", "hwy", "highway", "ln", "lane",
        "loop", "pkwy", "parkway", "pl", "place", "rd", "road", "sq", "square",
        "st", "street", "ter", "terrace", "tr", "trl", "trail", "way",
    }
    directions = {"n", "s", "e", "w", "ne", "nw", "se", "sw", "north", "south", "east", "west"}
    type_index = next(
        (idx for idx in range(len(street_tokens) - 1, -1, -1) if street_tokens[idx].lower().strip(".") in road_types),
        None,
    )
    type_token = street_tokens[type_index] if type_index is not None else ""
    name_candidates = street_tokens[:type_index] if type_index is not None else street_tokens
    name_token = next((token for token in name_candidates if token.lower().strip(".") not in directions), "")
    masked_street = f"{name_token[0].upper()}**** {type_token}" if name_token and type_token else "Street hidden"
    if city and state_zip:
        return f"{masked_street}, {city}, {state_zip}"
    if city:
        return f"{masked_street}, {city}"
    return masked_street

_ADDR_PARSE_RE = re.compile(r'^(.*?),\s*([A-Za-z\.\- ]+),\s*([A-Z]{2})(?:\s+(\d{5}))?\s*$')
_GARBLED_MARKERS = {
    "\xc3\xa2\xe2\x82\xac\xe2\x80\x9d",
    "\xc3\xa2\xe2\x82\xac\xe2\x80\x9c",
    "\xc3\xa2\xe2\x82\xac",
    "\xc3\x83\xc2\xa2\xc3\xa2\xe2\x80\x9a\xc2\xac\xc3\xa2\xe2\x82\xac\xc2\x9d",
    "\xc3\x83\xc2\xa2\xc3\xa2\xe2\x80\x9a\xc2\xac\xc3\xa2\xe2\x82\xac\xc5\x93",
    "\xc3\x82",
}

def _clean_text(v):
    if v is None:
        return ""
    if not isinstance(v, str):
        return v
    s = v.strip()
    if s in _GARBLED_MARKERS:
        return ""
    return s

def _backfill_fields(item):
    """For old listings missing city/state/zip, parse from the address string."""
    addr = item.get('address') or ''
    if not item.get('city') or not item.get('state'):
        m = _ADDR_PARSE_RE.match(addr.strip())
        if m:
            street, city, state, zip_code = m.groups()
            item.setdefault('street', street.strip())
            if not item.get('city'):  item['city']  = city.strip()
            if not item.get('state'): item['state'] = state.strip()
            if not item.get('zip'):   item['zip']   = (zip_code or '').strip()
    # Ensure all keys exist so the templates do not crash on older listings.
    for k in ['city','state','zip','owner','parcel_id','case_number',
              'amount_owed','sale_date','scraped_date','county','link',
              'bid','record_type','street']:
        if item.get(k) is None:
            item[k] = '' if k != 'bid' else 0
        elif k != 'bid':
            item[k] = _clean_text(item[k])
    return item

def _listing_price_cents(item):
    try:
        return max(0, int(round(float(item.get("price", 5)) * 100)))
    except (TypeError, ValueError):
        return 500

@app.route('/')
def index():
    listings = publishable_storefront_listings(current_listings())
    user = current_user()
    sub_counties = set()
    if user:
        sub_counties = {str(s.get("county") or "").strip().lower()
                        for s in _user_subs(user["id"])}
    display_listings = []
    for item in listings:
        masked_item = _storefront_display_row(item)
        masked_item['display_address'] = masked_item.get("address") or obfuscate_address(item.get('address', ''))
        masked_item['subscriber_county'] = (
            str(item.get("county") or "").strip().lower() in sub_counties
        )
        display_listings.append(masked_item)
    return render_template('index.html', listings=display_listings,
                           storefront_only=STOREFRONT_ONLY,
                           sub_counties=list(sub_counties))

@app.route('/admin')
def admin():
    if STOREFRONT_ONLY or not admin_allowed():
        return "Not Found", 404
    settings = load_json(SETTINGS_FILE, {"counties": [], "lookback_days": 30})
    source_cards = sorted(
        source_metadata().values(),
        key=lambda s: (s.get("region", ""), s.get("label", "")),
    )
    return render_template('admin.html', settings=settings, source_cards=source_cards)

@app.route('/admin/<path:bad_path>')
def admin_bad_link_redirect(bad_path):
    if STOREFRONT_ONLY or not admin_allowed():
        return "Not Found", 404
    """Recover from older Data Explorer links that missed the /data? route."""
    if bad_path.startswith("-file="):
        query = bad_path.replace("-file=", "file=", 1)
        return redirect(f"{url_for('admin_data')}?{query}", code=302)
    return "Not Found", 404

# ---- Auth ------------------------------------------------------------------

def current_user():
    """Return the logged-in user row, or None. Cached per request."""
    if not app.secret_key:
        return None
    try:
        uid = session.get("user_id")
    except RuntimeError:
        return None
    if not uid:
        return None
    if not db.is_configured():
        return None
    try:
        db.init_db()
        return db.get_user_by_id(uid)
    except Exception:
        return None


@app.context_processor
def inject_user():
    user = current_user()
    is_admin = admin_allowed()
    accounts_ready = _accounts_ready()
    database_configured = db.is_configured()
    secret_key_set = bool(app.secret_key)
    stripe_configured = bool(stripe.api_key)
    appwrite_endpoint_set = bool(os.getenv("APPWRITE_ENDPOINT", ""))
    appwrite_project_set = bool(os.getenv("APPWRITE_PROJECT_ID", ""))
    appwrite_key_set = bool(os.getenv("APPWRITE_API_KEY", ""))
    appwrite_configured = bool(appwrite_endpoint_set and appwrite_project_set and appwrite_key_set)
    return {
        "current_user_email": user["email"] if user else None,
        "accounts_ready": accounts_ready,
        "checkout_ready": bool(accounts_ready and stripe_configured),
        "subscriptions_ready": _subscriptions_ready(),
        "appwrite_pending": not appwrite_configured,
        "database_configured": database_configured,
        "secret_key_set": secret_key_set,
        "stripe_configured": stripe_configured,
        "appwrite_endpoint_set": appwrite_endpoint_set,
        "appwrite_project_set": appwrite_project_set,
        "appwrite_key_set": appwrite_key_set,
        "appwrite_configured": appwrite_configured,
        "admin_allowed": is_admin,
    }


@app.route('/healthz')
def healthz():
    return jsonify({"ok": True, "storefront_only": STOREFRONT_ONLY})


@app.route('/version')
def version():
    volume = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "")
    # The DB is "persistent" only if it physically lives under the mounted volume.
    persistent = bool(volume and os.path.abspath(SQLITE_DB).startswith(os.path.abspath(volume)))
    return jsonify({
        "build": APP_BUILD,
        "storefront_only": STOREFRONT_ONLY,
        "admin_email_configured": bool((os.getenv("ADMIN_EMAIL", "") or "").strip()),
        "owner_admin_configured": bool(OWNER_ADMIN_EMAILS),
        "railway_commit": os.getenv("RAILWAY_GIT_COMMIT_SHA", ""),
        "railway_branch": os.getenv("RAILWAY_GIT_BRANCH", ""),
        "volume_mount": volume,
        "sqlite_db": SQLITE_DB,
        "db_persistent": persistent,
    })


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def _admin_token():
    return os.getenv("ADMIN_TOKEN", "") or os.getenv("SKIPTRACE_ADMIN_TOKEN", "")


def admin_allowed():
    # Session writes require a secret key; without one Flask's NullSession
    # raises on assignment, so token access still works but isn't remembered.
    can_remember = bool(app.secret_key)
    token = _admin_token()
    if token:
        if can_remember and session.get("admin_allowed"):
            return True
        supplied = request.args.get("token") or request.form.get("token")
        if supplied and supplied == token:
            if can_remember:
                session["admin_allowed"] = True
                session["skiptrace_admin"] = True
            return True
    user = current_user()
    if user:
        admin_email = (os.getenv("ADMIN_EMAIL", "") or "").strip().lower()
        allowed = set(OWNER_ADMIN_EMAILS)
        allowed.update(email.strip() for email in admin_email.split(",") if email.strip())
        if user["email"].lower() in allowed:
            if can_remember:
                session["admin_allowed"] = True
            return True
    return False


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if admin_allowed():
            return view(*args, **kwargs)
        if request.path.startswith("/api/"):
            return jsonify({"error": "not_found"}), 404
        return "Not Found", 404
    return wrapped


def _accounts_ready():
    """True when both the DB and the session secret are configured."""
    return db.is_configured() and bool(app.secret_key)


@app.route('/register', methods=['GET', 'POST'])
def register():
    if not _accounts_ready():
        return render_template('auth.html', mode='register',
                               error="Accounts are not configured yet. Set Appwrite env vars and SECRET_KEY.")
    if current_user():
        return redirect(url_for('account'))
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip().lower()
        password = request.form.get('password') or ''
        if not email or '@' not in email:
            return render_template('auth.html', mode='register', error="Enter a valid email.", email=email)
        if len(password) < 8:
            return render_template('auth.html', mode='register', error="Password must be at least 8 characters.", email=email)
        db.init_db()
        user = db.create_user(
            email,
            generate_password_hash(password),
        )
        if not user:
            return render_template('auth.html', mode='register', error="That email is already registered. Try logging in.", email=email)
        session['user_id'] = user['id']
        return redirect(url_for('account'))
    return render_template('auth.html', mode='register')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if not _accounts_ready():
        return render_template('auth.html', mode='login',
                               error="Accounts are not configured yet. Set Appwrite env vars and SECRET_KEY.")
    if current_user():
        return redirect(url_for('account'))
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip().lower()
        password = request.form.get('password') or ''
        db.init_db()
        user = db.get_user_by_email(email)
        if not user or not check_password_hash(user['password_hash'], password):
            return render_template('auth.html', mode='login', error="Incorrect email or password.", email=email)
        session['user_id'] = user['id']
        nxt = request.args.get('next') or url_for('account')
        if not nxt.startswith('/'):
            nxt = url_for('account')
        return redirect(nxt)
    return render_template('auth.html', mode='login')


@app.route('/logout', methods=['POST'])
def logout():
    if app.secret_key:
        session.pop('user_id', None)
    return redirect(url_for('index'))


@app.route('/account')
@login_required
def account():
    user = current_user()
    orders = []
    try:
        orders = db.get_paid_orders_for_user(user['id'])
    except Exception:
        orders = []
    folders = set()
    statuses = ["New", "Contacted", "Follow-up", "Offer Made", "Won", "Lost", "Do Not Contact"]
    for order in orders:
        order_created = _iso_from_datetime(order.get("created_at"))
        for lead in order.get("leads_json") or []:
            _normalize_buyer_tracking(lead, purchased_at=order_created)
            folder = str(lead.get("buyer_folder") or "").strip()
            if folder:
                folders.add(folder)
    active_subs = _user_subs(user["id"]) if _subscriptions_ready() else []
    for s in active_subs:
        pt = str(s.get("pending_tier") or "").lower()
        if pt in TIER_LABELS:
            s["pending_tier_label"] = TIER_LABELS[pt]
            ts = s.get("pending_tier_at")
            try:
                s["pending_tier_date"] = datetime.fromtimestamp(int(ts)).strftime("%b %-d, %Y")
            except (TypeError, ValueError):
                try:
                    s["pending_tier_date"] = datetime.fromtimestamp(int(ts)).strftime("%b %d, %Y")
                except (TypeError, ValueError):
                    s["pending_tier_date"] = ""
    return render_template('account.html', email=user['email'], orders=orders,
                           folders=sorted(folders), statuses=statuses,
                           active_subs=active_subs,
                           tier_labels=TIER_LABELS, tier_amounts=TIER_AMOUNTS,
                           tier_included=TIER_INCLUDED,
                           subscriptions_ready=_subscriptions_ready())


@app.route('/account/debug')
@login_required
def account_debug():
    user = current_user()
    admin_email = (os.getenv("ADMIN_EMAIL", "") or "").strip().lower()
    return jsonify({
        "email": user["email"] if user else "",
        "user_id": user["id"] if user else "",
        "admin_allowed": admin_allowed(),
        "admin_email_configured": bool(admin_email),
        "owner_admin_match": bool(user and user["email"].lower() in OWNER_ADMIN_EMAILS),
    })


@app.route('/account/leads/update', methods=['POST'])
@login_required
def account_lead_update():
    user = current_user()
    order_id = (request.form.get("order_id") or "").strip()
    lead_id = (request.form.get("lead_id") or "").strip()
    folder = re.sub(r"\s+", " ", request.form.get("buyer_folder") or "").strip()[:80]
    status = re.sub(r"\s+", " ", request.form.get("buyer_status") or "").strip()[:40]
    priority = re.sub(r"\s+", " ", request.form.get("buyer_priority") or "").strip()[:20]
    notes = (request.form.get("buyer_notes") or "").strip()[:1000]
    anchor = re.sub(r"[^A-Za-z0-9_\-]", "", request.form.get("anchor") or "")
    if not order_id or not lead_id:
        return redirect(url_for("account") + "?failed=1")
    if not folder:
        folder = "New Leads"
    if not status:
        status = "New"
    try:
        ok = db.update_order_lead_tracking(order_id, user["id"], lead_id, {
            "buyer_folder": folder,
            "buyer_status": status,
            "buyer_priority": priority or "Normal",
            "buyer_notes": notes,
            "buyer_updated_at": _utc_now_iso(),
        })
    except Exception:
        ok = False
    if not ok:
        return redirect(url_for("account") + "?failed=1")
    suffix = f"#{anchor}" if anchor else ""
    return redirect(url_for("account") + "?saved=1" + suffix)


def _default_buyer_folder(lead):
    county = str(lead.get("county") or "").strip().title()
    state = str(lead.get("state") or "").strip().upper()
    if county and state:
        return f"{county}, {state}"
    if county:
        return county
    return "New Leads"


def _utc_now_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _iso_from_datetime(value):
    if not value:
        return ""
    if hasattr(value, "isoformat"):
        try:
            return value.replace(microsecond=0).isoformat()
        except Exception:
            return value.isoformat()
    return str(value)


@app.template_filter("activity_time")
def activity_time(value):
    if not value:
        return "—"
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
        return parsed.strftime("%b %d, %Y %I:%M %p")
    except Exception:
        return str(value)


def _normalize_buyer_tracking(lead, purchased_at=""):
    purchased = lead.get("buyer_purchased_at") or lead.get("purchased_at") or purchased_at or _utc_now_iso()
    updated = lead.get("buyer_updated_at") or purchased
    lead.setdefault("buyer_folder", _default_buyer_folder(lead))
    lead.setdefault("buyer_status", "New")
    lead.setdefault("buyer_priority", "Normal")
    lead.setdefault("buyer_notes", "")
    lead["buyer_purchased_at"] = purchased
    lead["buyer_updated_at"] = updated
    if not isinstance(lead.get("buyer_note_history"), list):
        first_note = str(lead.get("buyer_notes") or "").strip()
        lead["buyer_note_history"] = ([{"at": updated, "note": first_note}] if first_note else [])
    if not isinstance(lead.get("buyer_activity"), list):
        lead["buyer_activity"] = [{
            "at": purchased,
            "label": "Purchased",
            "detail": f"Lead added to {_default_buyer_folder(lead)}",
        }]
    return lead


def _prepare_purchased_leads(leads):
    prepared = []
    now = _utc_now_iso()
    for lead in leads:
        item = dict(lead)
        item["buyer_purchased_at"] = now
        item["buyer_updated_at"] = now
        _normalize_buyer_tracking(item, purchased_at=now)
        prepared.append(item)
    return prepared


def _skiptrace_status(lead):
    if lead.get("primary_phone") or lead.get("email_1"):
        return lead.get("skiptrace_status") or "completed"
    return lead.get("skiptrace_status") or "pending"


def _skiptrace_search_owner(owner):
    text = re.sub(r"\([^)]*\)", " ", str(owner or ""))
    text = re.sub(r"\b(ETUX|ET UX|ET AL|AKA)\b", " ", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" ,")
    if "," in text:
        last, rest = text.split(",", 1)
        text = f"{rest.strip()} {last.strip()}"
    text = re.sub(r"[^A-Za-z0-9\s.'-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _skiptrace_search_address(address):
    text = str(address or "")
    text = re.sub(r"\{[^}]*\}", " ", text)
    text = re.sub(r"C:\\.*", " ", text)
    text = re.sub(r"\s+\.\d+\b", " ", text)
    text = re.sub(r"\s+\d+/\d+/\d{4}.*", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" ,")
    return text


def _skiptrace_city_state(lead):
    city = str(lead.get("city") or "").strip()
    state = str(lead.get("state") or "").strip()
    address = str(lead.get("address") or "").strip()
    parts = [part.strip() for part in address.split(",") if part.strip()]
    if len(parts) >= 2:
        city = city or parts[-2]
        state_match = re.match(r"([A-Za-z]{2})\b", parts[-1])
        state = state or (state_match.group(1).upper() if state_match else parts[-1])
    return city, state


def _skiptrace_lead_score(item):
    address = str(item.get("address") or "").strip().lower()
    clean_address = _skiptrace_search_address(address).lower()
    owner = str(item.get("owner") or "").strip().lower()
    source = str(item.get("source") or "").strip().lower()
    score = 0
    if re.search(r"^[1-9]\d*\s+[a-z]", clean_address):
        score += 8
    if re.search(r"\d+\s+[a-z]", clean_address):
        score += 4
    if not clean_address.startswith("apn "):
        score += 2
    if source and " co." in source:
        score += 1
    if re.search(r"\b(llc|inc|trust|estate|heirs|association|gp|group|builders|housing solutions)\b", owner):
        score -= 8
    if re.search(r"&|/|\band\b", owner):
        score -= 5
    if re.search(r"\blot\b|\boff\b|apn|c:\\|documents|\\users\\|\.doc|housing solutions|development", address):
        score -= 8
    if clean_address.startswith("0 "):
        score -= 4
    return score


def _cyberbackgroundchecks_links(lead):
    owner = str(lead.get("owner") or "").strip()
    address = str(lead.get("address") or lead.get("street") or "").strip()
    city, state = _skiptrace_city_state(lead)
    base = "https://www.google.com/search?q="

    searchable_owner = _skiptrace_search_owner(owner)
    searchable_address = _skiptrace_search_address(address)

    name_parts = [f'"{searchable_owner}"' if searchable_owner else "", city, state]
    name_query = " ".join(part for part in name_parts if part).strip()

    address_parts = [f'"{searchable_address}"' if searchable_address else "", city, state]
    address_query = " ".join(part for part in address_parts if part).strip()

    combined_parts = [
        f'"{searchable_owner}"' if searchable_owner else "",
        f'"{searchable_address}"' if searchable_address else "",
        city,
        state,
    ]
    web_query = " ".join(part for part in combined_parts if part).strip()

    cbc_query = " ".join(part for part in ["site:cyberbackgroundchecks.com", searchable_owner, city, state] if part)
    cbc_search_text = " ".join(part for part in [searchable_owner, city, state] if part).strip()
    city_state = ", ".join(part for part in [city, state] if part)
    tps_base = "https://www.truepeoplesearch.com"
    tps_meta_query = " ".join(
        part for part in ["site:truepeoplesearch.com", searchable_owner, searchable_address, city, state]
        if part
    )

    from urllib.parse import quote_plus
    return {
        "name": base + quote_plus(name_query) if name_query else "",
        "address": base + quote_plus(address_query) if address_query else "",
        "web": base + quote_plus(web_query) if web_query.strip() else "",
        "cbc": base + quote_plus(cbc_query) if cbc_query.strip() else "",
        "cbc_home": "https://www.cyberbackgroundchecks.com/",
        "cbc_search_text": cbc_search_text,
        "address_search_text": searchable_address,
        "tps_name": (
            f"{tps_base}/results?name={quote_plus(searchable_owner)}&citystatezip={quote_plus(city_state)}"
            if searchable_owner else ""
        ),
        "tps_address": (
            f"{tps_base}/resultaddress?streetaddress={quote_plus(searchable_address)}&citystatezip={quote_plus(city_state)}"
            if searchable_address else ""
        ),
        "tps_meta": base + quote_plus(tps_meta_query) if tps_meta_query.strip() else "",
    }


def _skiptrace_admin_allowed():
    return admin_allowed()


def _skiptrace_dev_enabled():
    return _sqlite_enabled() and not db.is_configured()


def _skiptrace_dev_orders(force_real=False):
    orders = None if force_real else _sqlite_get("skiptrace_dev_orders", None)
    if not orders:
        sample_leads = []
        listings = publishable_storefront_listings(current_listings())
        preferred = [
            item for item in listings
            if str(item.get("owner") or "").strip()
            and "fannie" not in str(item.get("owner") or "").lower()
            and "homepath" not in str(item.get("source") or "").lower()
        ]

        preferred = sorted(preferred, key=_skiptrace_lead_score, reverse=True)
        for item in (preferred or listings)[:5]:
            lead = dict(item)
            lead.setdefault("mailing_address", "")
            lead.setdefault("primary_phone", "")
            lead.setdefault("phone_2", "")
            lead.setdefault("email_1", "")
            lead.setdefault("email_2", "")
            lead.setdefault("skiptrace_notes", "")
            lead.setdefault("skiptrace_status", "pending")
            sample_leads.append(lead)
        orders = [{
            "id": "local-dev-order-1",
            "user_id": 0,
            "email": "local-test@foreclosureleads.dev",
            "amount_cents": len(sample_leads) * 500,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "leads_json": sample_leads,
        }]
        _sqlite_set("skiptrace_dev_orders", orders)

    normalized = []
    for order in orders:
        order_copy = dict(order)
        created = order_copy.get("created_at")
        if isinstance(created, str):
            try:
                order_copy["created_at"] = datetime.fromisoformat(created)
            except ValueError:
                order_copy["created_at"] = None
        normalized.append(order_copy)
    return normalized


def _skiptrace_dev_update(order_id, lead_id, contact_fields):
    orders = _sqlite_get("skiptrace_dev_orders", [])
    updated = False
    for order in orders:
        if str(order.get("id")) != str(order_id):
            continue
        for lead in order.get("leads_json") or []:
            if str(lead.get("id")) == str(lead_id):
                lead.update(contact_fields)
                updated = True
                break
        if updated:
            break
    if not updated:
        return False
    _sqlite_set("skiptrace_dev_orders", orders)
    return True


@app.route('/admin/skiptrace')
@admin_required
def admin_skiptrace():
    if STOREFRONT_ONLY:
        return redirect(url_for('index'), code=302)
    dev_mode = _skiptrace_dev_enabled()
    if not _accounts_ready() and not dev_mode:
        return render_template('checkout_status.html', title="Accounts not configured",
                               message="Set Appwrite env vars and SECRET_KEY before using the skip trace queue.")
    try:
        if dev_mode:
            orders = _skiptrace_dev_orders()
        else:
            db.init_db()
            orders = db.get_paid_orders()
    except Exception as exc:
        return render_template('checkout_status.html', title="Skip trace unavailable",
                               message=f"Could not load paid orders: {type(exc).__name__}")

    queue = []
    for order in orders:
        for lead in order.get("leads_json") or []:
            queue.append({
                "order": order,
                "lead": lead,
                "status": _skiptrace_status(lead),
                "links": _cyberbackgroundchecks_links(lead),
            })
    skiptrace_ctl = skiptrace_control()
    return render_template(
        'skiptrace.html',
        queue=queue,
        dev_mode=dev_mode,
        counties=_skiptrace_county_options(),
        is_vercel=IS_VERCEL,
        is_hosted=IS_HOSTED,
        st=skiptrace_ctl.status(),
    )


@app.route('/admin/skiptrace/run', methods=['POST'])
@admin_required
def admin_skiptrace_run():
    if STOREFRONT_ONLY:
        return redirect(url_for('index'), code=302)
    if _skiptrace_dev_enabled():
        _skiptrace_dev_orders(force_real=True)
    return redirect(url_for('admin_skiptrace'))


@app.route('/admin/skiptrace/update', methods=['POST'])
@admin_required
def admin_skiptrace_update():
    if STOREFRONT_ONLY:
        return redirect(url_for('index'), code=302)
    order_id = request.form.get("order_id")
    lead_id = request.form.get("lead_id")
    contact_fields = {
        "primary_phone": (request.form.get("primary_phone") or "").strip(),
        "phone_2": (request.form.get("phone_2") or "").strip(),
        "email_1": (request.form.get("email_1") or "").strip(),
        "email_2": (request.form.get("email_2") or "").strip(),
        "mailing_address": (request.form.get("mailing_address") or "").strip(),
        "skiptrace_notes": (request.form.get("skiptrace_notes") or "").strip(),
        "skiptrace_source": "CyberBackgroundChecks",
        "skiptraced_at": datetime.now().isoformat(timespec="seconds"),
    }
    contact_fields["skiptrace_status"] = (
        "completed" if contact_fields["primary_phone"] or contact_fields["email_1"] else "pending"
    )
    dev_mode = _skiptrace_dev_enabled()
    try:
        if dev_mode:
            updated = _skiptrace_dev_update(order_id, lead_id, contact_fields)
        else:
            db.init_db()
            updated = db.update_order_lead_contacts(order_id, lead_id, contact_fields)
    except Exception as exc:
        return render_template('checkout_status.html', title="Skip trace update failed",
                               message=f"Could not update contact fields: {type(exc).__name__}")
    if not updated:
        return render_template('checkout_status.html', title="Lead not found",
                               message="That paid order or lead could not be found.")
    return redirect(url_for('admin_skiptrace'))


# ---- Skip trace control panel (local-only background runner) ---------------

def _skiptrace_county_options():
    """Every county in listings.json, with how many leads have owner names (traceable)
    vs total. Counties with no owner names are returned too (UI greys them out).
    Reads the flat file directly (bypasses SQLite) so the full dataset is visible —
    SQLite only holds a subset of rows when data has been pushed via git."""
    try:
        _json_path = _runtime_data_file("listings.json")
        with open(_json_path, "r", encoding="utf-8-sig") as _f:
            _all_items = json.load(_f)
    except Exception:
        _all_items = current_listings()
    counts = {}
    for item in _all_items:
        county = str(item.get("county") or "").strip()
        if not county:
            continue
        bucket = counts.setdefault(county, {"county": county, "total": 0, "traceable": 0, "pending": 0})
        bucket["total"] += 1
        if str(item.get("owner") or "").strip():
            bucket["traceable"] += 1
            if not (str(item.get("primary_phone") or "").strip() or str(item.get("email_1") or "").strip()):
                bucket["pending"] += 1
    out = sorted(counts.values(), key=lambda c: (-c["pending"], -c["traceable"]))
    for c in out:
        c["traced"] = c["traceable"] - c["pending"]          # leads that now have a number
        c["done"] = c["traceable"] > 0 and c["pending"] == 0  # whole county traced
    return out


@app.route('/admin/skiptrace/control')
@admin_required
def admin_skiptrace_control():
    if STOREFRONT_ONLY:
        return redirect(url_for('index'), code=302)
    skiptrace_ctl = skiptrace_control()
    return render_template('skiptrace_control.html',
                           counties=_skiptrace_county_options(),
                           is_vercel=IS_VERCEL,
                           status=skiptrace_ctl.status())


@app.route('/admin/skiptrace/status')
@admin_required
def admin_skiptrace_status():
    if STOREFRONT_ONLY:
        return jsonify({"error": "not available"}), 403
    skiptrace_ctl = skiptrace_control()
    return jsonify(skiptrace_ctl.status())


@app.route('/admin/skiptrace/start', methods=['POST'])
@admin_required
def admin_skiptrace_start():
    if STOREFRONT_ONLY:
        return jsonify({"error": "not available"}), 403
    skiptrace_ctl = skiptrace_control()
    try:
        limit = int(request.form.get("limit") or 0)
    except ValueError:
        limit = 0
    ok, msg = skiptrace_ctl.start(
        county=(request.form.get("county") or "").strip(),
        limit=limit,
        pace=(request.form.get("pace") or "normal"),
        breaks=(request.form.get("breaks") or "normal"),
        skip_traced=(request.form.get("skip_traced") == "on"),
        write=(request.form.get("write") == "on"),
        engine=(request.form.get("engine") or "ddg"),
    )
    return jsonify({"ok": ok, "message": msg, "status": skiptrace_ctl.status()})


@app.route('/admin/skiptrace/stop-after', methods=['POST'])
@admin_required
def admin_skiptrace_stop_after():
    if STOREFRONT_ONLY:
        return jsonify({"error": "not available"}), 403
    skiptrace_ctl = skiptrace_control()
    ok, msg = skiptrace_ctl.stop_after()
    return jsonify({"ok": ok, "message": msg, "status": skiptrace_ctl.status()})


@app.route('/admin/skiptrace/kill', methods=['POST'])
@admin_required
def admin_skiptrace_kill():
    if STOREFRONT_ONLY:
        return jsonify({"error": "not available"}), 403
    skiptrace_ctl = skiptrace_control()
    ok, msg = skiptrace_ctl.kill_now()
    return jsonify({"ok": ok, "message": msg, "status": skiptrace_ctl.status()})


# ---- Trace Export (organize leads into upload-ready CSVs by category/month) --

TRACE_TYPES = ["normal", "advanced", "parcel"]
TRACE_LABELS = {
    "normal": "Normal Trace",
    "advanced": "Advanced Trace",
    "parcel": "Parcel Trace (APN)",
}
TRACE_BLURB = {
    "normal": "Has an owner name — needs phones & emails.",
    "advanced": "No owner name — find the owner, then contacts.",
    "parcel": "Only an APN — look up owner by Parcel ID + County + State.",
}
# Columns written to each category's upload CSV.
TRACE_COLUMNS = {
    "normal": ["lead_id", "owner", "address", "city", "state", "zip", "county"],
    "advanced": ["lead_id", "address", "city", "state", "zip", "county"],
    "parcel": ["lead_id", "parcel_id", "county", "state", "address"],
}


def _trace_type(lead):
    """Route a lead to the category it qualifies for (cheapest-applicable order)."""
    owner = str(lead.get("owner") or "").strip()
    street = str(lead.get("street") or "").strip()
    addr = str(lead.get("address") or "").strip()
    apn = str(lead.get("parcel_id") or "").strip()
    has_real_addr = bool(street) or (bool(addr) and not addr.upper().lstrip().startswith("APN"))
    if owner:
        return "normal"
    if has_real_addr:
        return "advanced"
    if apn:
        return "parcel"
    return ""


def _lead_month(lead):
    """Month the notice/sale was ISSUED (parsed from the sale/record date), not scraped.
    Falls back to scrape month only when no issued date is parseable."""
    yr = str(lead.get("scraped_date") or "")[:4] or None
    for raw in (lead.get("sale_date"), lead.get("date")):
        iso = _parse_sale_date(raw, fallback_year=yr)
        if iso:
            return iso[:7]
    iso = _parse_sale_date(lead.get("scraped_date"))
    return iso[:7] if iso else "undated"


def _trace_full():
    """Every county with per-category totals AND a per-month breakdown, in one pass."""
    counties = {}
    for lead in current_listings():
        county = str(lead.get("county") or "").strip()
        ttype = _trace_type(lead)
        if not county or not ttype:
            continue
        mo = _lead_month(lead)
        c = counties.setdefault(county, {"county": county, "total": 0,
                                         "normal": 0, "advanced": 0, "parcel": 0, "_months": {}})
        c["total"] += 1
        c[ttype] += 1
        m = c["_months"].setdefault(mo, {"month": mo, "total": 0,
                                         "normal": 0, "advanced": 0, "parcel": 0})
        m["total"] += 1
        m[ttype] += 1
    out = []
    for c in sorted(counties.values(), key=lambda c: c["county"].lower()):   # by county A->Z
        months = list(c.pop("_months").values())
        real = sorted((m for m in months if m["month"] != "undated"),
                      key=lambda m: m["month"], reverse=True)               # newest issued first
        und = [m for m in months if m["month"] == "undated"]
        c["months"] = real + und
        out.append(c)
    return out


def _trace_select(county, ttype, month):
    out = []
    for lead in current_listings():
        if county and str(lead.get("county") or "").strip().lower() != county.lower():
            continue
        if _trace_type(lead) != ttype:
            continue
        if month and month != "all" and _lead_month(lead) != month:
            continue
        out.append(lead)
    return out


@app.route('/admin/trace')
@admin_required
def admin_trace():
    if STOREFRONT_ONLY:
        return redirect(url_for('index'), code=302)
    return render_template('trace.html', counties=_trace_full(),
                           types=TRACE_TYPES, type_labels=TRACE_LABELS, type_blurb=TRACE_BLURB)


@app.route('/admin/trace/export')
@admin_required
def admin_trace_export():
    if STOREFRONT_ONLY:
        return redirect(url_for('index'), code=302)
    county = (request.args.get("county") or "").strip()
    ttype = (request.args.get("type") or "").strip()
    month = (request.args.get("month") or "all").strip()
    if ttype not in TRACE_TYPES:
        return Response("Unknown trace type.", status=400, mimetype="text/plain")
    leads = _trace_select(county, ttype, month)
    cols = TRACE_COLUMNS[ttype]
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=cols)
    writer.writeheader()
    for lead in leads:
        writer.writerow({c: (lead.get("id") if c == "lead_id" else lead.get(c, "")) for c in cols})
    fname = f"{(county or 'all')}_{ttype}_{month}.csv".replace(" ", "-").lower()
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


# ---- Checkout --------------------------------------------------------------

@app.route('/api/create-checkout-session', methods=['POST'])
def create_checkout_session():
    if not stripe.api_key:
        return jsonify({"error": "Payments are not configured yet."}), 503
    if not _accounts_ready():
        return jsonify({"error": "Accounts are not configured yet. Set Appwrite env vars and SECRET_KEY."}), 503

    user = current_user()
    if not user:
        return jsonify({"login_required": True,
                        "error": "Please log in to purchase leads."}), 401

    payload = request.get_json(silent=True) or {}
    selected_ids = {str(lead_id) for lead_id in payload.get("lead_ids", [])}
    selected_county = str(payload.get("selected_county") or "").strip().lower()
    if not selected_ids:
        return jsonify({"error": "Select at least one lead first."}), 400
    if not selected_county:
        return jsonify({"error": "Choose a county before checkout."}), 400

    listings = current_listings()
    selected = [item for item in listings if str(item.get("id")) in selected_ids]
    if not selected:
        return jsonify({"error": "Selected leads were not found."}), 400
    mismatched = [
        item for item in selected
        if str(item.get("county") or "").strip().lower() != selected_county
    ]
    if mismatched:
        return jsonify({"error": "Checkout is limited to one selected county at a time. Clear your selection and choose leads from that county only."}), 400

    # County exclusivity: when subscriptions are live, a county claimed by an active
    # subscriber is reserved and not available for per-lead purchase by others.
    if _subscriptions_ready() and selected_county in _claimed_counties(exclude_user=user["id"]):
        return jsonify({"error": "This county is reserved for its subscriber and isn't available for per-lead purchase."}), 409

    total_cents = sum(_listing_price_cents(item) for item in selected)
    if total_cents <= 0:
        return jsonify({"error": "Selected leads do not have a valid price."}), 400

    origin = request.host_url.rstrip("/")
    try:
        checkout_session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            customer_email=user["email"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {
                        "name": f"{selected_county.title()} foreclosure lead(s)",
                    },
                    "unit_amount": total_cents,
                },
                "quantity": 1,
            }],
            metadata={
                "user_id": str(user["id"]),
                "county": selected_county,
                "lead_count": str(len(selected)),
                "lead_ids": ",".join(str(item.get("id")) for item in selected)[:500],
            },
            success_url=f"{origin}/checkout/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{origin}/checkout/cancel",
        )
    except stripe.error.StripeError as exc:
        return jsonify({"error": str(exc)}), 502

    # Record a pending order with a full snapshot of the purchased leads, so the
    # buyer keeps access even if a lead later drops out of the storefront CSV.
    try:
        db.init_db()
        order_id = db.create_pending_order(
            user_id=user["id"],
            email=user["email"],
            stripe_session_id=checkout_session.id,
            amount_cents=total_cents,
            leads=_prepare_purchased_leads(selected),
        )
    except Exception as exc:
        return jsonify({"error": f"Could not record this order before checkout: {type(exc).__name__}"}), 503
    if not order_id:
        return jsonify({"error": "Could not record this order before checkout."}), 503

    return jsonify({"url": checkout_session.url})


def _fulfill_session(session_id):
    """Mark the matching order paid if Stripe confirms payment. Idempotent."""
    if not session_id or not stripe.api_key:
        return False
    try:
        cs = stripe.checkout.Session.retrieve(session_id)
    except stripe.error.StripeError:
        return False
    if cs.get("payment_status") != "paid":
        return False
    try:
        db.init_db()
        return db.mark_order_paid(session_id)
    except Exception:
        return False


@app.route('/checkout/success')
def checkout_success():
    session_id = request.args.get('session_id')
    _fulfill_session(session_id)
    if current_user():
        return redirect(url_for('account'))
    return render_template('checkout_status.html', title="Payment complete",
                           message="Payment complete. Log in to view your purchased leads.")


@app.route('/webhook/stripe', methods=['POST'])
def stripe_webhook():
    secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    if not secret:
        return ("webhook not configured", 200)
    payload = request.get_data()
    sig = request.headers.get('Stripe-Signature', '')
    try:
        event = stripe.Webhook.construct_event(payload, sig, secret)
    except (ValueError, stripe.error.SignatureVerificationError):
        return ("invalid", 400)
    etype = event.get("type")
    obj = event.get("data", {}).get("object", {}) or {}
    if etype == "checkout.session.completed":
        if obj.get("mode") == "subscription":
            _fulfill_subscription(obj.get("id"))      # county subscription
        else:
            _fulfill_session(obj.get("id"))           # per-lead one-time order
    elif etype == "customer.subscription.deleted":
        _set_sub_status(obj.get("id"), "canceled")    # releases the county claim
    elif etype == "customer.subscription.updated":
        status = obj.get("status") or "active"
        new_status = "active" if status == "active" else "canceled"
        _set_sub_status(obj.get("id"), new_status)
        # Sync tier in case price changed (immediate upgrade, or a scheduled
        # downgrade landing at renewal). Clear any pending-downgrade marker
        # once the live price matches the new tier.
        try:
            price_id = obj["items"]["data"][0]["price"]["id"]
            new_tier = next((k for k, v in TIER_PRICES.items() if v == price_id), None)
            if new_tier:
                subs = _load_subs()
                for s in subs:
                    if s.get("stripe_subscription_id") == obj.get("id"):
                        s["tier"] = new_tier
                        if s.get("pending_tier") == new_tier:
                            s.pop("pending_tier", None)
                            s.pop("pending_tier_at", None)
                _save_subs(subs)
        except Exception:
            pass
    elif etype == "invoice.paid" and obj.get("subscription"):
        # renewal -> reset the monthly included-trace allowance
        subs = _load_subs()
        hit = False
        for s in subs:
            if s.get("stripe_subscription_id") == obj.get("subscription"):
                s["traces_used"] = 0
                s["period_start"] = datetime.now().isoformat(timespec="seconds")
                hit = True
        if hit:
            _save_subs(subs)
    return ("ok", 200)

@app.route('/checkout/cancel')
def checkout_cancel():
    return render_template('checkout_status.html', title="Checkout canceled", message="Checkout was canceled. Your selected leads were not charged.")


# ============================ County subscriptions ================================
# All routes below no-op (503) unless _subscriptions_ready(). Stripe price IDs come
# from env (STRIPE_PRICE_COUNTY_FIRST / _ADDL). Nothing here runs in the live app
# until the flag + price IDs are set, so this is safe to ship dormant.

def _sub_period_end(stripe_sub) -> str:
    """current_period_end moved onto the subscription item in newer Stripe API versions."""
    ts = stripe_sub.get("current_period_end")
    if not ts:
        items = (stripe_sub.get("items") or {}).get("data") or []
        ts = items[0].get("current_period_end") if items else None
    try:
        return datetime.utcfromtimestamp(int(ts)).isoformat() if ts else ""
    except Exception:
        return ""


def _activate_subscription(sub_id, customer_id, user_id, county, email="", tier="professional") -> bool:
    if not (sub_id and user_id and county):
        return False
    price_id = ""
    period_end = ""
    try:
        s = stripe.Subscription.retrieve(sub_id)
        period_end = _sub_period_end(s)
        items = (s.get("items") or {}).get("data") or []
        if items:
            price_id = (items[0].get("price") or {}).get("id", "")
    except Exception:
        pass
    # Infer tier from price_id if not explicitly passed
    if tier not in TIER_PRICES:
        tier = next((k for k, v in TIER_PRICES.items() if v == price_id), "professional")
    now = datetime.now().isoformat(timespec="seconds")
    _upsert_sub({
        "id": secrets.token_hex(8),
        "user_id": str(user_id),
        "email": email or "",
        "county": str(county).lower(),
        "tier": tier,
        "stripe_subscription_id": sub_id,
        "stripe_customer_id": customer_id or "",
        "status": "active",
        "price_id": price_id,
        "created_at": now,
        "current_period_end": period_end,
        "period_start": now,
        "traces_used": 0,
    })
    return True


def _set_sub_status(sub_id, status) -> bool:
    """Update an existing subscription's status (e.g. 'canceled' releases the county)."""
    subs = _load_subs()
    changed = False
    for s in subs:
        if s.get("stripe_subscription_id") == sub_id:
            s["status"] = status
            changed = True
    if changed:
        _save_subs(subs)
    return changed


def _fulfill_subscription(session_id) -> bool:
    if not session_id or not stripe.api_key:
        return False
    try:
        cs = stripe.checkout.Session.retrieve(session_id)
    except stripe.error.StripeError:
        return False
    if cs.get("mode") != "subscription":
        return False
    md = cs.get("metadata") or {}
    if md.get("kind") != "county_subscription":
        return False
    email = (cs.get("customer_details") or {}).get("email", "")
    return _activate_subscription(cs.get("subscription"), cs.get("customer"),
                                  md.get("user_id"), md.get("county"), email,
                                  md.get("tier", "professional"))


def record_trace_use(user_id, county) -> dict:
    """Count one skip-trace against the subscriber's monthly allowance. Returns
    {included, used, overage} — when used exceeds INCLUDED_TRACES_PER_MONTH, an
    overage invoice item ($5) is added to the Stripe subscription if a price is set.
    Call this at the point a trace is REVEALED to a subscribed buyer."""
    subs = _load_subs()
    target = None
    for s in subs:
        if (s.get("status") == "active" and str(s.get("user_id")) == str(user_id)
                and str(s.get("county") or "").lower() == str(county).lower()):
            target = s
            break
    if not target:
        return {"included": False, "used": 0, "overage": False}
    target["traces_used"] = int(target.get("traces_used") or 0) + 1
    used = target["traces_used"]
    included = _tier_included(target.get("tier", "professional"))
    overage = used > included
    _save_subs(subs)
    if overage and STRIPE_PRICE_TRACE_OVERAGE and target.get("stripe_subscription_id"):
        try:
            stripe.InvoiceItem.create(
                customer=target.get("stripe_customer_id"),
                price=STRIPE_PRICE_TRACE_OVERAGE,
                subscription=target.get("stripe_subscription_id"),
            )
        except Exception:
            pass  # never block a reveal on billing; reconcile later
    return {"included": True, "used": used, "overage": overage}


@app.route('/api/subscriber/reveal', methods=['POST'])
@login_required
def subscriber_reveal():
    """Return real contact data for one lead to an active county subscriber.
    Counts against their monthly allotment; triggers overage charge if exceeded."""
    user = current_user()
    payload = request.get_json(silent=True) or {}
    lead_id = str(payload.get("lead_id") or "").strip()
    if not lead_id:
        return jsonify({"error": "lead_id required"}), 400

    listings = current_listings()
    lead = next((r for r in listings if str(r.get("id")) == lead_id), None)
    if not lead:
        return jsonify({"error": "Lead not found"}), 404

    county = str(lead.get("county") or "").strip().lower()
    user_subs = _user_subs(user["id"])
    sub = next((s for s in user_subs
                if str(s.get("county") or "").lower() == county
                and s.get("status") == "active"), None)
    if not sub:
        return jsonify({"error": "You don't have an active subscription for this county."}), 403

    result = record_trace_use(user["id"], county)

    phone = str(lead.get("primary_phone") or "").strip()
    email = str(lead.get("email_1") or "").strip()
    if not phone and not email:
        return jsonify({
            "phone": "",
            "email": "",
            "overage": result.get("overage", False),
            "used": result.get("used", 0),
            "no_contact": True,
        })

    return jsonify({
        "phone": phone,
        "email": email,
        "overage": result.get("overage", False),
        "used": result.get("used", 0),
    })


@app.route('/api/subscriber/change-tier', methods=['POST'])
@login_required
def subscriber_change_tier():
    """Change a county subscription's tier.
    Upgrades apply immediately (prorated charge now). Downgrades are scheduled
    at the next renewal so the buyer keeps the higher lead allowance for the
    cycle they already paid for."""
    user = current_user()
    payload = request.get_json(silent=True) or {}
    county = str(payload.get("county") or "").strip().lower()
    new_tier = str(payload.get("tier") or "").strip().lower()

    if not county:
        return jsonify({"error": "county required"}), 400
    if new_tier not in TIER_PRICES:
        return jsonify({"error": "Invalid tier"}), 400

    user_subs = _user_subs(user["id"])
    sub = next((s for s in user_subs
                if str(s.get("county") or "").lower() == county
                and s.get("status") == "active"), None)
    if not sub:
        return jsonify({"error": "No active subscription found for this county."}), 404

    current_tier = str(sub.get("tier") or "professional").lower()
    if current_tier == new_tier:
        return jsonify({"error": f"You're already on the {TIER_LABELS[new_tier]} plan."}), 400

    stripe_sub_id = sub.get("stripe_subscription_id")
    if not stripe_sub_id:
        return jsonify({"error": "Subscription record missing Stripe ID."}), 500

    is_upgrade = TIER_AMOUNTS.get(new_tier, 0) > TIER_AMOUNTS.get(current_tier, 0)
    period_end = None

    try:
        stripe_sub = stripe.Subscription.retrieve(stripe_sub_id)
        existing_schedule = stripe_sub.get("schedule")
        period_end = stripe_sub.get("current_period_end")

        if is_upgrade:
            # Cancel any pending downgrade, then switch now with prorated charge.
            if existing_schedule:
                try:
                    stripe.SubscriptionSchedule.release(existing_schedule)
                except stripe.error.StripeError:
                    pass
                stripe_sub = stripe.Subscription.retrieve(stripe_sub_id)
            item_id = stripe_sub["items"]["data"][0]["id"]
            stripe.Subscription.modify(
                stripe_sub_id,
                items=[{"id": item_id, "price": TIER_PRICES[new_tier]}],
                proration_behavior="always_invoice",
            )
        else:
            # Downgrade: schedule the new price to start at the next renewal.
            # Rebuild the schedule fresh so the current phase mirrors reality.
            if existing_schedule:
                try:
                    stripe.SubscriptionSchedule.release(existing_schedule)
                except stripe.error.StripeError:
                    pass
            schedule = stripe.SubscriptionSchedule.create(from_subscription=stripe_sub_id)
            phase = schedule["phases"][0]
            stripe.SubscriptionSchedule.modify(
                schedule["id"],
                end_behavior="release",
                phases=[
                    {
                        "items": [{"price": phase["items"][0]["price"], "quantity": 1}],
                        "start_date": phase["start_date"],
                        "end_date": phase["end_date"],
                    },
                    {
                        "items": [{"price": TIER_PRICES[new_tier], "quantity": 1}],
                    },
                ],
            )
    except stripe.error.StripeError as exc:
        return jsonify({"error": str(exc)}), 502

    # Persist local record. Upgrade flips tier now; downgrade keeps the current
    # tier and records the pending change (webhook clears it at renewal).
    subs = _load_subs()
    for s in subs:
        if s.get("stripe_subscription_id") == stripe_sub_id:
            if is_upgrade:
                s["tier"] = new_tier
                s.pop("pending_tier", None)
                s.pop("pending_tier_at", None)
            else:
                s["pending_tier"] = new_tier
                s["pending_tier_at"] = period_end
    _save_subs(subs)

    return jsonify({
        "ok": True,
        "scheduled": (not is_upgrade),
        "tier": new_tier if is_upgrade else current_tier,
        "pending_tier": (None if is_upgrade else new_tier),
        "label": TIER_LABELS[new_tier],
        "amount": TIER_AMOUNTS[new_tier],
        "included": TIER_INCLUDED[new_tier],
    })


@app.route('/subscribe', methods=['POST'])
def subscribe():
    if not _subscriptions_ready():
        return jsonify({"error": "Subscriptions are not enabled yet."}), 503
    if not _accounts_ready():
        return jsonify({"error": "Accounts are not configured yet."}), 503
    user = current_user()
    if not user:
        return jsonify({"login_required": True, "error": "Please log in to subscribe."}), 401
    payload = request.get_json(silent=True) or request.form
    county = str(payload.get("county") or "").strip().lower()
    tier   = str(payload.get("tier") or "professional").strip().lower()
    if tier not in TIER_PRICES:
        tier = "professional"
    if not county:
        return jsonify({"error": "Choose a county to subscribe to."}), 400
    if any(str(s.get("county") or "").lower() == county for s in _user_subs(user["id"])):
        return jsonify({"error": "You already have an active subscription for this county."}), 409
    untapped = _county_untapped_counts()
    county_untapped = untapped.get(county, 0)
    if county_untapped < SUB_MIN_UNTAPPED:
        return jsonify({"error": "This county doesn't have enough available leads for a subscription right now."}), 409
    cap = _county_capacity(county_untapped)
    current_count = _county_sub_counts().get(county, 0)
    if current_count >= cap:
        return jsonify({"error": f"This county is full ({current_count}/{cap} slots taken). Try another county."}), 409

    price_id = _sub_price_for(tier)
    meta = {"kind": "county_subscription", "user_id": str(user["id"]), "county": county, "tier": tier}
    origin = request.host_url.rstrip("/")
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            customer_email=user["email"],
            line_items=[{"price": price_id, "quantity": 1}],
            metadata=meta,
            subscription_data={"metadata": meta},
            success_url=f"{origin}/subscribe/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{origin}/checkout/cancel",
        )
    except stripe.error.StripeError as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({"url": session.url})


@app.route('/subscribe/success')
def subscribe_success():
    _fulfill_subscription(request.args.get('session_id'))
    if current_user():
        return redirect(url_for('account'))
    return render_template('checkout_status.html', title="Subscription active",
                           message="Subscription active. Log in to manage your counties.")


@app.route('/billing/portal', methods=['POST'])
def billing_portal():
    """Send a subscriber to Stripe's customer portal to manage/cancel."""
    if not _subscriptions_ready():
        return jsonify({"error": "Subscriptions are not enabled."}), 503
    user = current_user()
    if not user:
        return jsonify({"login_required": True}), 401
    subs = _user_subs(user["id"])
    cust = next((s.get("stripe_customer_id") for s in subs if s.get("stripe_customer_id")), "")
    if not cust:
        return jsonify({"error": "No active subscription found."}), 404
    origin = request.host_url.rstrip("/")
    try:
        portal = stripe.billing_portal.Session.create(customer=cust, return_url=f"{origin}/account")
    except stripe.error.StripeError as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({"url": portal.url})

@app.route('/api/stripe-status')
@admin_required
def stripe_status():
    key = stripe.api_key or ""
    return jsonify({
        "stripe_configured": bool(key),
        "webhook_configured": bool(os.getenv("STRIPE_WEBHOOK_SECRET", "")),
        "key_prefix": (key[:8] if key else None),
    })

@app.route('/api/accounts-status')
@admin_required
def accounts_status():
    """Diagnostic for Appwrite/Postgres accounts."""
    backend_configured = db.is_configured()
    appwrite_configured = db.appwrite_configured()
    secret_set = bool(app.secret_key)
    backend_connected = False
    detail = None
    if backend_configured:
        try:
            db.init_db()
            backend_connected = True
        except Exception as exc:  # surface a short, password-free reason
            detail = type(exc).__name__ + ": " + str(exc)[:200]
    return jsonify({
        "accounts_ready": bool(backend_configured and secret_set and backend_connected),
        "backend": "appwrite" if appwrite_configured else "postgres" if backend_configured else None,
        "appwrite_configured": appwrite_configured,
        "backend_configured": backend_configured,
        "secret_key_set": secret_set,
        "backend_connected": backend_connected,
        "detail": detail,
    })

@app.route('/api/checkout-status')
@admin_required
def checkout_status_api():
    """Diagnostic for the full purchase-unlock flow."""
    backend_configured = db.is_configured()
    appwrite_configured = db.appwrite_configured()
    secret_set = bool(app.secret_key)
    stripe_set = bool(stripe.api_key)
    webhook_set = bool(os.getenv("STRIPE_WEBHOOK_SECRET", ""))
    backend_connected = False
    detail = None
    if backend_configured:
        try:
            db.init_db()
            backend_connected = True
        except Exception as exc:
            detail = type(exc).__name__ + ": " + str(exc)[:200]
    return jsonify({
        "checkout_ready": bool(stripe_set and backend_configured and secret_set and backend_connected),
        "stripe_secret_key_set": stripe_set,
        "stripe_webhook_secret_set": webhook_set,
        "backend": "appwrite" if appwrite_configured else "postgres" if backend_configured else None,
        "appwrite_configured": appwrite_configured,
        "backend_configured": backend_configured,
        "secret_key_set": secret_set,
        "backend_connected": backend_connected,
        "detail": detail,
    })

CSV_FIELDNAMES = [
    "county", "record_type", "owner_name", "property_address", "city",
    "state", "zip_code", "parcel_id", "tax_year", "amount_owed",
    "sale_date", "case_number", "source_url", "scraped_date", "notes",
]
CSV_LABELS = {
    "county": "County", "record_type": "Type", "owner_name": "Owner Name",
    "property_address": "Property Address", "city": "City", "state": "State",
    "zip_code": "ZIP", "parcel_id": "Parcel ID", "tax_year": "Tax Year",
    "amount_owed": "Amount Owed", "sale_date": "Sale / Record Date",
    "case_number": "Case #", "source_url": "Source", "scraped_date": "Scraped Date",
    "notes": "Notes", "sale_date_iso": "Sale Date (sortable)",
    "primary_phone": "Phone", "email_1": "Email",
}

def _list_csv_files():
    import glob
    files = sorted(
        glob.glob(os.path.join(DATA_DIR, '*.csv')),
        key=lambda p: os.path.getmtime(p),
        reverse=True,
    )
    return [os.path.basename(f) for f in files]

def _write_scrape_csv(source_key, records):
    if not records:
        return ""
    os.makedirs(DATA_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", source_key)
    filename = f"{safe_key}_{stamp}.csv"
    path = os.path.join(DATA_DIR, filename)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        for record in records:
            writer.writerow({field: record.get(field, "") for field in CSV_FIELDNAMES})
    return filename

def _load_csv_rows(filename):
    path = os.path.join(DATA_DIR, filename)
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            if filename == "storefront_listings.csv":
                status = (row.get("status") or "").strip()
                if not row.get("record_type"):
                    row["record_type"] = status
                if not row.get("owner_name"):
                    row["owner_name"] = row.get("owner", "")
                if not row.get("property_address"):
                    row["property_address"] = row.get("address", "")
                if not row.get("zip_code"):
                    row["zip_code"] = row.get("zip", "")
                if not row.get("source_url"):
                    row["source_url"] = row.get("link", "")
                if status == "Tax Lien":
                    row["record_type"] = "Tax Delinquent"
                elif status == "Pre-foreclosure":
                    row["record_type"] = "Pre-Foreclosure"
            rows.append(row)
    return rows

@app.route('/pricing')
def pricing():
    if STOREFRONT_ONLY:
        county_count = len({str(item.get("county") or "").strip() for item in current_listings() if item.get("county")})
    else:
        county_count = len(county_scraper_map())
    # Subscribable counties (only computed when subscriptions are live)
    sub_counties = []
    is_first_county = True
    if _subscriptions_ready():
        user = current_user()
        user_sub_counties = set()
        if user:
            is_first_county = not _user_subs(user["id"])
            user_sub_counties = {str(s.get("county") or "").lower() for s in _user_subs(user["id"])}
        untapped_map = _county_untapped_counts()
        sub_counts_map = _county_sub_counts()
        counties = sorted({str(r.get("county") or "").strip().lower()
                           for r in current_listings() if r.get("county")})
        for c in counties:
            untapped = untapped_map.get(c, 0)
            if untapped < SUB_MIN_UNTAPPED:
                continue  # too few leads — not worth a subscription
            cap = _county_capacity(untapped)
            current_subs = sub_counts_map.get(c, 0)
            # Full if at capacity, but don't show as full to a user who already holds this county
            full = (current_subs >= cap) and (c not in user_sub_counties)
            sub_counties.append({
                "key": c,
                "label": c.title(),
                "taken": full,
                "subs": current_subs,
                "capacity": cap,
            })
    return render_template('pricing.html', county_count=county_count,
                           sub_counties=sub_counties, is_first_county=is_first_county)


# ── County schedule ──────────────────────────────────────────────────────────

_COUNTY_SCHEDULE = [
    # Florida — Daily (Jax Daily Record posts every business day)
    {"ui_key":"duval-fl",    "label":"Duval (Jacksonville)", "state":"FL", "region":"Florida (Northeast)", "frequency":"Daily",    "freq_note":"Jax Daily Record, weekdays",         "listing_names":["duval"],       "source_keys":["duval_jaxdailyrecord","duval_jaxdailyrecord_retax"]},
    {"ui_key":"clay-fl",     "label":"Clay",                 "state":"FL", "region":"Florida (Northeast)", "frequency":"Daily",    "freq_note":"Jax Daily Record, weekdays",         "listing_names":["clay"],        "source_keys":["clay_jaxdailyrecord"]},
    {"ui_key":"nassau-fl",   "label":"Nassau",               "state":"FL", "region":"Florida (Northeast)", "frequency":"Daily",    "freq_note":"Jax Daily Record, weekdays",         "listing_names":["nassau"],      "source_keys":["nassau_jaxdailyrecord"]},
    {"ui_key":"stjohns-fl",  "label":"St. Johns",            "state":"FL", "region":"Florida (Northeast)", "frequency":"Daily",    "freq_note":"Jax Daily Record, weekdays",         "listing_names":["st. johns"],   "source_keys":["stjohns_jaxdailyrecord"]},
    # Michigan — Weekly (mipublicnotices.com, new filings weekly)
    {"ui_key":"wayne-mi",       "label":"Wayne (Detroit)",       "state":"MI", "region":"Michigan", "frequency":"Weekly", "freq_note":"~971 leads/90d", "listing_names":["wayne"],      "source_keys":["wayne_legalnotices"]},
    {"ui_key":"macomb-mi",      "label":"Macomb (Warren)",       "state":"MI", "region":"Michigan", "frequency":"Weekly", "freq_note":"~467 leads/90d", "listing_names":["macomb"],     "source_keys":["macomb_legalnotices"]},
    {"ui_key":"oakland-mi",     "label":"Oakland (Pontiac)",     "state":"MI", "region":"Michigan", "frequency":"Weekly", "freq_note":"~444 leads/90d", "listing_names":["oakland"],    "source_keys":["oakland_legalnotices"]},
    {"ui_key":"genesee-mi",     "label":"Genesee (Flint)",       "state":"MI", "region":"Michigan", "frequency":"Weekly", "freq_note":"~243 leads/90d", "listing_names":["genesee"],    "source_keys":["genesee_legalnotices"]},
    {"ui_key":"ingham-mi",      "label":"Ingham (Lansing)",      "state":"MI", "region":"Michigan", "frequency":"Weekly", "freq_note":"~168 leads/90d", "listing_names":["ingham"],     "source_keys":["ingham_legalnotices"]},
    {"ui_key":"kent-mi",        "label":"Kent (Grand Rapids)",   "state":"MI", "region":"Michigan", "frequency":"Weekly", "freq_note":"~140 leads/90d", "listing_names":["kent"],       "source_keys":["kent_legalnotices"]},
    {"ui_key":"jackson-mi",     "label":"Jackson",               "state":"MI", "region":"Michigan", "frequency":"Weekly", "freq_note":"~109 leads/90d", "listing_names":["jackson"],    "source_keys":["jackson_legalnotices"]},
    {"ui_key":"muskegon-mi",    "label":"Muskegon",              "state":"MI", "region":"Michigan", "frequency":"Weekly", "freq_note":"~95 leads/90d",  "listing_names":["muskegon"],   "source_keys":["muskegon_legalnotices"]},
    {"ui_key":"kalamazoo-mi",   "label":"Kalamazoo",             "state":"MI", "region":"Michigan", "frequency":"Weekly", "freq_note":"~69 leads/90d",  "listing_names":["kalamazoo"],  "source_keys":["kalamazoo_legalnotices"]},
    {"ui_key":"calhoun-mi",     "label":"Calhoun (Battle Creek)","state":"MI", "region":"Michigan", "frequency":"Weekly", "freq_note":"~74 leads/90d",  "listing_names":["calhoun"],    "source_keys":["calhoun_legalnotices"]},
    {"ui_key":"berrien-mi",     "label":"Berrien (Benton Harbor)","state":"MI","region":"Michigan", "frequency":"Weekly", "freq_note":"~62 leads/90d",  "listing_names":["berrien"],    "source_keys":["berrien_legalnotices"]},
    {"ui_key":"washtenaw-mi",   "label":"Washtenaw (Ann Arbor)", "state":"MI", "region":"Michigan", "frequency":"Weekly", "freq_note":"~59 leads/90d",  "listing_names":["washtenaw"],  "source_keys":["washtenaw_legalnotices"]},
    {"ui_key":"livingston-mi",  "label":"Livingston (Howell)",   "state":"MI", "region":"Michigan", "frequency":"Weekly", "freq_note":"~42 leads/90d",  "listing_names":["livingston"], "source_keys":["livingston_legalnotices"]},
    {"ui_key":"ottawa-mi",      "label":"Ottawa (Holland)",      "state":"MI", "region":"Michigan", "frequency":"Weekly", "freq_note":"~32 leads/90d",  "listing_names":["ottawa"],     "source_keys":["ottawa_legalnotices"]},
    {"ui_key":"saginaw-mi",     "label":"Saginaw",               "state":"MI", "region":"Michigan", "frequency":"Weekly", "freq_note":"~3 leads/90d",   "listing_names":["saginaw"],    "source_keys":["saginaw_legalnotices"]},
    {"ui_key":"barry-mi",       "label":"Barry (Hastings)",      "state":"MI", "region":"Michigan", "frequency":"Weekly / Quarterly", "freq_note":"Notices: weekly | Tax auction: quarterly", "listing_names":["barry"], "source_keys":["barry_legalnotices","barry_taxforeclosure"]},
    # California — Weekly (trustee-sale notices filed weekly with county recorder)
    {"ui_key":"sandiego",       "label":"San Diego",             "state":"CA", "region":"California", "frequency":"Weekly", "freq_note":"Trustee-sale notices", "listing_names":["san diego"],     "source_keys":["sandiego_legalnotices","sandiego_taxsale"]},
    {"ui_key":"losangeles",     "label":"Los Angeles",           "state":"CA", "region":"California", "frequency":"Weekly", "freq_note":"Trustee-sale notices", "listing_names":["los angeles"],   "source_keys":["losangeles_legalnotices"]},
    {"ui_key":"orange",         "label":"Orange",                "state":"CA", "region":"California", "frequency":"Weekly", "freq_note":"Trustee-sale notices", "listing_names":["orange"],        "source_keys":["orange_legalnotices","orange_taxsale"]},
    {"ui_key":"riverside",      "label":"Riverside",             "state":"CA", "region":"California", "frequency":"Weekly", "freq_note":"Trustee-sale notices", "listing_names":["riverside"],     "source_keys":["riverside_legalnotices"]},
    {"ui_key":"sanbernardino",  "label":"San Bernardino",        "state":"CA", "region":"California", "frequency":"Weekly", "freq_note":"Trustee-sale notices", "listing_names":["san bernardino"],"source_keys":["sanbernardino_legalnotices"]},
    {"ui_key":"ventura",        "label":"Ventura",               "state":"CA", "region":"California", "frequency":"Weekly", "freq_note":"Trustee-sale notices", "listing_names":["ventura"],       "source_keys":["ventura_legalnotices"]},
    {"ui_key":"sacramento",     "label":"Sacramento",            "state":"CA", "region":"California", "frequency":"Weekly", "freq_note":"Trustee-sale notices", "listing_names":["sacramento"],    "source_keys":["sacramento_legalnotices"]},
    {"ui_key":"alameda",        "label":"Alameda (Oakland)",     "state":"CA", "region":"California", "frequency":"Weekly", "freq_note":"Trustee-sale notices", "listing_names":["alameda"],       "source_keys":["alameda_legalnotices"]},
    {"ui_key":"santaclara",     "label":"Santa Clara (San Jose)","state":"CA", "region":"California", "frequency":"Weekly", "freq_note":"Trustee-sale notices", "listing_names":["santa clara"],   "source_keys":["santaclara_legalnotices"]},
    {"ui_key":"kern",           "label":"Kern (Bakersfield)",    "state":"CA", "region":"California", "frequency":"Weekly", "freq_note":"Trustee-sale notices", "listing_names":["kern"],          "source_keys":["kern_legalnotices"]},
    {"ui_key":"fresno",         "label":"Fresno",                "state":"CA", "region":"California", "frequency":"Weekly", "freq_note":"Trustee-sale notices", "listing_names":["fresno"],        "source_keys":["fresno_legalnotices"]},
    {"ui_key":"contracosta",    "label":"Contra Costa",          "state":"CA", "region":"California", "frequency":"Weekly", "freq_note":"Trustee-sale notices", "listing_names":["contra costa"],  "source_keys":["contracosta_legalnotices"]},
    {"ui_key":"sanmateo",       "label":"San Mateo",             "state":"CA", "region":"California", "frequency":"Weekly", "freq_note":"Trustee-sale notices", "listing_names":["san mateo"],     "source_keys":["sanmateo_legalnotices"]},
    # Arizona — Weekly
    {"ui_key":"maricopa-az",    "label":"Maricopa (Phoenix)",    "state":"AZ", "region":"Arizona", "frequency":"Weekly", "freq_note":"Trustee sales published weekly", "listing_names":["maricopa"], "source_keys":["maricopa_trusteesale","maricopa_azcapitoltimes","maricopa_recordreporter"]},
    # Texas — Monthly
    {"ui_key":"harris-tx",      "label":"Harris (Houston)",      "state":"TX", "region":"Texas", "frequency":"Monthly", "freq_note":"Delinquent tax list updates ~monthly", "listing_names":["harris"], "source_keys":["harris_taxsale"]},
    # Tennessee — Monthly
    {"ui_key":"davidson",       "label":"Davidson (Nashville)",  "state":"TN", "region":"Tennessee", "frequency":"Monthly", "freq_note":"Chancery Court posts lists ~monthly", "listing_names":["davidson"],   "source_keys":["davidson"]},
    {"ui_key":"williamson",     "label":"Williamson (Franklin)", "state":"TN", "region":"Tennessee", "frequency":"Monthly", "freq_note":"County delinquent tax list ~monthly",  "listing_names":["williamson"], "source_keys":["williamson"]},
    {"ui_key":"rutherford",     "label":"Rutherford (Murfreesboro)","state":"TN","region":"Tennessee","frequency":"Monthly","freq_note":"RC Chancery Court ~monthly",            "listing_names":["rutherford"], "source_keys":["rutherford"]},
    {"ui_key":"wilson-tn",      "label":"Wilson (Lebanon)",      "state":"TN", "region":"Tennessee", "frequency":"Monthly", "freq_note":"Chancery Court ~monthly",              "listing_names":["wilson"],     "source_keys":["wilson"]},
    {"ui_key":"sumner",         "label":"Sumner (Gallatin)",     "state":"TN", "region":"Tennessee", "frequency":"Monthly", "freq_note":"Chancery Court ~monthly",              "listing_names":["sumner"],     "source_keys":["sumner"]},
    {"ui_key":"robertson",      "label":"Robertson (Springfield)","state":"TN","region":"Tennessee", "frequency":"Monthly", "freq_note":"Court auctions ~monthly",              "listing_names":["robertson"],  "source_keys":["robertson"]},
    {"ui_key":"cheatham",       "label":"Cheatham (Ashland City)","state":"TN","region":"Tennessee", "frequency":"Monthly", "freq_note":"Tax sales ~monthly",                  "listing_names":["cheatham"],   "source_keys":["cheatham"]},
]


def _build_county_schedule():
    """Attach live stats (lead count, last scraped) from listings.json to each schedule row."""
    from datetime import date as _date
    today = _date.today()
    # Bypass load_json/SQLite — read the flat file directly so manual data
    # pushes (listings.json written outside the app) are always reflected.
    _json_path = os.path.join(BASE_DIR, 'listings.json')
    try:
        with open(_json_path, 'r', encoding='utf-8-sig') as _f:
            listings = json.load(_f)
    except (OSError, json.JSONDecodeError):
        listings = []
    # Build a dict: normalized county name → {count, last_scraped}
    stats = {}
    for item in listings:
        key = (item.get("county") or "").strip().lower()
        if not key:
            continue
        scraped = (item.get("scraped_date") or item.get("date") or "")[:10]
        if key not in stats:
            stats[key] = {"count": 0, "last_scraped": ""}
        stats[key]["count"] += 1
        if scraped and scraped > stats[key]["last_scraped"]:
            stats[key]["last_scraped"] = scraped

    enriched = []
    for row in _COUNTY_SCHEDULE:
        count = sum(stats.get(n.lower(), {}).get("count", 0) for n in row["listing_names"])
        last = max((stats.get(n.lower(), {}).get("last_scraped", "") for n in row["listing_names"]), default="")
        days_since = 0
        if last:
            try:
                from datetime import datetime as _dt
                days_since = (today - _dt.strptime(last, "%Y-%m-%d").date()).days
            except ValueError:
                pass
        enriched.append({**row, "lead_count": count, "last_scraped": last, "days_since_scraped": days_since})
    return enriched


@app.route('/admin/counties')
@admin_required
def admin_counties():
    if STOREFRONT_ONLY:
        return redirect(url_for('index'), code=302)
    schedule = _build_county_schedule()
    total_leads = sum(r["lead_count"] for r in schedule)
    states = sorted({r["state"] for r in schedule})
    daily_count   = sum(1 for r in schedule if r["frequency"].startswith("Daily"))
    weekly_count  = sum(1 for r in schedule if r["frequency"].startswith("Weekly"))
    monthly_count = sum(1 for r in schedule if r["frequency"].startswith("Monthly"))
    return render_template(
        'admin_counties.html',
        schedule=schedule,
        total_counties=len(schedule),
        total_leads=total_leads,
        states=states,
        daily_count=daily_count,
        weekly_count=weekly_count,
        monthly_count=monthly_count,
    )


@app.route('/admin/data')
@admin_required
def admin_data():
    if STOREFRONT_ONLY:
        return redirect(url_for('index'), code=302)

    files = _list_csv_files()
    settings = normalize_settings(load_json(SETTINGS_FILE, {}))
    selected = request.args.get('file', '') or (files[0] if files else '')
    filter_county = request.args.get('filter_county', '').strip().lower()
    filter_type   = request.args.get('filter_type', '').strip().lower()
    filter_q      = request.args.get('q', '').strip().lower()
    sort_col      = request.args.get('sort', 'county')
    sort_dir      = request.args.get('dir', 'asc')

    rows = _load_csv_rows(selected) if selected else []
    total = len(rows)

    # Normalize the messy sale/record date into a clean, sortable ISO column.
    for r in rows:
        yr = (str(r.get("scraped_date") or "")[:4] or None)
        r["sale_date_iso"] = _parse_sale_date(r.get("sale_date") or r.get("date"), fallback_year=yr)

    if filter_county:
        rows = [r for r in rows if filter_county in r.get('county', '').lower()]
    if filter_type:
        rows = [r for r in rows if filter_type in r.get('record_type', '').lower()]
    if filter_q:
        rows = [r for r in rows if any(filter_q in str(v).lower() for v in r.values())]

    # Enrich CSV rows with skip-trace phone/email from current_listings (by lead id)
    listings_lookup = {str(r.get("id")): r for r in current_listings() if r.get("id")}
    for r in rows:
        live = listings_lookup.get(str(r.get("id") or "")) or {}
        r["primary_phone"] = live.get("primary_phone") or ""
        r["email_1"] = live.get("email_1") or ""

    display_fields = list(CSV_FIELDNAMES) + ["primary_phone", "email_1", "sale_date_iso"]
    if sort_col in display_fields:
        rows = sorted(rows, key=lambda r: _csv_sort_value(r, sort_col), reverse=(sort_dir == 'desc'))

    all_rows = _load_csv_rows(selected) if selected else []
    counties = sorted({r.get('county', '') for r in all_rows if r.get('county')})
    types    = sorted({r.get('record_type', '') for r in all_rows if r.get('record_type')})

    return render_template(
        'admin_data.html',
        rows=rows, total=total, filtered=len(rows),
        files=files, selected=selected,
        fieldnames=display_fields, column_labels=CSV_LABELS,
        filter_county=request.args.get('filter_county', ''),
        filter_type=request.args.get('filter_type', ''),
        filter_q=request.args.get('q', ''),
        sort_col=sort_col, sort_dir=sort_dir,
        counties=counties, types=types,
        settings=settings,
    )

@app.route('/admin/data/download')
@admin_required
def admin_data_download():
    if STOREFRONT_ONLY:
        return redirect(url_for('index'), code=302)
    from flask import send_file
    filename = request.args.get('file', '')
    if not filename:
        files = _list_csv_files()
        if not files:
            return "No CSV files available", 404
        filename = files[0]
    path = os.path.join(DATA_DIR, filename)
    if not os.path.exists(path):
        return "File not found", 404
    return send_file(path, as_attachment=True, download_name=filename)

@app.route('/csv-dash')
@admin_required
def csv_dash():
    if STOREFRONT_ONLY:
        return redirect(url_for('index'), code=302)
    listings = current_listings()
    settings = normalize_settings(load_json(SETTINGS_FILE, {}))
    return render_template('csv_dash.html', listings=listings, settings=settings)

@app.route('/api/settings', methods=['POST'])
@admin_required
def update_settings():
    if STOREFRONT_ONLY:
        return jsonify({"status": "disabled", "reason": "storefront_only"}), 403
    settings = request.json
    save_json(SETTINGS_FILE, settings)
    return jsonify({"status": "success"})

@app.route('/api/sources')
@admin_required
def sources_json():
    if STOREFRONT_ONLY:
        return jsonify({"sources": [], "county_sources": {}, "storefront_only": True})
    return jsonify({
        "sources": sorted(
            source_metadata().values(),
            key=lambda s: (s.get("region", ""), s.get("label", "")),
        ),
        "county_sources": county_scraper_map(),
    })

@app.route('/api/scrape', methods=['POST'])
@admin_required
def run_scraper():
    if STOREFRONT_ONLY:
        return jsonify({"status": "disabled", "reason": "storefront_only"}), 403
    scrape_sync, run_scrapers, _request_kill, clear_kill, ScraperKilled = scraper_runtime()
    scraper_map = county_scraper_map()

    if scrape_status["running"]:
        return jsonify({"status": "already_running"})

    payload = request.get_json(silent=True) or {}
    settings = normalize_settings(load_json(SETTINGS_FILE, {}))
    counties = payload.get("counties") or settings.get("counties") or ["chatham-ga", "glynn-ga", "camden-ga", "duval-fl", "stjohns-fl", "nassau-fl"]
    lookback = int(payload.get("lookback_days", settings.get("lookback_days", 30)))
    lookback = max(1, min(365, lookback))
    sources = {
        "include_tax_records": bool(payload.get("include_tax_records", settings["sources"]["include_tax_records"])),
        "include_hud": bool(payload.get("include_hud", settings["sources"]["include_hud"])),
        "include_homepath": bool(payload.get("include_homepath", settings["sources"]["include_homepath"])),
    }
    counties = prioritize_counties(counties, sources)
    if payload.get("save_defaults"):
        settings["counties"] = counties
        settings["lookback_days"] = lookback
        settings["sources"] = sources
        save_json(SETTINGS_FILE, settings)
    stop_event = threading.Event()
    scrape_control["stop_event"] = stop_event
    scrape_status["stopping"] = False
    clear_kill()  # reset kill flag at start of new run

    def do_scrape():
        scrape_status["running"] = True
        scrape_status["started_at"] = datetime.now().isoformat(timespec="seconds")
        scrape_status["updated_at"] = None
        existing_count = len(load_json(DATA_FILE, []))
        scrape_status["count"] = existing_count
        scrape_status["last"] = "running"
        scrape_status["scraper_results"]["_existing_in_db"] = {
            "count": existing_count, "raw": existing_count,
            "status": "ok", "note": f"{existing_count} records already in database before this run",
        }

        def save_batch(batch):
            with listing_lock:
                current = load_json(DATA_FILE, [])
                merged, added = merge_listings(current, batch)
                save_json(DATA_FILE, merged)
                scrape_status["count"] = len(merged)
                scrape_status["updated_at"] = datetime.now().isoformat(timespec="seconds")
                scrape_status["last"] = f"saved {added} new listing(s)"
                return added

        def update_progress(message):
            scrape_status["current_step"] = message
            scrape_status["updated_at"] = datetime.now().isoformat(timespec="seconds")
            scrape_status["last"] = message

        try:
            scrape_status["scraper_results"] = {}

            # Phase 1: Tax delinquency PDFs via scrapers/
            if sources.get("include_tax_records") and not stop_event.is_set():
                scraper_counties = [
                    sk for c in counties if c in scraper_map
                    for sk in scraper_map[c]
                ]
                if scraper_counties:
                    # Run each scraper individually so we can track per-key results
                    for sk in scraper_counties:
                        if stop_event.is_set():
                            break
                        update_progress(f"Scraping: {sk}")
                        try:
                            recs = run_scrapers([sk], lookback_days=lookback)
                            csv_file = _write_scrape_csv(sk, recs)
                            listings = property_records_to_listings(recs)
                            real = [r for r in recs if r.get("owner_name") or r.get("parcel_id") or r.get("property_address")]
                            stub_only = len(real) == 0
                            new_count = save_batch(listings) if listings else 0
                            status = "empty" if stub_only else "ok"
                            note = "stub/portal link only" if stub_only else f"{new_count} new / {len(listings)} kept / {len(recs)} raw"
                            if csv_file:
                                note += f" / CSV: {csv_file}"
                            scrape_status["scraper_results"][sk] = source_result(
                                sk, len(recs), len(listings), new_count, status, note
                            )
                        except ScraperKilled:
                            scrape_status["scraper_results"][sk] = source_result(sk, 0, 0, 0, "error", "killed mid-run")
                            update_progress(f"{sk} killed mid-run")
                            break  # exit the per-scraper loop entirely
                        except Exception as e:
                            scrape_status["scraper_results"][sk] = source_result(sk, 0, 0, 0, "error", str(e)[:120])
                            update_progress(f"Error scraping {sk}: {e}")

            # Phase 2: HUD + HomePath REO via scraper.py
            if not stop_event.is_set():
                p2_before = scrape_status["count"]
                results = scrape_sync(
                    counties,
                    lookback,
                    stop_event=stop_event,
                    options=sources,
                    on_results=save_batch,
                    on_progress=update_progress,
                )
                if results:
                    with listing_lock:
                        current = load_json(DATA_FILE, [])
                        merged, _ = merge_listings(current, results)
                        save_json(DATA_FILE, merged)
                        scrape_status["count"] = len(merged)
                p2_count = scrape_status["count"] - p2_before
                scrape_status["scraper_results"]["hud_homepath"] = {
                    "count": max(0, p2_count),
                    "raw": len(results) if results else 0,
                    "status": "ok" if p2_count > 0 else "empty",
                    "note": f"{max(0,p2_count)} new leads (HUD + HomePath)" if p2_count > 0 else "no new listings found",
                }

            scrape_status["last"] = "stopped" if stop_event.is_set() else "success"
        except Exception as e:
            scrape_status["last"] = f"error: {e}"
        finally:
            scrape_status["running"] = False
            scrape_status["stopping"] = False
            scrape_status["current_step"] = None
            scrape_control["thread"] = None

    t = threading.Thread(target=do_scrape)
    t.daemon = True
    scrape_control["thread"] = t
    t.start()
    return jsonify({"status": "started", "counties": counties, "lookback_days": lookback})

@app.route('/api/scrape/stop', methods=['POST'])
@admin_required
def stop_scraper():
    if STOREFRONT_ONLY:
        return jsonify({"status": "disabled", "reason": "storefront_only"}), 403

    if not scrape_status["running"]:
        return jsonify({"status": "not_running"})
    force = (request.args.get("force") == "true" or
             (request.get_json(silent=True) or {}).get("force") is True)
    if scrape_control["stop_event"] is not None:
        scrape_control["stop_event"].set()
    if force:
        _scrape_sync, _run_scrapers, request_kill, _clear_kill, _ScraperKilled = scraper_runtime()
        request_kill()
        scrape_status["stopping"] = "kill"
        scrape_status["last"] = "kill_requested"
        return jsonify({"status": "killing"})
    scrape_status["stopping"] = True
    scrape_status["last"] = "stopping_requested"
    return jsonify({"status": "stopping"})

@app.route('/api/scrape/status')
@admin_required
def scrape_status_check():
    if STOREFRONT_ONLY:
        return jsonify({"running": False, "last": "storefront_only"})
    return jsonify(scrape_status)

@app.route('/api/listings')
@admin_required
def listings_json():
    if STOREFRONT_ONLY:
        return jsonify({"status": "disabled", "reason": "storefront_only"}), 403
    with listing_lock:
        listings = load_json(DATA_FILE, [])
    return jsonify({"count": len(listings), "listings": listings})

@app.route('/api/listings.csv')
@admin_required
def listings_csv():
    if STOREFRONT_ONLY:
        return jsonify({"status": "disabled", "reason": "storefront_only"}), 403
    with listing_lock:
        listings = load_json(DATA_FILE, [])
    output = io.StringIO()
    fieldnames = ["id", "address", "status", "price", "bid", "date", "county", "link", "source"]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for item in listings:
        row = {k: item.get(k, "") for k in fieldnames}
        writer.writerow(row)
    csv_data = output.getvalue()
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=listings.csv"},
    )

if __name__ == '__main__':
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8095"))
    print(f"Foreclosure app running at http://{host}:{port}")
    app.run(host=host, port=port, debug=False, use_reloader=False)

