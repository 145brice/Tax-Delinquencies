import os
import re
import json
import hashlib
import secrets
import threading
import time
import csv
import io
import copy
import shutil
import sqlite3
import base64
import smtplib
import urllib.request
from email.message import EmailMessage
from urllib.parse import urlencode
from datetime import datetime, timezone
from functools import wraps
from contextlib import closing
from urllib.parse import urlsplit
import commerce
from authlib.integrations.flask_client import OAuth
from flask import Flask, render_template, request, jsonify, Response, redirect, url_for, session
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
import stripe
import db
from lead_pricing import (BASE_CENTS, FLOOR_CENTS, age_days, discovery_date,
                          normalize_listing_dates, price_cents)

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


# Some legal-notice sources (esp. Michigan foreclosure notices) title records
# "Mortgage Foreclosure Sale-<homeowner>", so the owner field ended up as boiler-
# plate ("Mortgage Foreclosure Sale") that the storefront masked to "M.F.S.".
# Strip that prefix so the real mortgagor name shows.
_OWNER_BOILERPLATE_RE = re.compile(
    r"^\s*(?:Notice of\s+)?(?:Mortgage|Judicial)?\s*Foreclosure\s+(?:Sale|Notice)\b\s*[-–—:]*\s*",
    re.I)
# If what's left after stripping still contains notice-speak, it isn't a name.
_NON_NAME_RE = re.compile(
    r"foreclos|advertisement|\bnotice\b|state journal|county\)|purchasers|default\b", re.I)


def clean_owner_name(owner):
    """Return the real homeowner from an owner field, or "" if it's just
    foreclosure-notice boilerplate with no recoverable name."""
    if not owner:
        return owner
    cleaned = _OWNER_BOILERPLATE_RE.sub("", str(owner)).strip()
    if len(cleaned) < 3 or _NON_NAME_RE.search(cleaned):
        return ""
    return cleaned


def owner_looks_polluted(owner):
    """True if an owner value is foreclosure boilerplate rather than a real name."""
    if not owner:
        return False
    return bool(_OWNER_BOILERPLATE_RE.match(str(owner))) or bool(_NON_NAME_RE.search(str(owner)))


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

        scraped_date = r.get("scraped_date") or datetime.now().strftime('%Y-%m-%d')
        event_date = r.get("sale_date") or ""
        date_str = event_date or scraped_date
        # Parcel/APN is the most reliable unique identifier a county issues —
        # prefer it over the human-parsed address text, which can collide
        # when detail-page scraping mis-extracts the same (or blank) address
        # for two different properties. This mirrors _parcel_date_key(), used
        # later for cross-run merge dedup, so both stages agree on identity.
        #
        # Probate/divorce (and some notice) records carry no street address or
        # parcel, so the address is just "City, ST" — without the case number
        # every record for that city+date would collapse into one listing, and
        # keying on scraped date would mint a new id for the same case each
        # run. Key those on county+case instead.
        case_no = str(r.get("case_number") or "").strip().lower()
        county_key = str(r.get("county") or "").lower().strip()
        if parcel and county_key:
            key_basis = f"{county_key}-parcel-{parcel.lower()}"
        elif not street and not parcel and case_no:
            key_basis = f"{county_key}-case-{case_no}"
        else:
            key_basis = f"{address.lower().strip()}-{date_str}"
        key = hashlib.sha256(f"{state.lower()}|{key_basis}".encode()).hexdigest()[:32]
        if key in seen:
            continue
        seen.add(key)

        amount_str = r.get("amount_owed") or ""
        bid_digits = re.sub(r'[^\d]', '', amount_str)
        bid = int(bid_digits) if bid_digits else 0

        record_type = (r.get("record_type") or "").lower()
        if "tax sale" in record_type or "tax-sale" in record_type:
            status = "Tax Sale"
        elif "delinquent" in record_type or "tax" in record_type:
            status = "Tax Lien"
        elif "notice of default" in record_type or "default" in record_type:
            # Earliest foreclosure stage (mostly CA/AZ/NV non-judicial). Check
            # before the generic "foreclos" branch so NODs get their own bucket.
            status = "Notice of Default"
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
            "owner":        clean_owner_name(r.get("owner_name", "")),
            "parcel_id":    r.get("parcel_id", ""),
            "case_number":  r.get("case_number", ""),
            "status":       status,
            "record_type":  r.get("record_type", ""),
            "price":        5,
            "bid":          bid,
            "amount_owed":  r.get("amount_owed", ""),
            "date":         event_date,
            "sale_date":    event_date,
            "scraped_date": scraped_date,
            "first_seen":   scraped_date,
            "county":       county_raw.lower(),
            "link":         r.get("source_url", ""),
            "source":    county_raw.title() + " Co.",
        })
    return listings

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
oauth = OAuth(app)
google_oauth = oauth.register(
    name="google",
    client_id=os.getenv("GOOGLE_OAUTH_CLIENT_ID", ""),
    client_secret=os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", ""),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)
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

# Central age-based storefront prices. Promotions never alter these prices.
PRICING_PHASE = os.getenv("LEAD_PRICING_PHASE", "beta").strip().lower()
if PRICING_PHASE not in BASE_CENTS:
    raise ValueError("LEAD_PRICING_PHASE must be beta or post_beta")
LEAD_PRICE_RAW, LEAD_PRICE_TRACED = [v / 100 for v in BASE_CENTS[PRICING_PHASE]]


@app.context_processor
def inject_lead_pricing():
    return {"lead_price_raw": LEAD_PRICE_RAW, "lead_price_traced": LEAD_PRICE_TRACED,
            "lead_floor_raw": FLOOR_CENTS[PRICING_PHASE][0] / 100,
            "pricing_phase": PRICING_PHASE}


def _lead_is_traced(item):
    return any(str(item.get(field) or "").strip()
               for field in ("primary_phone", "phone_2", "email_1", "email_2"))


def _lead_price(item):
    return price_cents(item, _lead_is_traced(item), PRICING_PHASE) / 100


def _price_str(amount):
    amount = float(amount)
    return f"{amount:.0f}" if amount.is_integer() else f"{amount:.2f}"


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
    # Bearer-style admin token auth isn't ambient like a session cookie, so a
    # malicious page can't forge it cross-site — CSRF doesn't apply. This lets
    # server-to-server callers (e.g. the scheduled scraper) authenticate
    # without a browser session.
    admin_token = _admin_token()
    supplied_admin_token = request.headers.get("X-Admin-Token") or request.args.get("token") or request.form.get("token")
    if admin_token and supplied_admin_token and secrets.compare_digest(supplied_admin_token, admin_token):
        return None
    expected = session.get("_csrf_token") or ""
    supplied = (request.headers.get("X-CSRF-Token")
                or request.form.get("csrf_token") or "")
    if not expected or not secrets.compare_digest(supplied, expected):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Invalid or missing CSRF token. Refresh the page and try again."}), 403
        return "Invalid or missing CSRF token. Refresh the page and try again.", 403
    return None


# Google Analytics 4 — measurement ID for the Tax Delinquencies stream.
GA_MEASUREMENT_ID = os.environ.get("GA_MEASUREMENT_ID", "G-62TXZTPFMH")
_GA_SNIPPET = (
    '<script async src="https://www.googletagmanager.com/gtag/js?id=%(id)s"></script>'
    '<script>window.dataLayer=window.dataLayer||[];'
    'function gtag(){dataLayer.push(arguments);}'
    "gtag('js',new Date());gtag('config','%(id)s');</script>"
) % {"id": GA_MEASUREMENT_ID}


@app.after_request
def inject_analytics(response):
    """Insert the GA4 gtag snippet into every full HTML page, once, before </head>."""
    if not GA_MEASUREMENT_ID:
        return response
    ctype = (response.content_type or "")
    if "text/html" not in ctype or response.direct_passthrough:
        return response
    try:
        body = response.get_data(as_text=True)
    except (RuntimeError, UnicodeDecodeError):
        return response
    if "</head>" not in body or "googletagmanager.com/gtag" in body:
        return response
    response.set_data(body.replace("</head>", _GA_SNIPPET + "</head>", 1))
    return response


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
SQLITE_DB = os.path.abspath(SQLITE_DB)
# Paid state must never silently fall back to the container's disposable disk.
def _persistent_storage_ready():
    if not IS_HOSTED:
        return True
    volume = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "")
    if not IS_RAILWAY or not volume:
        return False
    root = os.path.realpath(volume)
    try:
        return os.path.commonpath([root, os.path.realpath(SQLITE_DB)]) == root
    except ValueError:
        return False

app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=IS_HOSTED)
STOREFRONT_CSV = os.getenv("STOREFRONT_CSV", os.path.join(DATA_DIR, "storefront_listings.csv"))
STOREFRONT_FIELDS = [
    "id", "status", "county", "state", "city", "zip", "address", "street",
    "owner", "parcel_id", "case_number", "record_type", "price", "bid",
    "amount_owed", "date", "sale_date", "scraped_date", "first_seen", "link", "source",
]

def _sqlite_enabled():
    return not IS_VERCEL

def _sqlite_conn():
    os.makedirs(os.path.dirname(SQLITE_DB) or ".", exist_ok=True)
    conn = sqlite3.connect(SQLITE_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_json (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    return conn

purchase_store = commerce.Store(_sqlite_conn)

def _sqlite_get(key, default):
    try:
        with closing(_sqlite_conn()) as conn:
            row = conn.execute("SELECT value FROM app_json WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default
    except (sqlite3.Error, json.JSONDecodeError):
        app.logger.exception("Could not read durable application state: %s", key)
        raise

def _sqlite_set(key, data):
    with closing(_sqlite_conn()) as conn, conn:
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

# Where runtime-writable files live:
#   local   -> the project dir (persists, git-tracked)
#   Vercel  -> /tmp (only writable path; wiped between invocations)
#   Railway -> the mounted volume, so scrape output + resume progress survive
#              container restarts (an OOM restart does NOT rebuild the image)
if IS_VERCEL:
    _RUNTIME_DIR = "/tmp/foreclosure-app"
elif os.getenv("RAILWAY_VOLUME_MOUNT_PATH"):
    _RUNTIME_DIR = os.path.join(os.getenv("RAILWAY_VOLUME_MOUNT_PATH"), "app-data")
else:
    _RUNTIME_DIR = None


def _runtime_data_file(filename):
    """Runtime-writable path for a data file, seeded from the bundled git copy.

    Seed only when the runtime copy is missing. A deployment must never
    overwrite the live inventory or financial state based on file timestamps.
    """
    if not _RUNTIME_DIR:
        return os.path.join(BASE_DIR, filename)
    os.makedirs(_RUNTIME_DIR, exist_ok=True)
    runtime_file = os.path.join(_RUNTIME_DIR, filename)
    bundled_file = os.path.join(BASE_DIR, filename)
    if os.path.exists(bundled_file) and (
        not os.path.exists(runtime_file)
    ):
        shutil.copyfile(bundled_file, runtime_file)
    return runtime_file

DATA_FILE = _runtime_data_file('listings.json')
SETTINGS_FILE = _runtime_data_file('settings.json')
# Per-source progress of the last scrape run, so an interrupted run can resume.
SCRAPE_PROGRESS_FILE = _runtime_data_file('scrape_progress.json')
SCRAPE_RUN_INVENTORY_KEY = "scrape_run_inventory"
COUNTY_REQUESTS_KEY = "county_interest_requests"
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
    # Sold leads are exclusive: hidden from the storefront but kept in the
    # stored list so re-scrapes merge into them instead of re-listing them.
    if str(item.get("sold_at") or "").strip():
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
    reserved = _promo_reserved_ids() | purchase_store.claimed_ids()
    properties = purchase_store.claimed_properties()
    return sort_storefront_listings([item for item in listings if _is_publishable_listing(item)
                                    and str(item.get("id")) not in reserved
                                    and not commerce.identity_keys(item).intersection(properties)])

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

def _storefront_display_row(item, reveal=False):
    # reveal=True (admin viewing the storefront) shows full address/owner/
    # contact instead of the public masked teasers. Never set for public
    # visitors — masking is the paywall.
    public_item = _backfill_fields(dict(item))
    status = str(public_item.get("status") or public_item.get("record_type") or "").lower()
    source = str(public_item.get("source") or "").lower()
    county = str(public_item.get("county") or "").lower()
    link = str(public_item.get("link") or "").lower()
    source_id = _source_listing_id(public_item)
    parcel_display_address = False

    public_item["owner"] = (_storefront_owner(public_item) if reveal
                            else _mask_public_owner(_storefront_owner(public_item)))
    # Acquisition and source-event dates stay separate. Legacy date migration
    # happens in stored data, not through ambiguous display fallbacks.
    public_item["scraped_date"] = public_item.get("scraped_date") or ""

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
        public_item["sale_date"] = public_item.get("sale_date") or ""
    elif "tax" in status:
        public_item["sale_date"] = public_item.get("sale_date") or ""
        # Only fall back to "Parcel X, Jacksonville" when the record has no real
        # street address. Enriched records (via the Property Appraiser lookup)
        # carry a street and should keep it.
        _has_street = bool(re.match(r"^\s*\d", str(public_item.get("address") or ""))
                           or str(public_item.get("street") or "").strip())
        if (county == "duval" and ("duval" in source or "re_tax" in link)
                and public_item.get("parcel_id") and not _has_street):
            public_item["address"] = f"Parcel {public_item['parcel_id']}, Jacksonville, FL"
            public_item["date"] = DUVAL_TAX_CERTIFICATE_SALE_DATE
            public_item["sale_date"] = DUVAL_TAX_CERTIFICATE_SALE_DATE
            parcel_display_address = True
    elif "foreclosure" in status:
        public_item["sale_date"] = public_item.get("sale_date") or ""

    if public_item.get("address") and not parcel_display_address and not reveal:
        public_item["address"] = obfuscate_address(str(public_item["address"]))
    public_item["street"] = ""

    # Privacy: show only coarse contact teasers. Full contact data never reaches
    # the page source — unless reveal (admin), which shows the real values.
    digits = re.sub(r"\D", "", str(public_item.get("primary_phone") or ""))
    if reveal:
        public_item["phone_prefix"] = digits[-10:][:3] if len(digits) >= 10 else ""
        public_item["phone_display"] = str(public_item.get("primary_phone") or "")
    elif len(digits) >= 10:
        area = digits[-10:][:3]
        public_item["phone_prefix"] = area
        public_item["phone_display"] = f"({area})"
    else:
        public_item["phone_prefix"] = ""
        public_item["phone_display"] = ""
    email = str(public_item.get("email_1") or "").strip()
    if reveal:
        public_item["email_display"] = email
    elif email and "@" in email:
        local_part, domain = email.split("@", 1)
        domain_name, dot, suffix = domain.partition(".")
        local_initial = local_part[:1].lower()
        domain_initial = domain_name[:1].lower()
        suffix_display = f"{dot}{suffix}" if dot and suffix else ""
        public_item["email_display"] = f"{local_initial}****@{domain_initial}****{suffix_display}"
    else:
        public_item["email_display"] = ""
    # Calculate before private contact fields are stripped.
    price_value = _lead_price(item)
    public_item["price"] = price_value
    public_item["price_display"] = _price_str(price_value)
    public_item["is_traced"] = _lead_is_traced(item)
    public_item["age_days"] = age_days(item)
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


def _patch_subscription(sub_id, changes, *, remove=(), create=False):
    with purchase_store.transaction() as conn:
        current = purchase_store.read(conn, "subscriptions", [])
        target = next((s for s in current if s.get("stripe_subscription_id") == sub_id), None)
        if target is None:
            if not create:
                return None
            target = dict(changes)
            current.append(target)
        else:
            # Lifecycle writes must not overwrite concurrent usage/refunds or
            # renewal watermarks, or any other subscription in the store.
            protected = {"traces_used", "period_start", "last_renewal", "created_at", "id"}
            target.update({k: v for k, v in changes.items() if k not in protected})
        for key in remove:
            target.pop(key, None)
        purchase_store.write(conn, "subscriptions", current)
        return dict(target)


# --- Credit wallet (prepaid balance for per-lead unlocks) --------------------
# Paid wallet credits remain separate from signup promo entitlements.
WALLET_FILE = _runtime_data_file('credit_wallets.json')
# Signup allowances are stored separately in signup_promos.
PROMO_LOCK = threading.Lock()
WALLET_LOCK = threading.Lock()


def _load_wallets():
    if _sqlite_enabled():
        return _sqlite_get("credit_wallets", {})
    return load_json(WALLET_FILE, {})


def _save_wallets(wallets):
    if _sqlite_enabled():
        _sqlite_set("credit_wallets", wallets)
    else:
        save_json(WALLET_FILE, wallets)


def _wallet_balance_cents(user_id):
    return int((_load_wallets().get(str(user_id)) or {}).get("balance_cents", 0))


def _wallet_adjust(user_id, delta_cents, reason):
    with purchase_store.transaction() as conn:
        balance, _ = purchase_store.wallet_change(conn, user_id, int(delta_cents), reason)
    return balance


def _wallet_credit_once(user_id, cents, event_id, reason):
    return purchase_store.credit(user_id, max(0, int(cents or 0)), str(event_id))


# Credit packs (pay-as-you-go top-ups). price_cents = charged, credit_cents =
# added to the wallet (higher tiers include bonus credit). Built on the fly at
# checkout, so no Stripe dashboard products are required. Override via env
# CREDIT_PACKS_JSON if you want to retune without a deploy.
def _load_credit_packs():
    raw = os.getenv("CREDIT_PACKS_JSON", "")
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    return [
        {"id": "p25",  "price_cents": 2500,  "credit_cents": 2500,  "label": "$25",  "bonus": 0},
        {"id": "p60",  "price_cents": 6000,  "credit_cents": 7000,  "label": "$60",  "bonus": 1000},
        {"id": "p120", "price_cents": 12000, "credit_cents": 15000, "label": "$120", "bonus": 3000},
    ]


def _credit_pack_sessions():
    if _sqlite_enabled():
        return set(_sqlite_get("credit_pack_fulfilled", []))
    return set(load_json(_runtime_data_file("credit_pack_fulfilled.json"), []))


def _mark_credit_pack_fulfilled(session_id):
    done = _credit_pack_sessions()
    done.add(session_id)
    data = sorted(done)[-1000:]
    if _sqlite_enabled():
        _sqlite_set("credit_pack_fulfilled", data)
    else:
        save_json(_runtime_data_file("credit_pack_fulfilled.json"), data)


def _fulfill_credit_pack(session_id):
    if not session_id or not stripe.api_key or not _persistent_storage_ready():
        return False
    cs = stripe.checkout.Session.retrieve(session_id)
    meta = cs.get("metadata") or {}
    pack = next((p for p in _load_credit_packs()
                 if str(p.get("id")) == str(meta.get("pack_id"))), None)
    try:
        paid_cents = int(cs.get("amount_total"))
        credited_cents = int(meta.get("credit_cents"))
        expected_price = int(pack["price_cents"])
        expected_credit = int(pack["credit_cents"])
    except (TypeError, ValueError, KeyError):
        return False
    if (meta.get("kind") != "credit_pack" or cs.get("mode") != "payment"
            or cs.get("payment_status") != "paid" or cs.get("currency") != "usd"
            or paid_cents != expected_price or credited_cents != expected_credit):
        return False
    uid, cents = meta.get("user_id"), expected_credit
    if not uid or cents <= 0:
        return False
    # Preserve the old fulfillment marker while moving to an atomic ledger.
    with purchase_store.transaction() as conn:
        done = purchase_store.read(conn, "credit_pack_fulfilled", [])
        if session_id not in done:
            purchase_store.wallet_change(conn, uid, cents, "pack:" + session_id)
    return True


SKIPTRACE_FULFILL_LOCK = threading.Lock()
SKIPTRACE_FULFILLING = set()


def _fulfill_order_skiptraces(session_id):
    """Trace only paid order lines that explicitly requested skip tracing."""
    try:
        order = db.get_order_by_session(session_id)
        if not order or order.get("status") != "paid":
            return False
        order_id = order.get("id")
        user_id = order.get("user_id")
        for lead in order.get("leads_json") or []:
            if lead.get("purchase_mode") != "skip":
                continue
            if lead.get("skiptrace_status") in {"completed", "failed_credited"}:
                continue
            lead_id = str(lead.get("id") or "")
            event_id = f"skiptrace-refund:{session_id}:{lead_id}"
            refund_decided = purchase_store.credit_recorded(event_id)
            evidence = lead.get("_purchase_evidence") or {}
            source = evidence.get("lead") if isinstance(evidence.get("lead"), dict) else lead
            phones = [str(source.get(key) or "").strip() for key in ("primary_phone", "phone_2")]
            emails = [str(source.get(key) or "").strip() for key in ("email_1", "email_2")]
            phones = [value for value in phones if value]
            emails = [value for value in emails if value]
            if refund_decided:
                phones, emails = [], []
            query = ""
            error = ""
            if not phones and not emails and not refund_decided:
                try:
                    from scrapers import skiptrace_search
                    city, state = _skiptrace_city_state(source)
                    result = skiptrace_search.lookup(
                        str(source.get("owner") or ""),
                        str(source.get("street") or source.get("address") or ""),
                        city,
                        state,
                        engine="serper",
                        use_cache=False,
                    )
                    phones = list(result.get("phones") or [])
                    emails = list(result.get("emails") or [])
                    query = str(result.get("query") or "")
                    error = str(result.get("error") or "")
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"

            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if phones or emails:
                updated = db.update_order_lead_contacts(order_id, lead_id, {
                    "primary_phone": phones[0] if phones else "",
                    "phone_2": phones[1] if len(phones) > 1 else "",
                    "email_1": emails[0] if emails else "",
                    "email_2": emails[1] if len(emails) > 1 else "",
                    "skiptrace_status": "completed",
                    "skiptrace_source": "Google (Serper)" if query else str(source.get("skiptrace_source") or "Existing verified data"),
                    "skiptrace_notes": query,
                    "skiptraced_at": now,
                })
                if not updated:
                    raise RuntimeError("Could not persist completed skip trace")
                continue

            raw_cents = int(lead.get("raw_price_cents") or evidence.get("raw_price_cents") or 0)
            skip_cents = int(lead.get("skip_price_cents") or evidence.get("skip_price_cents") or 0)
            if error and error != "no usable owner name":
                raise RuntimeError("Skip-trace provider unavailable; will retry")
            credit_cents = int(lead.get("skiptrace_refund_cents", max(0, skip_cents - raw_cents)))
            event_id = f"skiptrace-refund:{session_id}:{lead_id}"
            _wallet_credit_once(
                user_id, credit_cents, event_id,
                f"Skip trace unavailable; raw lead delivered ({lead_id})",
            )
            _restore_included_trace(lead, event_id)
            updated = db.update_order_lead_contacts(order_id, lead_id, {
                "skiptrace_status": "failed_credited",
                "skiptrace_credit_cents": credit_cents,
                "skiptrace_error": error or "No usable phone or email found",
                "skiptraced_at": now,
            })
            if not updated:
                raise RuntimeError("Could not persist failed skip trace")
        return True
    except Exception as exc:
        app.logger.exception("post-payment skip tracing failed for %s: %s", session_id, exc)
        return False
    finally:
        with SKIPTRACE_FULFILL_LOCK:
            SKIPTRACE_FULFILLING.discard(str(session_id))


def _start_order_skiptraces(session_id):
    if session_id:
        purchase_store.add_job("trace:" + str(session_id), "trace")


def _restore_included_trace(lead, event_id):
    included = lead.get("included_trace")
    if not included:
        return
    with purchase_store.transaction() as conn:
        marker = "allowance:" + event_id
        if conn.execute("SELECT 1 FROM commerce_events WHERE id=?", (marker,)).fetchone():
            return
        subs = purchase_store.read(conn, "subscriptions", [])
        for sub in subs:
            if (sub.get("stripe_subscription_id") == included["subscription_id"]
                    and sub.get("period_start", "") == included["period"]):
                sub["traces_used"] = max(0, int(sub.get("traces_used", 0)) - 1)
        purchase_store.write(conn, "subscriptions", subs)
        conn.execute("INSERT INTO commerce_events VALUES (?)", (marker,))


def _renew_subscription_allowance(invoice):
    # Invoice IDs deduplicate delivery; period_start rejects delayed old renewals.
    with purchase_store.transaction() as conn:
        event = "invoice:" + str(invoice["id"])
        if conn.execute("SELECT 1 FROM commerce_events WHERE id=?", (event,)).fetchone():
            return
        subs = purchase_store.read(conn, "subscriptions", [])
        matched = False
        for sub in subs:
            if sub.get("stripe_subscription_id") == invoice["subscription"]:
                matched = True
                period = int(invoice.get("period_start") or 0)
                if invoice.get("billing_reason") == "subscription_cycle" and period > int(sub.get("last_renewal", 0)):
                    sub["traces_used"] = 0
                    sub["period_start"] = datetime.fromtimestamp(period, timezone.utc).isoformat()
                    sub["last_renewal"] = period
        if not matched:
            raise RuntimeError("Subscription has not been activated yet")
        purchase_store.write(conn, "subscriptions", subs)
        conn.execute("INSERT INTO commerce_events VALUES (?)", (event,))


def _subscription_access_status(stripe_status):
    return {
        "active": "active", "trialing": "active", "past_due": "past_due",
        "incomplete": "incomplete", "incomplete_expired": "inactive",
        "unpaid": "inactive", "paused": "paused", "canceled": "canceled",
    }.get(str(stripe_status or "").lower(), "inactive")


def _sync_subscription_object(stripe_sub):
    sub_id = stripe_sub.get("id")
    if not sub_id:
        return False
    metadata = stripe_sub.get("metadata") or {}
    items = (stripe_sub.get("items") or {}).get("data") or []
    price_id = ((items[0].get("price") or {}).get("id") if items else "") or ""
    tier = next((key for key, value in TIER_PRICES.items() if value and value == price_id), None)
    existing = next((s for s in _load_subs() if s.get("stripe_subscription_id") == sub_id), None)
    if not existing:
        # A Stripe account can send subscription events for other products to the
        # same endpoint. Acknowledge those events instead of retrying them forever.
        if metadata.get("kind") != "county_subscription":
            return True
        if not metadata.get("user_id") or not metadata.get("county") or not tier:
            return False
    changes = {"stripe_subscription_id": sub_id,
               "status": _subscription_access_status(stripe_sub.get("status")),
               "current_period_end": _sub_period_end(stripe_sub)}
    if tier:
        changes.update({"tier": tier, "price_id": price_id})
    if not existing:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        changes.update({"id": secrets.token_hex(8), "user_id": str(metadata["user_id"]),
                        "email": str(metadata.get("email") or ""),
                        "county": str(metadata["county"]).lower(),
                        "stripe_customer_id": str(stripe_sub.get("customer") or ""),
                        "created_at": now, "period_start": now, "traces_used": 0})
    remove = ()
    if existing and tier and existing.get("pending_tier") == tier:
        remove = ("pending_tier", "pending_tier_at")
    return _patch_subscription(sub_id, changes, remove=remove, create=not bool(existing)) is not None


def _record_billing_problem(event_id, event_type, obj):
    subscription = obj.get("subscription") or ((obj.get("parent") or {}).get("subscription_details") or {}).get("subscription")
    with purchase_store.transaction() as conn:
        problems = purchase_store.read(conn, "stripe_billing_problems", {})
        problems[str(event_id)] = {"type": event_type, "subscription": str(subscription or ""),
            "invoice": str(obj.get("id") or ""), "customer": str(obj.get("customer") or ""),
            "created": int(obj.get("created") or time.time()), "attempt_count": int(obj.get("attempt_count") or 0)}
        purchase_store.write(conn, "stripe_billing_problems", dict(list(problems.items())[-500:]))
    if subscription:
        try:
            return _sync_subscription_object(stripe.Subscription.retrieve(subscription))
        except stripe.error.StripeError:
            return False
    return True


def _grant_signup_promo(user_id):
    """Register a new account's allowance without adding wallet money."""
    with closing(_sqlite_conn()) as conn, conn:
        conn.execute("CREATE TABLE IF NOT EXISTS signup_promos (user_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        conn.execute("INSERT OR IGNORE INTO signup_promos VALUES (?, ?)",
                     (str(user_id), json.dumps({"remaining": 3})))


def _promo_status(user_id):
    with closing(_sqlite_conn()) as conn, conn:
        conn.execute("CREATE TABLE IF NOT EXISTS signup_promos (user_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        row = conn.execute("SELECT payload FROM signup_promos WHERE user_id=?", (str(user_id),)).fetchone()
    return json.loads(row[0]) if row else {"remaining": 0}


def _promo_reserved_ids():
    with closing(_sqlite_conn()) as conn, conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='signup_promos'").fetchone():
            return set()
        return {lead_id for row in conn.execute("SELECT payload FROM signup_promos")
                for lead_id in json.loads(row[0]).get("reserved_ids", [])}


@app.route('/api/claim-aged-leads', methods=['POST'])
def claim_aged_leads():
    if not _accounts_ready():
        return jsonify({"error": "Accounts and persistent storage must be configured."}), 503
    user = current_user()
    if not user:
        return jsonify({"login_required": True}), 401
    try:
        _ensure_purchase_history()
        state = _promo_status(user["id"])
        key = state.get("pending_key")
        if state.get("pending"):
            order = purchase_store.adopt_promo(user["id"], user["email"], state,
                                              _prepare_purchased_leads(state["pending"]["leads"]))
            key = order["id"]
        elif key:
            order = purchase_store.get(key=key)
        else:
            claimed = _claimed_counties(exclude_user=user["id"]) if _subscriptions_ready() else {}
            available = [it for it in publishable_storefront_listings(current_listings())
                         if (age_days(it) or 0) > 60 and str(it.get("county") or "").lower() not in claimed]
            available.sort(key=lambda it: (_lead_is_traced(it), -age_days(it), str(it["id"])))
            selected = available[:max(0, min(3, state["remaining"]))]
            if not selected:
                return jsonify({"ok": True, "unlocked": 0, "remaining": state["remaining"]})
            key = "aged_" + secrets.token_hex(16)
            def consume(conn, payload):
                row = conn.execute("SELECT payload FROM signup_promos WHERE user_id=?", (str(user["id"]),)).fetchone()
                current = json.loads(row[0]) if row else {"remaining": 0}
                if current.get("pending_key") or current.get("pending") or current["remaining"] < len(selected):
                    raise commerce.Unavailable("Another claim is processing; please retry")
                current["remaining"] -= len(selected)
                current["pending_key"] = key
                conn.execute("UPDATE signup_promos SET payload=? WHERE user_id=?", (json.dumps(current), str(user["id"])))
            order = purchase_store.reserve(key, {"kind": "promo", "user_id": str(user["id"]),
                "email": user["email"], "amount_cents": 0, "leads": _prepare_purchased_leads(selected)}, prepare=consume)
        _deliver_purchase(order)
        with purchase_store.transaction() as conn:
            row = conn.execute("SELECT payload FROM signup_promos WHERE user_id=?", (str(user["id"]),)).fetchone()
            state = json.loads(row[0])
            state.pop("pending_key", None)
            conn.execute("UPDATE signup_promos SET payload=? WHERE user_id=?", (json.dumps(state), str(user["id"])))
        return jsonify({"ok": True, "unlocked": len(order["leads"]), "remaining": state["remaining"]})
    except commerce.Unavailable as exc:
        return jsonify({"error": str(exc)}), 409
    except Exception:
        app.logger.exception("Promo order awaiting recovery")
        return jsonify({"error": "Your claim is saved; please retry shortly."}), 503


# --- Per-user preferences (storefront column order, etc.) --------------------
# Stored in the SQLite app_json table under "prefs:<user_id>", so they live on
# the Railway volume and roam across the user's devices. Falls back to a flat
# file off SQLite. See [[railway-ephemeral-sqlite]].
PREFS_FILE = _runtime_data_file('user_prefs.json')

# Canonical storefront column keys. Saved column orders are validated against
# this set so a stale/bad client payload can't inject unknown columns.
STOREFRONT_COLUMN_KEYS = [
    "select", "type", "amount", "price", "sale_date", "scraped", "county",
    "state", "city", "zip", "address", "owner", "phone", "email",
    "parcel", "case", "src",
]


def _all_prefs():
    if _sqlite_enabled():
        return _sqlite_get("user_prefs", {}) or {}
    return load_json(PREFS_FILE, {}) or {}


def _save_all_prefs(prefs):
    if _sqlite_enabled():
        _sqlite_set("user_prefs", prefs)
    else:
        save_json(PREFS_FILE, prefs)


def _get_user_pref(user_id, key, default=None):
    return _all_prefs().get(str(user_id), {}).get(key, default)


def _set_user_pref(user_id, key, value):
    prefs = _all_prefs()
    prefs.setdefault(str(user_id), {})[key] = value
    _save_all_prefs(prefs)


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
    key = record.get("stripe_subscription_id") or record.get("id")
    return _patch_subscription(key, record, create=True)

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
    # Use the same state-scoped identity as exclusive purchase reservations.
    # Address/case fallback also merges date-only ID changes on re-scrapes.
    keys = commerce.identity_keys(item)
    for prefix in ("parcel:", "case:", "address:"):
        match = next((key for key in keys if key.startswith(prefix)), None)
        if match:
            return match
    return ""

def _upgrade_listing(existing, incoming):
    """Keep existing lead identity, but fill in better data from newer scrapes."""
    dates = [d for d in (discovery_date(existing), discovery_date(incoming)) if d]
    if dates:
        existing["first_seen"] = min(dates).isoformat()
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
        elif key == "owner" and owner_looks_polluted(old) and not owner_looks_polluted(value):
            # Replace stale "Mortgage Foreclosure Sale-..." owners with the real name.
            existing[key] = value
    return existing

def _mark_leads_sold(lead_ids):
    """Stamp sold_at on the given listings so the storefront hides them.

    The rows stay in the stored list (only filtered from display) so future
    re-scrapes of the same parcel merge into the hidden row via
    merge_listings/_upgrade_listing instead of re-adding a fresh public copy.
    Scraped data never carries sold_at, so upgrades can't clear it.
    """
    ids = {str(x) for x in lead_ids if x}
    if not ids:
        return 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with listing_lock:
        listings = load_json(DATA_FILE, [])
        changed = 0
        for item in listings:
            if str(item.get("id")) in ids and not item.get("sold_at"):
                item["sold_at"] = now
                changed += 1
        if changed:
            save_json(DATA_FILE, listings)
    return changed


def source_result(source_key, raw_count, kept_count, new_count, status, note="", seconds=None):
    meta = source_metadata().get(source_key, {})
    return {
        "count": new_count,
        "new": new_count,
        "kept": kept_count,
        "raw": raw_count,
        "duplicates": max(0, kept_count - new_count),
        "status": status,
        "note": note,
        "seconds": round(seconds, 1) if seconds is not None else None,
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
    name_token = name_token or (name_candidates[0] if name_candidates else "")
    location_types = {"township", "village", "city", "borough"}
    location_type = (
        street_tokens[-1]
        if len(street_tokens) > 1 and street_tokens[-1].lower().strip(".") in location_types
        else ""
    )
    if name_token and type_token:
        masked_street = f"{name_token[0].upper()}**** {type_token}"
    elif name_token and location_type:
        masked_street = f"{name_token[0].upper()}**** {location_type}"
    elif name_token:
        masked_street = f"{name_token[0].upper()}****"
    else:
        masked_street = "Location hidden"
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
              'amount_owed','sale_date','scraped_date','first_seen','county','link',
              'bid','record_type','street']:
        if item.get(k) is None:
            item[k] = '' if k != 'bid' else 0
        elif k != 'bid':
            item[k] = _clean_text(item[k])
    return item

def _listing_price_cents(item):
    # Use the same price engine as storefront display and credit unlocks.
    return int(round(_lead_price(item) * 100))


def _purchase_price_cents(item, mode="raw"):
    return price_cents(item, traced=(str(mode).lower() == "skip"), phase=PRICING_PHASE)


def _requested_lead_modes(payload, selected_ids):
    requested = payload.get("lead_modes") if isinstance(payload, dict) else {}
    requested = requested if isinstance(requested, dict) else {}
    return {
        lead_id: ("skip" if str(requested.get(lead_id) or "raw").lower() == "skip" else "raw")
        for lead_id in selected_ids
    }

@app.route('/')
def index():
    listings = publishable_storefront_listings(current_listings())
    user = current_user()
    sub_counties = set()
    if user:
        sub_counties = {str(s.get("county") or "").strip().lower()
                        for s in _user_subs(user["id"])}
    # Admins viewing the storefront see full address/owner/contact; the public
    # sees masked teasers (the paywall). Everything else about the page is
    # identical.
    reveal = admin_allowed()

    # Rows are rendered client-side from this compact JSON, not as 15k+ server
    # rendered <tr> (which ballooned the storefront HTML to ~30 MB and stalled
    # the browser). Only the fields the row template needs are serialized.
    leads_json = []
    for item in listings:
        masked_item = _storefront_display_row(item, reveal=reveal)
        if reveal:
            display_address = masked_item.get("address") or item.get('address', '')
        else:
            display_address = masked_item.get("address") or obfuscate_address(item.get('address', ''))
        subscriber_county = str(item.get("county") or "").strip().lower() in sub_counties
        raw_price = _purchase_price_cents(item, "raw") / 100
        skip_price = _purchase_price_cents(item, "skip") / 100
        leads_json.append({
            "id": masked_item.get("id"),
            "status": masked_item.get("status") or "",
            "amount_owed": masked_item.get("amount_owed") or "",
            "bid": masked_item.get("bid") or 0,
            "price": masked_item.get("price") or 0,
            "price_display": masked_item.get("price_display") or "",
            "raw_price": raw_price,
            "raw_price_display": _price_str(raw_price),
            "skip_price": skip_price,
            "skip_price_display": _price_str(skip_price),
            "age_days": masked_item.get("age_days"),
            "sale_date": masked_item.get("sale_date") or "",
            "scraped_date": masked_item.get("scraped_date") or "",
            "county": masked_item.get("county") or "",
            "state": masked_item.get("state") or "",
            "city": masked_item.get("city") or "",
            "zip": masked_item.get("zip") or "",
            "address": display_address,
            "owner": masked_item.get("owner") or "",
            "phone_display": masked_item.get("phone_display") or "",
            "email_display": masked_item.get("email_display") or "",
            "parcel_id": masked_item.get("parcel_id") or "",
            "case_number": masked_item.get("case_number") or "",
            "link": masked_item.get("link") or "",
            "sub": subscriber_county,
        })
    column_order = None
    if user:
        saved = _get_user_pref(user["id"], "column_order", None)
        if isinstance(saved, list):
            column_order = [c for c in saved if c in STOREFRONT_COLUMN_KEYS]
    return render_template('index.html', leads_json=leads_json,
                           has_listings=bool(leads_json),
                           storefront_only=STOREFRONT_ONLY,
                           sub_counties=list(sub_counties),
                           column_order=column_order,
                           user_authed=bool(user),
                           current_user_email=(user.get("email", "") if user else ""),
                           admin_reveal=reveal)

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
        "appwrite_pending": not database_configured,
        "database_configured": database_configured,
        "secret_key_set": secret_key_set,
        "stripe_configured": stripe_configured,
        "google_oauth_ready": _google_oauth_ready(),
        "appwrite_endpoint_set": appwrite_endpoint_set,
        "appwrite_project_set": appwrite_project_set,
        "appwrite_key_set": appwrite_key_set,
        "appwrite_configured": appwrite_configured,
        "admin_allowed": is_admin,
    }


@app.route('/healthz')
def healthz():
    ready = _persistent_storage_ready()
    return jsonify({"ok": ready, "persistent_storage": ready}), (200 if ready else 503)


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
        "serper_configured": bool((os.getenv("SERPER_API_KEY", "") or "").strip()),
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
    token = _admin_token()
    proof = hashlib.sha256(token.encode()).hexdigest() if token else ""
    if token:
        supplied = request.headers.get("X-Admin-Token") or request.args.get("token") or request.form.get("token")
        if supplied and secrets.compare_digest(supplied, token):
            if app.secret_key:
                session["admin_token_proof"] = proof
            return True
        if app.secret_key and secrets.compare_digest(session.get("admin_token_proof", ""), proof):
            return True
    # Email ownership is not verified by this app. Grant roles only to
    # explicitly provisioned, immutable account IDs, never an entered email.
    user = current_user()
    allowed = {uid.strip() for uid in os.getenv("ADMIN_USER_IDS", "").split(",") if uid.strip()}
    return bool(user and str(user["id"]) in allowed)


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
    return db.is_configured() and bool(app.secret_key) and _persistent_storage_ready()


def _safe_local_next(value, default=None):
    target = str(value or "")
    if not target.startswith('/') or target.startswith('//') or '\\' in target or urlsplit(target).netloc:
        return default or url_for('account')
    return target


def _google_oauth_ready():
    return bool(os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip()
                and os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
                and _accounts_ready() and db.backend_name() == "sqlite")


@app.route('/register', methods=['GET', 'POST'])
def register():
    if not _accounts_ready():
        return render_template('auth.html', mode='register',
                               error="Accounts are not configured yet. Configure persistent storage and SECRET_KEY.")
    if current_user():
        return redirect(url_for('account'))
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip().lower()
        password = request.form.get('password') or ''
        if not email or '@' not in email:
            return render_template('auth.html', mode='register', error="Enter a valid email.", email=email)
        if len(password) < 8:
            return render_template('auth.html', mode='register', error="Password must be at least 8 characters.", email=email)
        try:
            db.init_db()
            user = db.create_user(
                email,
                generate_password_hash(password),
            )
        except Exception:
            # e.g. Appwrite project paused for inactivity — don't 500 on the user
            app.logger.exception("Account backend unavailable during register")
            return render_template('auth.html', mode='register', email=email,
                                   error="Account service is temporarily unavailable. Please try again in a few minutes.")
        if not user:
            return render_template('auth.html', mode='register', error="That email is already registered. Try logging in.", email=email)
        try:
            _grant_signup_promo(user['id'])   # separate aged-lead allowance
        except Exception:
            app.logger.exception("Could not register signup promo")
        session.clear()
        session['user_id'] = user['id']
        return redirect(url_for('account'))
    return render_template('auth.html', mode='register')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if not _accounts_ready():
        return render_template('auth.html', mode='login',
                               error="Accounts are not configured yet. Configure persistent storage and SECRET_KEY.")
    if current_user():
        return redirect(url_for('account'))
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip().lower()
        password = request.form.get('password') or ''
        try:
            db.init_db()
            user = db.get_user_by_email(email)
        except Exception:
            # e.g. Appwrite project paused for inactivity — don't 500 on the user
            app.logger.exception("Account backend unavailable during login")
            return render_template('auth.html', mode='login', email=email,
                                   error="Account service is temporarily unavailable. Please try again in a few minutes.")
        if not user or not check_password_hash(user['password_hash'], password):
            return render_template('auth.html', mode='login', error="Incorrect email or password.", email=email)
        session.clear()
        session['user_id'] = user['id']
        return redirect(_safe_local_next(request.args.get('next')))
    return render_template('auth.html', mode='login')


@app.route('/auth/google')
def google_login():
    if not _google_oauth_ready():
        return render_template('auth.html', mode='login',
                               error="Google sign-in is not configured yet."), 503
    if current_user():
        return redirect(url_for('account'))
    session['oauth_next'] = _safe_local_next(request.args.get('next'))
    nonce = secrets.token_urlsafe(24)
    session['google_oauth_nonce'] = nonce
    redirect_uri = os.getenv("GOOGLE_OAUTH_REDIRECT_URI", "").strip() or url_for('google_callback', _external=True)
    return google_oauth.authorize_redirect(redirect_uri, nonce=nonce, prompt="select_account")


@app.route('/auth/google/callback')
def google_callback():
    if not _google_oauth_ready():
        return render_template('auth.html', mode='login',
                               error="Google sign-in is not configured yet."), 503
    next_url = _safe_local_next(session.pop('oauth_next', None))
    try:
        token = google_oauth.authorize_access_token()
        identity = token.get('userinfo') or {}
    except Exception:
        app.logger.exception("Google sign-in failed")
        session.pop('google_oauth_nonce', None)
        return render_template('auth.html', mode='login',
                               error="Google sign-in could not be completed. Please try again."), 400
    verified = identity.get('email_verified')
    subject = str(identity.get('sub') or '').strip()
    email = str(identity.get('email') or '').strip().lower()
    if verified not in (True, "true", "True", 1) or not subject or not email:
        session.pop('google_oauth_nonce', None)
        return render_template('auth.html', mode='login',
                               error="Google did not return a verified email address."), 400
    try:
        user, created = db.get_or_create_oauth_user('google', subject, email)
    except Exception:
        app.logger.exception("Could not persist Google identity")
        return render_template('auth.html', mode='login',
                               error="Google sign-in could not be completed. Please try again."), 503
    session.clear()
    session['user_id'] = user['id']
    if created:
        try:
            _grant_signup_promo(user['id'])
        except Exception:
            app.logger.exception("Could not register Google signup promo")
    return redirect(next_url)


@app.route('/logout', methods=['POST'])
def logout():
    if app.secret_key:
        session.clear()
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
    for order in orders:
        if any(lead.get("purchase_mode") == "skip" and lead.get("skiptrace_status") == "pending"
               for lead in order.get("leads_json") or []):
            _start_order_skiptraces(order.get("stripe_session_id"))
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
    wallet = _load_wallets().get(str(user["id"])) or {}
    wallet_ledger = list(reversed(wallet.get("ledger", [])))[:25]
    return render_template('account.html', email=user['email'], orders=orders,
                           folders=sorted(folders), statuses=statuses,
                           active_subs=active_subs,
                           tier_labels=TIER_LABELS, tier_amounts=TIER_AMOUNTS,
                           tier_included=TIER_INCLUDED,
                           subscriptions_ready=_subscriptions_ready(),
                           wallet_balance_cents=int(wallet.get("balance_cents", 0)),
                           wallet_ledger=wallet_ledger,
                           credit_packs=_load_credit_packs(),
                           payments_ready=bool(stripe.api_key))


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


def _prepare_purchased_leads(leads, modes=None):
    prepared = []
    now = _utc_now_iso()
    modes = modes or {}
    for lead in leads:
        # Preserve the exact, unmasked source record and calculated checkout
        # price as immutable purchase evidence. Buyer workflow fields remain at
        # the top level and can change without rewriting this complaint record.
        source_record = copy.deepcopy(dict(lead))
        item = copy.deepcopy(source_record)
        lead_id = str(source_record.get("id") or "")
        mode = "skip" if str(modes.get(lead_id) or "raw").lower() == "skip" else "raw"
        raw_cents = _purchase_price_cents(source_record, "raw")
        skip_cents = _purchase_price_cents(source_record, "skip")
        item["_purchase_evidence"] = {
            "schema_version": 1,
            "captured_at": now,
            "mode": mode,
            "price_cents": skip_cents if mode == "skip" else raw_cents,
            "raw_price_cents": raw_cents,
            "skip_price_cents": skip_cents,
            "lead": source_record,
        }
        item["purchase_mode"] = mode
        item["purchase_price_cents"] = skip_cents if mode == "skip" else raw_cents
        item["raw_price_cents"] = raw_cents
        item["skip_price_cents"] = skip_cents
        # Contacts are delivered only for the paid mode. Skip requests start
        # pending and are populated after Stripe confirms payment.
        for field in ("primary_phone", "phone_2", "email_1", "email_2",
                      "mailing_address", "skiptrace_notes", "skiptrace_source", "skiptraced_at"):
            item[field] = ""
        item["skiptrace_status"] = "pending" if mode == "skip" else "raw"
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
    if not stripe.api_key or not _accounts_ready():
        return jsonify({"error": "Payments require account configuration and persistent storage."}), 503
    user = current_user()
    if not user:
        return jsonify({"login_required": True}), 401
    try:
        payload, selected, modes, county = _purchase_selection(user)
        key = "checkout_" + secrets.token_hex(16)
        amount = sum(_purchase_price_cents(it, modes[str(it["id"])]) for it in selected)
        apply_wallet = payload.get("apply_wallet") is True
        origin = request.host_url.rstrip("/")
        checkout_args = {
            "mode": "payment", "payment_method_types": ["card"], "customer_email": user["email"],
            "line_items": [{"price_data": {"currency": "usd", "product_data": {
                "name": f"{county.title()} exclusive lead order"}, "unit_amount": amount}, "quantity": 1}],
            "metadata": {"user_id": str(user["id"]), "purchase_id": key, "county": county,
                         "order_total_cents": str(amount)},
            "expires_at": int(time.time()) + 3600,
            "success_url": f"{origin}/checkout/success?session_id={{CHECKOUT_SESSION_ID}}",
            "cancel_url": f"{origin}/checkout/cancel",
        }
        def reserve_wallet_credit(conn, data):
            if not apply_wallet:
                return
            wallet = purchase_store.read(conn, "credit_wallets", {}).get(str(user["id"]), {})
            available = max(0, int(wallet.get("balance_cents") or 0))
            if available >= amount:
                raise commerce.Unavailable("Your credit balance covers this order. Use Unlock with credits.")
            # Stripe requires a non-zero card payment. Fifty cents is its USD
            # minimum; whole-dollar balances normally leave at least $1.
            applied = min(available, max(0, amount - 50))
            if not applied:
                return
            data["order_total_cents"] = amount
            data["wallet_applied_cents"] = applied
            data["amount_cents"] = amount - applied
            data["checkout_args"]["line_items"][0]["price_data"]["unit_amount"] = amount - applied
            data["checkout_args"]["metadata"]["wallet_applied_cents"] = str(applied)
            purchase_store.wallet_change(conn, user["id"], -applied, "split-purchase:" + key)

        order = purchase_store.reserve(key, {"kind": "stripe", "user_id": str(user["id"]),
            "email": user["email"], "amount_cents": amount,
            "leads": _prepare_purchased_leads(selected, modes), "checkout_args": checkout_args},
            prepare=reserve_wallet_credit)
        cs = _create_reserved_checkout(order)
        return jsonify({"url": cs.url, "wallet_applied_cents": order.get("wallet_applied_cents", 0),
                        "card_amount_cents": order["amount_cents"]})
    except commerce.Unavailable as exc:
        return jsonify({"error": str(exc)}), 409
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        app.logger.exception("Could not create checkout; journal will retry")
        return jsonify({"error": "Checkout could not finish. Your reservation will be recovered automatically."}), 503


def _user_active_sub_counties(user_id):
    """Counties where this user has an active monthly lead plan."""
    return {
        str(s.get("county") or "").strip().lower()
        for s in _load_subs()
        if str(s.get("user_id")) == str(user_id) and s.get("status") == "active"
    }


@app.route('/api/wallet')
def wallet_status():
    user = current_user()
    if not user:
        return jsonify({"login_required": True}), 401
    return jsonify({
        "balance_cents": _wallet_balance_cents(user["id"]),
        "aged_promo_remaining": _promo_status(user["id"])["remaining"],
        "aged_promo_pending": bool(_promo_status(user["id"]).get("pending") or _promo_status(user["id"]).get("pending_key")),
        "price_traced_cents": int(round(LEAD_PRICE_TRACED * 100)),
        "price_raw_cents": int(round(LEAD_PRICE_RAW * 100)),
    })


@app.route('/api/unlock', methods=['POST'])
def unlock_with_credits():
    if not _accounts_ready():
        return jsonify({"error": "Accounts and persistent storage must be configured."}), 503
    user = current_user()
    if not user:
        return jsonify({"login_required": True}), 401
    try:
        return jsonify(_wallet_purchase(user, request.get_json(silent=True) or {}))
    except commerce.InsufficientCredit:
        return jsonify({"error": "Not enough credit.", "balance_cents": _wallet_balance_cents(user["id"])}), 402
    except commerce.Unavailable as exc:
        return jsonify({"error": str(exc)}), 409
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        app.logger.exception("Wallet order awaiting recovery")
        return jsonify({"error": "Your order is saved and delivery is being retried. You will not be charged twice."}), 503


@app.route('/api/unlock/quote', methods=['POST'])
def unlock_quote():
    if not _accounts_ready():
        return jsonify({"error": "Purchases are temporarily unavailable."}), 503
    user = current_user()
    if not user:
        return jsonify({"login_required": True}), 401
    try:
        return jsonify(purchase_runtime.quote(sys.modules[__name__], user, request.get_json(silent=True) or {}))
    except commerce.Unavailable as exc:
        return jsonify({"error": str(exc)}), 409
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route('/api/credit-packs')
def credit_packs():
    return jsonify({"packs": _load_credit_packs()})


@app.route('/api/buy-credits', methods=['POST'])
def buy_credits():
    """Start a Stripe checkout to top up the wallet. Built on the fly (no Stripe
    dashboard products needed). Credit is granted on fulfillment."""
    if not stripe.api_key or not _accounts_ready():
        return jsonify({"error": "Payments are not configured yet."}), 503
    user = current_user()
    if not user:
        return jsonify({"login_required": True, "error": "Please log in to buy credits."}), 401

    pack_id = str((request.get_json(silent=True) or {}).get("pack_id") or "")
    pack = next((p for p in _load_credit_packs() if p["id"] == pack_id), None)
    if not pack:
        return jsonify({"error": "Unknown credit pack."}), 400

    # Spell out the bonus on the Stripe checkout line item, so the buyer sees
    # exactly how much credit (and free credit) they get before paying.
    credit_dollars = int(pack["credit_cents"]) // 100
    bonus_cents = int(pack.get("bonus") or 0)
    pack_name = f"Lead credits — ${credit_dollars} wallet credit"
    if bonus_cents:
        pack_name += f" (includes ${bonus_cents // 100} bonus free)"

    origin = request.host_url.rstrip("/")
    try:
        cs = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            customer_email=user["email"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": pack_name},
                    "unit_amount": int(pack["price_cents"]),
                },
                "quantity": 1,
            }],
            metadata={
                "kind": "credit_pack",
                "user_id": str(user["id"]),
                "credit_cents": str(int(pack["credit_cents"])),
                "price_cents": str(int(pack["price_cents"])),
                "pack_id": pack["id"],
            },
            success_url=f"{origin}/checkout/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{origin}/checkout/cancel",
        )
    except stripe.error.StripeError as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({"url": cs.url})


def _fulfill_session(session_id):
    if not session_id or not stripe.api_key or not _persistent_storage_ready():
        return False
    cs = stripe.checkout.Session.retrieve(session_id)
    if cs.get("mode") != "payment" or (cs.get("metadata") or {}).get("kind") == "credit_pack":
        return False
    if cs.get("payment_status") != "paid":
        return False
    order = purchase_store.get(session_id=session_id)
    if not order:
        key = (cs.get("metadata") or {}).get("purchase_id")
        order = purchase_store.get(key=key) if key else None
        if order:
            purchase_store.bind(key, session_id)
        else:
            # Checkout sessions created before the journal was introduced.
            old = db.get_order_by_session(session_id)
            if not old:
                return False
            if old.get("status") == "refunded":
                return True
            if old.get("status") == "paid":
                _mark_leads_sold([it["id"] for it in old["leads_json"]])
                _start_order_skiptraces(session_id)
                return True
            try:
                order = purchase_store.reserve(session_id, {"kind": "stripe", "user_id": str(old["user_id"]),
                    "email": old["email"], "amount_cents": old["amount_cents"], "leads": old["leads_json"]})
            except commerce.Unavailable:
                # A pre-upgrade checkout had no reservation. Never deliver a
                # second copy if its leads were purchased in the meantime.
                refund = stripe.Refund.create(payment_intent=cs["payment_intent"],
                    idempotency_key="exclusive-refund:" + session_id)
                if refund.get("status") not in {"pending", "succeeded"}:
                    raise RuntimeError("Inventory-conflict refund not accepted")
                return db.set_order_status(session_id, "refunded")
            purchase_store.bind(order["id"], session_id)
    if int(cs.get("amount_total", -1)) != int(order["amount_cents"]) or cs.get("currency") != "usd":
        raise ValueError("Payment does not match reserved order")
    purchase_store.paid(order["id"])
    return _deliver_purchase(purchase_store.get(key=order["id"]))


@app.route('/checkout/success')
def checkout_success():
    sid = request.args.get('session_id')
    success = False
    try:
        if sid and stripe.api_key:
            cs = stripe.checkout.Session.retrieve(sid)
            success = (_fulfill_credit_pack(sid) if (cs.get("metadata") or {}).get("kind") == "credit_pack"
                       else _fulfill_session(sid))
    except Exception:
        app.logger.exception("Return-page fulfillment pending")
    if success and sid and (cs.get("metadata") or {}).get("kind") != "credit_pack":
        order = db.get_order_by_session(sid)
        if order and order.get("status") == "refunded":
            return render_template('checkout_status.html', title="Payment refunded",
                message="These exclusive leads were no longer available. Your payment has been refunded to your original payment method.")
    if success and current_user():
        return redirect(url_for('account'))
    return render_template('checkout_status.html', title="Payment complete" if success else "Payment processing",
        message="Log in to view your purchased leads." if success else "We are confirming your payment and preparing your order. Please check your account shortly."), (200 if success else 202)


@app.route('/webhook/stripe', methods=['POST'])
def stripe_webhook():
    try:
        if not _persistent_storage_ready():
            return ("persistent storage unavailable", 503)
        secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")
        if not secret:
            return ("webhook not configured", 503)
        payload = request.get_data()
        sig = request.headers.get('Stripe-Signature', '')
        try:
            event = stripe.Webhook.construct_event(payload, sig, secret)
        except (ValueError, stripe.error.SignatureVerificationError):
            return ("invalid", 400)
        etype = event.get("type")
        event_id = event.get("id") or secrets.token_hex(8)
        obj = event.get("data", {}).get("object", {}) or {}
        if etype in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}:
            if obj.get("mode") == "subscription":
                if not _fulfill_subscription(obj.get("id")):
                    return ("fulfillment pending", 503)
            elif (obj.get("metadata") or {}).get("kind") == "credit_pack":
                if not _fulfill_credit_pack(obj.get("id")):
                    return ("fulfillment pending", 503)
            else:
                if not _fulfill_session(obj.get("id")):
                    return ("fulfillment pending", 503)
        elif etype == "customer.subscription.deleted":
            if not _sync_subscription_object(obj):
                return ("subscription synchronization pending", 503)
        elif etype == "customer.subscription.updated":
            obj = stripe.Subscription.retrieve(obj["id"])
            if not _sync_subscription_object(obj):
                return ("subscription synchronization pending", 503)
        elif etype == "checkout.session.expired":
            order = purchase_store.get(session_id=obj.get("id"))
            if order:
                cs = stripe.checkout.Session.retrieve(obj["id"])
                if cs.get("status") == "expired":
                    purchase_store.expire(order["id"])
        elif etype == "checkout.session.async_payment_failed":
            order = purchase_store.get(session_id=obj.get("id"))
            if order:
                cs = stripe.checkout.Session.retrieve(obj["id"])
                if cs.get("payment_status") == "unpaid":
                    purchase_store.expire(order["id"])
        elif etype in {"customer.subscription.created", "customer.subscription.paused",
                       "customer.subscription.resumed"}:
            authoritative = stripe.Subscription.retrieve(obj["id"])
            if not _sync_subscription_object(authoritative):
                return ("subscription synchronization pending", 503)
        elif etype == "invoice.paid":
            subscription = obj.get("subscription") or ((obj.get("parent") or {}).get("subscription_details") or {}).get("subscription")
            if subscription:
                known = any(s.get("stripe_subscription_id") == subscription for s in _load_subs())
                if not known:
                    if not _sync_subscription_object(stripe.Subscription.retrieve(subscription)):
                        return ("subscription synchronization pending", 503)
                    known = any(s.get("stripe_subscription_id") == subscription for s in _load_subs())
                if known:
                    _renew_subscription_allowance({**obj, "subscription": subscription})
        elif etype in {"invoice.payment_failed", "invoice.payment_action_required",
                       "invoice.finalization_failed"}:
            if not _record_billing_problem(event_id, etype, obj):
                return ("billing status synchronization pending", 503)
        return ("ok", 200)
    except Exception:
        app.logger.exception("Stripe webhook fulfillment failed")
        return ("fulfillment pending", 503)

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
        return False
    if s.get("status") != "active":
        return False
    # Infer tier from price_id if not explicitly passed
    tier = next((k for k, v in TIER_PRICES.items() if v and v == price_id), None)
    if not tier:
        return False
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
    return _patch_subscription(sub_id, {"status": status}) is not None


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


def record_trace_use(user_id, county):
    raise RuntimeError("Trace allowances must be consumed with an exclusive purchase")


@app.route('/api/subscriber/reveal', methods=['POST'])
@login_required
def subscriber_reveal():
    user = current_user()
    payload = request.get_json(silent=True) or {}
    lead_id = str(payload.get("lead_id") or "").strip()
    if not lead_id:
        return jsonify({"error": "lead_id required"}), 400
    # Repeated reveals return the owned snapshot without consuming another slot.
    for order in db.get_paid_orders_for_user(user["id"]):
        for lead in order.get("leads_json") or []:
            if str(lead.get("id")) == lead_id and lead.get("purchase_mode") == "skip":
                return jsonify({"phone": lead.get("primary_phone", ""), "email": lead.get("email_1", ""),
                                "pending": lead.get("skiptrace_status") == "pending"})
    lead = next((it for it in current_listings() if str(it.get("id")) == lead_id), None)
    if not lead:
        return jsonify({"error": "Lead not found"}), 404
    if str(lead.get("county") or "").lower() not in _user_active_sub_counties(user["id"]):
        return jsonify({"error": "No active county subscription"}), 403
    if not _lead_is_traced(lead):
        return jsonify({"phone": "", "email": "", "no_contact": True, "overage": False})
    try:
        result = _wallet_purchase(user, {"lead_ids": [lead_id], "lead_modes": {lead_id: "skip"},
                                          "max_charge_cents": payload.get("max_charge_cents", 0)})
        return jsonify({"phone": lead.get("primary_phone", ""), "email": lead.get("email_1", ""),
                        "overage": result["charged_cents"] > 0, "charged_cents": result["charged_cents"]})
    except commerce.InsufficientCredit:
        return jsonify({"error": "Included traces used. Add wallet credit for the displayed skip-trace upgrade."}), 402
    except commerce.Unavailable as exc:
        return jsonify({"error": str(exc)}), 409


@app.route('/api/subscriber/change-tier', methods=['POST'])
@login_required
def subscriber_change_tier():
    return jsonify({"error": "County plans are retired. Plan changes are no longer available."}), 410


@app.route('/api/prefs/columns', methods=['POST'])
@login_required
def save_column_prefs():
    """Persist the user's storefront column order (account-level, roams across
    devices). Validated against STOREFRONT_COLUMN_KEYS."""
    user = current_user()
    payload = request.get_json(silent=True) or {}
    order = payload.get("order")
    if not isinstance(order, list):
        return jsonify({"error": "order must be a list"}), 400
    # Keep only known keys, drop dupes, preserve client order.
    seen, clean = set(), []
    for c in order:
        if isinstance(c, str) and c in STOREFRONT_COLUMN_KEYS and c not in seen:
            seen.add(c)
            clean.append(c)
    _set_user_pref(user["id"], "column_order", clean)
    return jsonify({"ok": True})


@app.route('/subscribe', methods=['POST'])
def subscribe():
    return jsonify({"error": "County plans are no longer offered. Buy individual leads or credit packs."}), 410


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
    """Diagnostic for the configured account store."""
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
        "backend": db.backend_name(),
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
        "backend": db.backend_name(),
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
    path = _safe_csv_path(filename)
    if not path:
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
    return render_template('pricing.html', county_count=county_count,
                           credit_packs=_load_credit_packs())


# ── County schedule ──────────────────────────────────────────────────────────

_COUNTY_SCHEDULE = [
    # Florida — Daily (Jax Daily Record posts every business day)
    {"ui_key":"duval-fl",    "label":"Duval (Jacksonville)", "state":"FL", "region":"Florida (Northeast)", "frequency":"Daily",    "freq_note":"Jax Daily Record, weekdays",         "listing_names":["duval"],       "source_keys":["duval_jaxdailyrecord","duval_jaxdailyrecord_retax"]},
    {"ui_key":"clay-fl",     "label":"Clay",                 "state":"FL", "region":"Florida (Northeast)", "frequency":"Daily",    "freq_note":"Jax Daily Record, weekdays",         "listing_names":["clay"],        "source_keys":["clay_jaxdailyrecord"]},
    {"ui_key":"nassau-fl",   "label":"Nassau",               "state":"FL", "region":"Florida (Northeast)", "frequency":"Daily",    "freq_note":"Jax Daily Record, weekdays",         "listing_names":["nassau"],      "source_keys":["nassau_jaxdailyrecord"]},
    {"ui_key":"stjohns-fl",  "label":"St. Johns",            "state":"FL", "region":"Florida (Northeast)", "frequency":"Daily",    "freq_note":"Jax Daily Record, weekdays",         "listing_names":["st. johns"],   "source_keys":["stjohns_jaxdailyrecord"]},
    {"ui_key":"broward-fl",      "label":"Broward (Fort Lauderdale)", "state":"FL", "region":"Florida (South)", "frequency":"Daily", "freq_note":"Statewide public notices", "listing_names":["broward"], "source_keys":["broward_publicnotices"]},
    {"ui_key":"miamidade-fl",    "label":"Miami-Dade",                "state":"FL", "region":"Florida (South)", "frequency":"Daily", "freq_note":"Statewide public notices", "listing_names":["miami-dade"], "source_keys":["miamidade_publicnotices"]},
    {"ui_key":"palmbeach-fl",    "label":"Palm Beach",                "state":"FL", "region":"Florida (South)", "frequency":"Daily", "freq_note":"Statewide public notices", "listing_names":["palm beach"], "source_keys":["palmbeach_publicnotices"]},
    {"ui_key":"orange-fl",       "label":"Orange (Orlando)",          "state":"FL", "region":"Florida (Central)", "frequency":"Daily", "freq_note":"Statewide public notices", "listing_names":["orange"], "source_keys":["orangefl_publicnotices"]},
    {"ui_key":"hillsborough-fl", "label":"Hillsborough (Tampa)",      "state":"FL", "region":"Florida (Tampa Bay)", "frequency":"Daily", "freq_note":"Statewide public notices", "listing_names":["hillsborough"], "source_keys":["hillsborough_publicnotices"]},
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
    {"ui_key":"monroe-mi",      "label":"Monroe",                "state":"MI", "region":"Michigan", "frequency":"Weekly", "freq_note":"~68 leads/90d",  "listing_names":["monroe"],     "source_keys":["monroe_legalnotices"]},
    {"ui_key":"lenawee-mi",     "label":"Lenawee (Adrian)",      "state":"MI", "region":"Michigan", "frequency":"Weekly", "freq_note":"~65 leads/90d",  "listing_names":["lenawee"],    "source_keys":["lenawee_legalnotices"]},
    {"ui_key":"hillsdale-mi",   "label":"Hillsdale",             "state":"MI", "region":"Michigan", "frequency":"Weekly", "freq_note":"~60 leads/90d",  "listing_names":["hillsdale"],  "source_keys":["hillsdale_legalnotices"]},
    {"ui_key":"lapeer-mi",      "label":"Lapeer",                "state":"MI", "region":"Michigan", "frequency":"Weekly", "freq_note":"~45 leads/90d",  "listing_names":["lapeer"],     "source_keys":["lapeer_legalnotices"]},
    {"ui_key":"eaton-mi",       "label":"Eaton (Charlotte)",     "state":"MI", "region":"Michigan", "frequency":"Weekly", "freq_note":"~43 leads/90d",  "listing_names":["eaton"],      "source_keys":["eaton_legalnotices"]},
    {"ui_key":"bay-mi",         "label":"Bay (Bay City)",        "state":"MI", "region":"Michigan", "frequency":"Weekly", "freq_note":"~34 leads/90d",  "listing_names":["bay"],        "source_keys":["bay_legalnotices"]},
    {"ui_key":"montcalm-mi",    "label":"Montcalm (Stanton)",    "state":"MI", "region":"Michigan", "frequency":"Weekly", "freq_note":"~34 leads/90d",  "listing_names":["montcalm"],   "source_keys":["montcalm_legalnotices"]},
    {"ui_key":"tuscola-mi",     "label":"Tuscola (Caro)",        "state":"MI", "region":"Michigan", "frequency":"Weekly", "freq_note":"~33 leads/90d",  "listing_names":["tuscola"],    "source_keys":["tuscola_legalnotices"]},
    {"ui_key":"allegan-mi",     "label":"Allegan",               "state":"MI", "region":"Michigan", "frequency":"Weekly", "freq_note":"~26 leads/90d",  "listing_names":["allegan"],    "source_keys":["allegan_legalnotices"]},
    {"ui_key":"cass-mi",        "label":"Cass (Cassopolis)",     "state":"MI", "region":"Michigan", "frequency":"Weekly", "freq_note":"~23 leads/90d",  "listing_names":["cass"],       "source_keys":["cass_legalnotices"]},
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
    {"ui_key":"collin-tx",      "label":"Collin (Plano / Frisco)","state":"TX", "region":"Texas", "frequency":"Daily", "freq_note":"County foreclosure notice portal", "listing_names":["collin"], "source_keys":["collin_foreclosures"]},
    {"ui_key":"bexar-tx",       "label":"Bexar (San Antonio)",   "state":"TX", "region":"Texas", "frequency":"Monthly", "freq_note":"Mortgage + tax foreclosure map", "listing_names":["bexar"], "source_keys":["bexar_foreclosures"]},
    # Nevada — Weekly
    {"ui_key":"clark-nv",       "label":"Clark (Las Vegas)",     "state":"NV", "region":"Nevada", "frequency":"Weekly", "freq_note":"County sheriff sales", "listing_names":["clark"], "source_keys":["clark_sheriff_sales"]},
    # Tennessee — Monthly
    {"ui_key":"davidson",       "label":"Davidson (Nashville)",  "state":"TN", "region":"Tennessee", "frequency":"Monthly", "freq_note":"Chancery Court posts lists ~monthly", "listing_names":["davidson"],   "source_keys":["davidson"]},
    {"ui_key":"williamson",     "label":"Williamson (Franklin)", "state":"TN", "region":"Tennessee", "frequency":"Monthly", "freq_note":"County delinquent tax list ~monthly",  "listing_names":["williamson"], "source_keys":["williamson"]},
    {"ui_key":"rutherford",     "label":"Rutherford (Murfreesboro)","state":"TN","region":"Tennessee","frequency":"Monthly","freq_note":"RC Chancery Court ~monthly",            "listing_names":["rutherford"], "source_keys":["rutherford"]},
    {"ui_key":"wilson-tn",      "label":"Wilson (Lebanon)",      "state":"TN", "region":"Tennessee", "frequency":"Monthly", "freq_note":"Chancery Court ~monthly",              "listing_names":["wilson"],     "source_keys":["wilson"]},
    {"ui_key":"sumner",         "label":"Sumner (Gallatin)",     "state":"TN", "region":"Tennessee", "frequency":"Monthly", "freq_note":"Chancery Court ~monthly",              "listing_names":["sumner"],     "source_keys":["sumner"]},
    {"ui_key":"robertson",      "label":"Robertson (Springfield)","state":"TN","region":"Tennessee", "frequency":"Monthly", "freq_note":"Court auctions ~monthly",              "listing_names":["robertson"],  "source_keys":["robertson"]},
    {"ui_key":"cheatham",       "label":"Cheatham (Ashland City)","state":"TN","region":"Tennessee", "frequency":"Monthly", "freq_note":"Tax sales ~monthly",                  "listing_names":["cheatham"],   "source_keys":["cheatham"]},
]


_COUNTY_REQUEST_RATE = {}
_COUNTY_REQUEST_RATE_LOCK = threading.Lock()
_US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}


def _normalize_county_name(value):
    text = re.sub(r"\([^)]*\)", " ", str(value or "")).lower()
    text = re.sub(r"\b(county|parish|borough|census area|municipality)\b", " ", text)
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _county_display_name(value):
    cleaned = re.sub(r"\s+", " ", str(value or "").strip())
    cleaned = re.sub(r"\s+(county|parish|borough)\s*$", "", cleaned, flags=re.I)
    return cleaned.title()


def _normalize_mobile(value):
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return "+1" + digits if len(digits) == 10 else ""


def _valid_email(value):
    return bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", str(value or "").strip()))


def _county_request_is_covered(county_key, state):
    for row in _COUNTY_SCHEDULE:
        if str(row.get("state") or "").upper() != state:
            continue
        if any(_normalize_county_name(name) == county_key for name in row.get("listing_names") or []):
            return True
    return False


def _county_live_count(county_key, state, listings=None):
    rows = listings if listings is not None else publishable_storefront_listings(current_listings())
    return sum(
        1 for item in rows
        if str(item.get("state") or "").upper() == state
        and _normalize_county_name(item.get("county")) == county_key
    )


def _county_alerts_configured():
    email_ready = bool(os.getenv("SMTP_HOST") and os.getenv("SMTP_FROM"))
    sms_ready = bool(os.getenv("TWILIO_ACCOUNT_SID") and os.getenv("TWILIO_AUTH_TOKEN")
                     and os.getenv("TWILIO_FROM_NUMBER"))
    return email_ready and sms_ready


def _send_county_email(entry):
    host = os.environ["SMTP_HOST"]
    port = int(os.getenv("SMTP_PORT", "587"))
    security = os.getenv("SMTP_SECURITY", "starttls").strip().lower()
    message = EmailMessage()
    message["From"] = os.environ["SMTP_FROM"]
    message["To"] = entry["email"]
    message["Subject"] = f"{entry['county']} County, {entry['state']} leads are ready"
    base_url = os.getenv("PUBLIC_BASE_URL", "https://tax-delinquencies-production.up.railway.app").rstrip("/")
    link = f"{base_url}/?county={entry['county_key'].replace(' ', '+')}&state={entry['state']}"
    message.set_content(
        f"{entry['county']} County, {entry['state']} is ready on ForeclosureLeads Pro.\n\n"
        f"View the available leads: {link}\n\n"
        "You received this one-time notice because you requested an availability alert."
    )
    client_type = smtplib.SMTP_SSL if security == "ssl" else smtplib.SMTP
    with client_type(host, port, timeout=20) as client:
        if security == "starttls":
            client.starttls()
        username = os.getenv("SMTP_USERNAME")
        if username:
            client.login(username, os.getenv("SMTP_PASSWORD", ""))
        client.send_message(message)


def _send_county_sms(entry):
    sid = os.environ["TWILIO_ACCOUNT_SID"]
    token = os.environ["TWILIO_AUTH_TOKEN"]
    base_url = os.getenv("PUBLIC_BASE_URL", "https://tax-delinquencies-production.up.railway.app").rstrip("/")
    body = urlencode({
        "From": os.environ["TWILIO_FROM_NUMBER"],
        "To": entry["phone"],
        "Body": (f"ForeclosureLeads Pro: {entry['county']} County, {entry['state']} is ready. "
                 f"View leads: {base_url}/?county={entry['county_key'].replace(' ', '+')}&state={entry['state']} "
                 "Reply STOP to opt out."),
    }).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
        data=body,
        headers={"Authorization": "Basic " + base64.b64encode(f"{sid}:{token}".encode()).decode()},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError(f"Twilio returned HTTP {response.status}")


def _deliver_county_alert(request_id):
    with purchase_store.transaction() as conn:
        entries = purchase_store.read(conn, COUNTY_REQUESTS_KEY, {})
        entry = entries.get(request_id)
    if not entry or entry.get("status") == "notified":
        return True
    if entry.get("status") != "ready" or not _county_alerts_configured():
        return False

    if entry.get("email_status") != "sent":
        _send_county_email(entry)
        with purchase_store.transaction() as conn:
            entries = purchase_store.read(conn, COUNTY_REQUESTS_KEY, {})
            entries[request_id]["email_status"] = "sent"
            entries[request_id]["email_sent_at"] = datetime.now(timezone.utc).isoformat()
            purchase_store.write(conn, COUNTY_REQUESTS_KEY, entries)
    if entry.get("sms_status") != "sent":
        _send_county_sms(entry)
        with purchase_store.transaction() as conn:
            entries = purchase_store.read(conn, COUNTY_REQUESTS_KEY, {})
            entries[request_id]["sms_status"] = "sent"
            entries[request_id]["sms_sent_at"] = datetime.now(timezone.utc).isoformat()
            entries[request_id]["status"] = "notified"
            entries[request_id]["notified_at"] = datetime.now(timezone.utc).isoformat()
            purchase_store.write(conn, COUNTY_REQUESTS_KEY, entries)
    return True


def _mark_county_requests_ready(listings):
    available = {
        (_normalize_county_name(item.get("county")), str(item.get("state") or "").upper())
        for item in listings if item.get("county") and item.get("state")
    }
    if not available:
        return 0
    ready_ids = []
    now = datetime.now(timezone.utc).isoformat()
    with purchase_store.transaction() as conn:
        entries = purchase_store.read(conn, COUNTY_REQUESTS_KEY, {})
        for request_id, entry in entries.items():
            if entry.get("status") == "waiting" and (entry.get("county_key"), entry.get("state")) in available:
                entry.update({"status": "ready", "ready_at": now,
                              "email_status": "pending", "sms_status": "pending"})
                ready_ids.append(request_id)
        if ready_ids:
            purchase_store.write(conn, COUNTY_REQUESTS_KEY, entries)
            if _county_alerts_configured():
                for request_id in ready_ids:
                    purchase_store.enqueue(conn, "county-alert:" + request_id, "county_alert")
    return len(ready_ids)


def _enqueue_ready_county_alerts():
    if not _county_alerts_configured():
        return 0
    queued = 0
    with purchase_store.transaction() as conn:
        entries = purchase_store.read(conn, COUNTY_REQUESTS_KEY, {})
        for request_id, entry in entries.items():
            if entry.get("status") == "ready":
                purchase_store.enqueue(conn, "county-alert:" + request_id, "county_alert")
                queued += 1
    return queued


@app.route('/api/county-interest', methods=['POST'])
def county_interest():
    payload = request.get_json(silent=True) or {}
    if payload.get("website"):
        return jsonify({"ok": True})
    county = _county_display_name(payload.get("county"))
    county_key = _normalize_county_name(county)
    state = str(payload.get("state") or "").strip().upper()
    email = str(payload.get("email") or "").strip().lower()
    phone = _normalize_mobile(payload.get("phone"))
    if not 2 <= len(county_key) <= 80:
        return jsonify({"error": "Enter a valid county name."}), 400
    if state not in _US_STATES:
        return jsonify({"error": "Choose a valid state."}), 400
    if not _valid_email(email):
        return jsonify({"error": "Enter a valid email address."}), 400
    if not phone:
        return jsonify({"error": "Enter a valid 10-digit mobile number."}), 400
    if payload.get("consent") is not True:
        return jsonify({"error": "Agree to the one-time email and text alert to continue."}), 400

    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    cutoff = time.time() - 3600
    with _COUNTY_REQUEST_RATE_LOCK:
        attempts = [stamp for stamp in _COUNTY_REQUEST_RATE.get(ip, []) if stamp >= cutoff]
        if len(attempts) >= 10:
            return jsonify({"error": "Too many requests. Please try again later."}), 429
        attempts.append(time.time())
        _COUNTY_REQUEST_RATE[ip] = attempts

    live_count = _county_live_count(county_key, state)
    covered = _county_request_is_covered(county_key, state)
    request_id = hashlib.sha256(f"{county_key}|{state}|{email}|{phone}".encode()).hexdigest()[:32]
    now = datetime.now(timezone.utc).isoformat()
    consent_text = ("One email and one text alert about this county's availability; "
                    "message and data rates may apply; reply STOP to opt out.")
    with purchase_store.transaction() as conn:
        entries = purchase_store.read(conn, COUNTY_REQUESTS_KEY, {})
        prior = entries.get(request_id, {})
        entries[request_id] = {
            **prior, "id": request_id, "county": county, "county_key": county_key,
            "state": state, "email": email, "phone": phone,
            "first_requested_at": prior.get("first_requested_at") or now,
            "last_requested_at": now, "request_count": int(prior.get("request_count") or 0) + 1,
            "consent_at": now, "consent_version": "county-alert-v1", "consent_text": consent_text,
            "covered_at_request": covered, "live_count_at_request": live_count,
            "status": ("available" if live_count else
                       ("notified" if prior.get("status") == "notified" else "waiting")),
        }
        purchase_store.write(conn, COUNTY_REQUESTS_KEY, entries)

    if live_count:
        return jsonify({"ok": True, "availability": "available", "count": live_count,
                        "message": f"Yes—we have {live_count:,} live leads in {county} County, {state}.",
                        "url": f"/?county={county_key.replace(' ', '+')}&state={state}"})
    if covered:
        message = (f"We cover {county} County, {state}, but there are no current leads. "
                   "We saved your request and will email and text you when fresh inventory is ready.")
    else:
        message = (f"We saved {county} County, {state}. We will add the county you entered. "
                   "Stay tuned over the next day or so—we will email and text you when it is ready.")
    return jsonify({"ok": True, "availability": "waiting", "message": message})


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
    scrape_runs = list(reversed(_sqlite_get(SCRAPE_RUN_INVENTORY_KEY, [])[-250:]))
    county_requests = sorted(
        _sqlite_get(COUNTY_REQUESTS_KEY, {}).values(),
        key=lambda item: item.get("last_requested_at", ""), reverse=True,
    )
    return render_template(
        'admin_counties.html',
        schedule=schedule,
        total_counties=len(schedule),
        total_leads=total_leads,
        states=states,
        daily_count=daily_count,
        weekly_count=weekly_count,
        monthly_count=monthly_count,
        scrape_runs=scrape_runs,
        county_requests=county_requests,
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

@app.route('/admin/competition')
@admin_required
def admin_competition():
    """Competitive-position dashboard: live inventory stats vs researched
    competitor intel, scored per strategic dimension."""
    if STOREFRONT_ONLY:
        return redirect(url_for('index'), code=302)
    from collections import Counter
    from datetime import date as _date, timedelta as _td

    with listing_lock:
        listings = load_json(DATA_FILE, [])
    pub = publishable_storefront_listings(listings)
    total = len(pub)
    sold = sum(1 for x in listings if x.get("sold_at"))
    counties = {str(x.get("county") or "").strip().lower() for x in pub if x.get("county")}
    states = {str(x.get("state") or "").strip().upper() for x in pub if x.get("state")}
    owner_n = sum(1 for x in pub if str(x.get("owner") or "").strip())
    traced_n = sum(1 for x in pub if _lead_is_traced(x))
    today = _date.today()

    def _days_old(x):
        raw = str(x.get("scraped_date") or "")[:10]
        try:
            return (today - _date.fromisoformat(raw)).days
        except ValueError:
            return None
    ages = [d for d in (_days_old(x) for x in pub) if d is not None]
    fresh7 = sum(1 for d in ages if d <= 7)
    fresh30 = sum(1 for d in ages if d <= 30)
    types = Counter(str(x.get("status") or "Unknown") for x in pub)

    def pct(n, d):
        return round(n * 100 / d) if d else 0

    stats = {
        "total": total,
        "sold": sold,
        "counties": len(counties),
        "registry_counties": 61,   # distinct counties in scrapers/source_registry.py
        "states": len(states),
        "owner_pct": pct(owner_n, total),
        "traced_n": traced_n,
        "traced_pct": pct(traced_n, total),
        "fresh7_pct": pct(fresh7, len(ages)),
        "fresh30_pct": pct(fresh30, len(ages)),
        "dated_n": len(ages),
        "types": types.most_common(),
        "price_raw": LEAD_PRICE_RAW,
        "price_traced": LEAD_PRICE_TRACED,
        "as_of": str(today),
    }
    return render_template('admin_competition.html', s=stats)


# One-shot cleanup for stray county values in the listings store:
#  - Ontario municipal tax-sale rows scraped before the scraper labeled them
#    county="Ontario" (each municipality showed as its own "county", and the
#    June 2026 batch's tender dates have all passed).
#  - Legacy HUD/HomePath rows for counties with no active source (GA/IL,
#    from early manual runs; REO inventory that stale is long gone).
_STRAY_COUNTIES = {
    "bradford west gwillimbury", "clearview", "coleman", "ear falls",
    "east garafraxa", "fort erie", "gananoque", "innisfil", "kirkland lake",
    "leeds and the thousand islands", "pelham", "richmond hill",
    "south glengarry",
    "chatham", "glynn", "camden", "fulton", "cook", "lowndes",
}

def _duval_needs_street(item):
    if str(item.get("source") or "") != "Duval Co.":
        return False
    if not str(item.get("parcel_id") or "").strip():
        return False
    addr = str(item.get("address") or "")
    return not re.match(r"^\s*\d", addr) and not str(item.get("street") or "").strip()


@app.route('/api/admin/duval-parcels-needing-address')
@admin_required
def duval_parcels_needing_address():
    """List Duval records still missing a street address, as {id: parcel_id}.
    The address lookup itself must run from a residential IP (the Jacksonville
    Property Appraiser tar-pits datacenter IPs), so resolution happens off-box
    and the results come back via apply-duval-addresses."""
    if STOREFRONT_ONLY:
        return jsonify({"status": "disabled", "reason": "storefront_only"}), 403
    with listing_lock:
        listings = load_json(DATA_FILE, [])
    parcels = {str(it.get("id")): str(it.get("parcel_id"))
               for it in listings if _duval_needs_street(it)}
    return jsonify({"count": len(parcels), "parcels": parcels})


@app.route('/api/admin/apply-duval-addresses', methods=['POST'])
@admin_required
def apply_duval_addresses():
    """Apply resolved street addresses (looked up off-box) to Duval records.
    Body: {"resolved": {"<lead id>": "<street address>", ...}}. Idempotent."""
    if STOREFRONT_ONLY:
        return jsonify({"status": "disabled", "reason": "storefront_only"}), 403
    payload = request.get_json(silent=True) or {}
    resolved = payload.get("resolved") or {}
    if not isinstance(resolved, dict):
        return jsonify({"error": "resolved must be an object of id->street"}), 400

    updated = 0
    with listing_lock:
        listings = load_json(DATA_FILE, [])
        for item in listings:
            street = resolved.get(str(item.get("id")))
            if not street or not str(street).strip():
                continue
            if str(item.get("street") or "").strip():
                continue   # already has a street
            city = str(item.get("city") or "Jacksonville").strip() or "Jacksonville"
            item["street"] = str(street).strip()
            item["address"] = f"{str(street).strip()}, {city}, FL"
            updated += 1
        if updated:
            save_json(DATA_FILE, listings)
        remaining = sum(1 for it in listings if _duval_needs_street(it))

    return jsonify({"updated": updated, "remaining": remaining, "done": remaining == 0})


@app.route('/api/admin/purge-stray-counties', methods=['POST'])
@admin_required
def purge_stray_counties():
    if STOREFRONT_ONLY:
        return jsonify({"status": "disabled", "reason": "storefront_only"}), 403
    from collections import Counter
    with listing_lock:
        listings = load_json(DATA_FILE, [])
        removed = Counter(
            str(item.get("county") or "").strip().lower()
            for item in listings
            if str(item.get("county") or "").strip().lower() in _STRAY_COUNTIES
        )
        if removed:
            kept = [item for item in listings
                    if str(item.get("county") or "").strip().lower() not in _STRAY_COUNTIES]
            save_json(DATA_FILE, kept)
    return jsonify({"removed_total": sum(removed.values()),
                    "removed_by_county": dict(removed)})

@app.route('/admin/data/download')
@admin_required
def admin_data_download():
    from flask import send_file
    filename = request.args.get('file', '')
    if not filename:
        files = _list_csv_files()
        if not files:
            return "No CSV files available", 404
        filename = files[0]
    path = _safe_csv_path(filename)
    if not path:
        return "File not found", 404
    return send_file(path, as_attachment=True, download_name=os.path.basename(path))

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
    if os.getenv("EXTERNAL_SCRAPER_JOBS", "").strip().lower() in {"1", "true", "yes", "on"}:
        return jsonify({"status": "disabled", "reason": "external_scraper_jobs"}), 409
    scrape_sync, run_scrapers, _request_kill, clear_kill, ScraperKilled = scraper_runtime()
    scraper_map = county_scraper_map()

    if scrape_status["running"]:
        return jsonify({"status": "already_running"})

    payload = request.get_json(silent=True) or {}
    resume = bool(payload.get("resume"))
    prior_progress = load_json(SCRAPE_PROGRESS_FILE, {}) if resume else {}
    if resume and (prior_progress.get("finished") or not prior_progress.get("completed")):
        prior_progress = {}   # nothing to resume — run fresh
    settings = normalize_settings(load_json(SETTINGS_FILE, {}))
    counties = payload.get("counties") or settings.get("counties") or ["chatham-ga", "glynn-ga", "camden-ga", "duval-fl", "stjohns-fl", "nassau-fl"]
    lookback = int(payload.get("lookback_days", settings.get("lookback_days", 30)))
    lookback = max(1, min(365, lookback))
    if prior_progress:
        # Re-run the interrupted run's own config so "resume" means exactly that.
        counties = prior_progress.get("counties") or counties
        lookback = int(prior_progress.get("lookback_days") or lookback)
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
                # Resume: skip sources the interrupted run already finished and
                # surface their prior results so the panel shows the full run.
                completed = dict(prior_progress.get("completed") or {})
                if completed:
                    for sk, prior in completed.items():
                        if sk in scraper_counties and isinstance(prior, dict):
                            prior = dict(prior)
                            prior["note"] = (prior.get("note") or "") + " [previous run]"
                            scrape_status["scraper_results"][sk] = prior
                    update_progress(f"Resuming: {len(completed)} source(s) already done")
                progress = {
                    "started_at": prior_progress.get("started_at") or scrape_status["started_at"],
                    "updated_at": scrape_status["started_at"],
                    "finished": False,
                    "counties": counties,
                    "lookback_days": lookback,
                    "planned": scraper_counties,
                    "completed": completed,
                }
                save_json(SCRAPE_PROGRESS_FILE, progress)

                def _record_done(sk, result):
                    progress["completed"][sk] = result
                    progress["updated_at"] = datetime.now().isoformat(timespec="seconds")
                    save_json(SCRAPE_PROGRESS_FILE, progress)

                if scraper_counties:
                    # Run each scraper individually so we can track per-key results
                    for sk in scraper_counties:
                        if stop_event.is_set():
                            break
                        if sk in completed:
                            continue    # finished in the interrupted run
                        update_progress(f"Scraping: {sk}")
                        t0 = time.monotonic()
                        try:
                            recs = run_scrapers([sk], lookback_days=lookback)
                            elapsed = time.monotonic() - t0
                            csv_file = _write_scrape_csv(sk, recs)
                            listings = property_records_to_listings(recs)
                            real = [r for r in recs if r.get("owner_name") or r.get("parcel_id") or r.get("property_address")]
                            stub_only = len(real) == 0
                            new_count = save_batch(listings) if listings else 0
                            status = "empty" if stub_only else "ok"
                            note = "stub/portal link only" if stub_only else f"{new_count} new / {len(listings)} kept / {len(recs)} raw"
                            if csv_file:
                                note += f" / CSV: {csv_file}"
                            result = source_result(
                                sk, len(recs), len(listings), new_count, status, note,
                                seconds=elapsed,
                            )
                            scrape_status["scraper_results"][sk] = result
                            _record_done(sk, result)
                        except ScraperKilled:
                            scrape_status["scraper_results"][sk] = source_result(
                                sk, 0, 0, 0, "error", "killed mid-run",
                                seconds=time.monotonic() - t0)
                            update_progress(f"{sk} killed mid-run")
                            break  # exit the per-scraper loop entirely
                        except Exception as e:
                            # Errors are not marked completed, so a resume retries them.
                            scrape_status["scraper_results"][sk] = source_result(
                                sk, 0, 0, 0, "error", str(e)[:120],
                                seconds=time.monotonic() - t0)
                            update_progress(f"Error scraping {sk}: {e}")

                if not stop_event.is_set():
                    progress["finished"] = True
                    save_json(SCRAPE_PROGRESS_FILE, progress)

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

@app.route('/api/scrape/ingest', methods=['POST'])
@admin_required
def ingest_scrape_results():
    """Merge one completed source batch from an external, short-lived job."""
    if STOREFRONT_ONLY:
        return jsonify({"status": "disabled", "reason": "storefront_only"}), 403

    payload = request.get_json(silent=True) or {}
    county_key = str(payload.get("county") or "").strip().lower()
    source_key = str(payload.get("source") or "").strip().lower()
    records = payload.get("records")
    allowed_sources = set(county_scraper_map().get(county_key) or [])

    if not county_key or source_key not in allowed_sources:
        return jsonify({"error": "invalid county/source pair"}), 400
    if not isinstance(records, list):
        return jsonify({"error": "records must be a list"}), 400
    if len(records) > 500:
        return jsonify({"error": "batch exceeds 500 records"}), 413
    if any(not isinstance(record, dict) for record in records):
        return jsonify({"error": "every record must be an object"}), 400

    run_id = str(payload.get("run_id") or "").strip()[:240]
    try:
        batch_number = int(payload.get("batch_number") or 1)
        batch_total = int(payload.get("batch_total") or 1)
        attempts_used = max(1, min(int(payload.get("attempts_used") or 1), 3))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid batch numbering"}), 400
    if batch_number < 1 or batch_total < 1 or batch_number > batch_total:
        return jsonify({"error": "invalid batch numbering"}), 400
    # Older/manual publishers do not send a run ID. Give those deliveries a
    # unique receipt while scheduled jobs use their stable workflow run ID.
    if not run_id:
        run_id = f"legacy:{county_key}:{source_key}:{time.time_ns()}"
    inventory_id = f"{run_id}:{source_key}"
    received_at = datetime.now(timezone.utc).isoformat()

    incoming = property_records_to_listings(records)
    with listing_lock:
        inventory = _sqlite_get(SCRAPE_RUN_INVENTORY_KEY, [])
        prior = next((item for item in inventory if item.get("id") == inventory_id), None)
        prior_batch = (prior or {}).get("batches", {}).get(str(batch_number))
        if prior_batch:
            return jsonify({**prior_batch, "replayed": True})

        current = load_json(DATA_FILE, [])
        merged, added = merge_listings(current, incoming)
        if incoming:
            save_json(DATA_FILE, merged)

        observed_dates = []
        for record in records:
            fallback_year = str(record.get("scraped_date") or "")[:4] or None
            normalized = _parse_sale_date(
                record.get("posted_date") or record.get("sale_date") or record.get("date"),
                fallback_year=fallback_year,
            )
            if normalized:
                observed_dates.append(normalized)

        result = {
            "status": "success", "county": county_key, "source": source_key,
            "raw": len(records), "kept": len(incoming), "added": added,
            "total": len(merged), "batch_number": batch_number,
            "batch_total": batch_total,
        }
        entry = prior or {
            "id": inventory_id, "run_id": run_id, "county": county_key,
            "source": source_key, "workflow_run_id": str(payload.get("workflow_run_id") or "")[:80],
            "workflow_run_attempt": str(payload.get("workflow_run_attempt") or "")[:20],
            "started_at": str(payload.get("started_at") or "")[:80],
            "completed_at": str(payload.get("completed_at") or "")[:80],
            "first_received_at": received_at, "last_received_at": received_at,
            "raw": 0, "kept": 0, "added": 0, "batch_total": batch_total,
            "batches": {}, "county_date_min": "", "county_date_max": "",
            "run_status": str(payload.get("run_status") or "success")[:20],
            "error": str(payload.get("error") or "")[:500],
            "schedule_reason": str(payload.get("schedule_reason") or "legacy")[:30],
            "scheduled_local_time": str(payload.get("scheduled_local_time") or "")[:80],
            "attempts_used": attempts_used,
        }
        entry["last_received_at"] = received_at
        entry["completed_at"] = str(payload.get("completed_at") or entry.get("completed_at") or "")[:80]
        entry["batch_total"] = max(int(entry.get("batch_total") or 1), batch_total)
        entry["raw"] = int(entry.get("raw") or 0) + len(records)
        entry["kept"] = int(entry.get("kept") or 0) + len(incoming)
        entry["added"] = int(entry.get("added") or 0) + added
        entry["batches"][str(batch_number)] = result
        if observed_dates:
            candidates = observed_dates + [entry.get("county_date_min"), entry.get("county_date_max")]
            candidates = [value for value in candidates if value]
            entry["county_date_min"], entry["county_date_max"] = min(candidates), max(candidates)
        if entry.get("run_status") == "failed":
            entry["status"] = "failed"
        else:
            entry["status"] = ("complete" if len(entry["batches"]) >= entry["batch_total"] else "receiving")
        if prior:
            inventory.remove(prior)
        inventory.append(entry)
        _sqlite_set(SCRAPE_RUN_INVENTORY_KEY, inventory[-5000:])

        if added:
            _mark_county_requests_ready(incoming)

    return jsonify(result)


@app.route('/api/admin/scrape-runs')
@admin_required
def scrape_run_inventory():
    """Return the durable source-run ledger, newest first."""
    try:
        limit = max(1, min(int(request.args.get("limit", 500)), 5000))
    except ValueError:
        return jsonify({"error": "invalid limit"}), 400
    runs = list(reversed(_sqlite_get(SCRAPE_RUN_INVENTORY_KEY, [])[-limit:]))
    public_runs = [{key: value for key, value in item.items() if key != "batches"} for item in runs]
    return jsonify({"count": len(public_runs), "runs": public_runs})


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
    status = dict(scrape_status)
    # Let the admin UI offer "Resume last run" when a run was interrupted.
    progress = load_json(SCRAPE_PROGRESS_FILE, {})
    done = len(progress.get("completed") or {})
    planned = len(progress.get("planned") or [])
    status["resumable"] = bool(
        not scrape_status["running"]
        and progress
        and not progress.get("finished")
        and done
        and planned > done
    )
    if status["resumable"]:
        status["resume_info"] = {
            "completed": done,
            "planned": planned,
            "started_at": progress.get("started_at"),
        }
    return jsonify(status)

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

# --- CA trustee-sale address cleanup + cross-source dedupe -------------------
_CA_LOCATION_RE = re.compile(r"(\d[^,\n]{2,60},\s*[A-Za-z .]+,\s*CA\s*\d{5})", re.I)
_CA_CITY_STATE_RE = re.compile(r"([A-Za-z .]+),\s*CA\s+(\d{5})", re.I)
_CA_APN_RE = re.compile(
    r"(?:A\.?P\.?N\.?|Assessor'?s?\s+Parcel\s+(?:No|Number|#))[:#\s.]+([0-9][\d\-]{5,})", re.I)


def _clean_ca_address_inplace(item):
    """Some CA trustee-sale rows stored the raw notice blurb as the address
    ('is: 24638 PAPPAS ROAD, RAMONA, CA 92065 Assessor's Parcel No. ...'). Pull
    out the clean street/city/zip and the parcel. Returns True if it changed."""
    addr = str(item.get("address") or "")
    # A raw notice address embeds the location as "..., CA 92065" (no comma
    # before the zip), whereas our cleaned output is "..., CA, 92065". So a
    # _CA_LOCATION_RE hit uniquely identifies a still-messy row, and the check
    # is self-idempotent (cleaned rows no longer match).
    loc = _CA_LOCATION_RE.search(addr)
    if not loc:
        return False
    changed = False
    location = re.sub(r"\s+", " ", loc.group(1)).strip()
    cs = _CA_CITY_STATE_RE.search(location)
    if cs:
        street = location[:cs.start()].strip(" ,")
        item["street"] = street
        item["city"] = cs.group(1).strip().title()
        item["zip"] = cs.group(2)
        item["state"] = "CA"
        item["address"] = ", ".join(p for p in [street, item["city"], "CA", cs.group(2)] if p)
        changed = True
    apn = _CA_APN_RE.search(addr)
    if apn and not str(item.get("parcel_id") or "").strip():
        item["parcel_id"] = apn.group(1)
        changed = True
    return changed


def _dedupe_key(item):
    return _parcel_date_key(item) or None


def _completeness(item):
    return sum(1 for k in ("owner", "street", "parcel_id", "zip", "amount_owed", "price")
               if str(item.get(k) or "").strip() and str(item.get(k)) != "5")


def _pick_better(a, b):
    """Keep the more complete row (tie: more recent scrape), filling any blanks
    on the winner from the loser so no field is lost."""
    dates = [d for d in (discovery_date(a), discovery_date(b)) if d]
    winner, loser = (a, b)
    if _completeness(b) > _completeness(a):
        winner, loser = b, a
    elif _completeness(b) == _completeness(a):
        if str(b.get("scraped_date") or "") > str(a.get("scraped_date") or ""):
            winner, loser = b, a
    for k, v in loser.items():
        if str(winner.get(k) or "").strip() in ("", "5") and str(v or "").strip() not in ("", "5"):
            winner[k] = v
    if dates:
        winner["first_seen"] = min(dates).isoformat()
    return winner


def _migrate_clean_and_dedupe():
    """One-time, idempotent startup maintenance: clean polluted owner names,
    clean messy CA addresses (+parcels), then collapse duplicate rows for the
    same property. Only writes when something changes, so it no-ops afterward."""
    if STOREFRONT_ONLY:
        return
    try:
        listings = load_json(DATA_FILE, [])
        original = len(listings)
        owners = addrs = dates = 0
        for item in listings:
            if normalize_listing_dates(item):
                dates += 1
            if owner_looks_polluted(item.get("owner")):
                cleaned = clean_owner_name(item.get("owner"))
                if cleaned != item.get("owner"):
                    item["owner"] = cleaned
                    owners += 1
            if _clean_ca_address_inplace(item):
                addrs += 1

        best, order, passthrough = {}, [], []
        for item in listings:
            k = _dedupe_key(item)
            if k is None:
                passthrough.append(item)
                continue
            if k in best:
                best[k] = _pick_better(best[k], item)
            else:
                best[k] = item
                order.append(k)
        deduped = [best[k] for k in order] + passthrough
        removed = original - len(deduped)

        # Safety valve: a correct dedupe never nukes a huge share of the store.
        if removed > original * 0.25:
            print(f"[migrate] ABORT dedupe: would remove {removed}/{original} (>25%).")
            return
        if owners or addrs or dates or removed:
            save_json(DATA_FILE, deduped)
            print(f"[migrate] owners={owners} ca_addr={addrs} dates={dates} deduped={removed} "
                  f"({original}->{len(deduped)}).")
    except Exception as e:
        print(f"[migrate] cleanup/dedupe skipped: {e}")


import sys
import purchase_runtime
purchase_runtime.install(sys.modules[__name__])

if os.getenv("DISABLE_STARTUP_MIGRATION") != "1":
    _migrate_clean_and_dedupe()


if __name__ == '__main__':
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8095"))
    print(f"Foreclosure app running at http://{host}:{port}")
    app.run(host=host, port=port, debug=False, use_reloader=False)
