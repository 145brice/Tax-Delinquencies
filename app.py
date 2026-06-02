import os
import re
import json
import hashlib
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
from scraper import scrape_sync
from scraper_runner import run_scrapers
from scrapers.base_scraper import request_kill, clear_kill, ScraperKilled
from scrapers.source_registry import SOURCE_METADATA, UI_COUNTY_SOURCES

load_dotenv()

# Maps Flask county keys to scraper_runner county names
# UI key to one or more scraper_runner keys (a single UI selection may fan
# out into multiple scrapers, e.g. San Diego has separate tax-sale and
# legal-notice sources).
COUNTY_SCRAPER_MAP = UI_COUNTY_SOURCES


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
STOREFRONT_ONLY = IS_VERCEL
SQLITE_DB = os.getenv("SQLITE_DB", os.path.join(BASE_DIR, "foreclosure_local.sqlite3"))
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
    address = str(item.get("address") or "").strip()
    if len(address) < 8:
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
    source_id = _source_listing_id(public_item)

    public_item["owner"] = _storefront_owner(public_item)
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
    elif "foreclosure" in status:
        public_item["sale_date"] = public_item.get("sale_date") or public_item.get("date") or public_item.get("scraped_date")

    if public_item.get("address"):
        public_item["address"] = obfuscate_address(str(public_item["address"]))
    public_item["street"] = ""
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
    meta = SOURCE_METADATA.get(source_key, {})
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
    parts = address.split(' ', 1)
    if len(parts) > 1:
        return f"XXXX {parts[1]}"
    return "Address Hidden"

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
    display_listings = []
    for item in listings:
        masked_item = _storefront_display_row(item)
        masked_item['display_address'] = masked_item.get("address") or obfuscate_address(item.get('address', ''))
        display_listings.append(masked_item)
    return render_template('index.html', listings=display_listings, storefront_only=STOREFRONT_ONLY)

@app.route('/admin')
def admin():
    if STOREFRONT_ONLY:
        return redirect(url_for('index'), code=302)
    settings = load_json(SETTINGS_FILE, {"counties": [], "lookback_days": 30})
    source_cards = sorted(
        SOURCE_METADATA.values(),
        key=lambda s: (s.get("region", ""), s.get("label", "")),
    )
    return render_template('admin.html', settings=settings, source_cards=source_cards)

@app.route('/admin/<path:bad_path>')
def admin_bad_link_redirect(bad_path):
    if STOREFRONT_ONLY:
        return redirect(url_for('index'), code=302)
    """Recover from older Data Explorer links that missed the /data? route."""
    if bad_path.startswith("-file="):
        query = bad_path.replace("-file=", "file=", 1)
        return redirect(f"{url_for('admin_data')}?{query}", code=302)
    return "Not Found", 404

# ---- Auth ------------------------------------------------------------------

def current_user():
    """Return the logged-in user row, or None. Cached per request."""
    uid = session.get("user_id")
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
    return {"current_user_email": user["email"] if user else None}


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def _accounts_ready():
    """True when both the DB and the session secret are configured."""
    return db.is_configured() and bool(app.secret_key)


@app.route('/register', methods=['GET', 'POST'])
def register():
    if not _accounts_ready():
        return render_template('auth.html', mode='register',
                               error="Accounts are not configured yet. Set DATABASE_URL and SECRET_KEY.")
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
        user = db.create_user(email, generate_password_hash(password))
        if not user:
            return render_template('auth.html', mode='register', error="That email is already registered. Try logging in.", email=email)
        session['user_id'] = user['id']
        return redirect(url_for('account'))
    return render_template('auth.html', mode='register')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if not _accounts_ready():
        return render_template('auth.html', mode='login',
                               error="Accounts are not configured yet. Set DATABASE_URL and SECRET_KEY.")
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


@app.route('/logout', methods=['POST', 'GET'])
def logout():
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
    return render_template('account.html', email=user['email'], orders=orders)


# ---- Checkout --------------------------------------------------------------

@app.route('/api/create-checkout-session', methods=['POST'])
def create_checkout_session():
    if not stripe.api_key:
        return jsonify({"error": "Payments are not configured yet."}), 503
    if not _accounts_ready():
        return jsonify({"error": "Accounts are not configured yet. Set DATABASE_URL and SECRET_KEY."}), 503

    user = current_user()
    if not user:
        return jsonify({"login_required": True,
                        "error": "Please log in to purchase leads."}), 401

    payload = request.get_json(silent=True) or {}
    selected_ids = {str(lead_id) for lead_id in payload.get("lead_ids", [])}
    if not selected_ids:
        return jsonify({"error": "Select at least one lead first."}), 400

    listings = current_listings()
    selected = [item for item in listings if str(item.get("id")) in selected_ids]
    if not selected:
        return jsonify({"error": "Selected leads were not found."}), 400

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
                        "name": f"{len(selected)} foreclosure lead(s)",
                    },
                    "unit_amount": total_cents,
                },
                "quantity": 1,
            }],
            metadata={
                "user_id": str(user["id"]),
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
            leads=selected,
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
    if event.get("type") == "checkout.session.completed":
        sess = event["data"]["object"]
        _fulfill_session(sess.get("id"))
    return ("ok", 200)

@app.route('/checkout/cancel')
def checkout_cancel():
    return render_template('checkout_status.html', title="Checkout canceled", message="Checkout was canceled. Your selected leads were not charged.")

@app.route('/api/stripe-status')
def stripe_status():
    key = stripe.api_key or ""
    return jsonify({
        "stripe_configured": bool(key),
        "webhook_configured": bool(os.getenv("STRIPE_WEBHOOK_SECRET", "")),
        "key_prefix": (key[:8] if key else None),
    })

@app.route('/api/accounts-status')
def accounts_status():
    """Diagnostic: confirms the DB env var is set, the session secret is set,
    and that we can actually connect to Postgres and create the tables."""
    db_url_set = db.is_configured()
    secret_set = bool(app.secret_key)
    db_connected = False
    detail = None
    if db_url_set:
        try:
            db.init_db()
            db_connected = True
        except Exception as exc:  # surface a short, password-free reason
            detail = type(exc).__name__ + ": " + str(exc)[:200]
    return jsonify({
        "accounts_ready": bool(db_url_set and secret_set and db_connected),
        "db_url_set": db_url_set,
        "secret_key_set": secret_set,
        "db_connected": db_connected,
        "detail": detail,
    })

@app.route('/api/checkout-status')
def checkout_status_api():
    """Diagnostic for the full purchase-unlock flow."""
    db_url_set = db.is_configured()
    secret_set = bool(app.secret_key)
    stripe_set = bool(stripe.api_key)
    webhook_set = bool(os.getenv("STRIPE_WEBHOOK_SECRET", ""))
    db_connected = False
    detail = None
    if db_url_set:
        try:
            db.init_db()
            db_connected = True
        except Exception as exc:
            detail = type(exc).__name__ + ": " + str(exc)[:200]
    return jsonify({
        "checkout_ready": bool(stripe_set and db_url_set and secret_set and db_connected),
        "stripe_secret_key_set": stripe_set,
        "stripe_webhook_secret_set": webhook_set,
        "database_url_set": db_url_set,
        "secret_key_set": secret_set,
        "database_connected": db_connected,
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
    "notes": "Notes",
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
            rows.append(row)
    return rows

@app.route('/admin/data')
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

    if filter_county:
        rows = [r for r in rows if filter_county in r.get('county', '').lower()]
    if filter_type:
        rows = [r for r in rows if filter_type in r.get('record_type', '').lower()]
    if filter_q:
        rows = [r for r in rows if any(filter_q in str(v).lower() for v in r.values())]

    if sort_col in CSV_FIELDNAMES:
        rows = sorted(rows, key=lambda r: _csv_sort_value(r, sort_col), reverse=(sort_dir == 'desc'))

    all_rows = _load_csv_rows(selected) if selected else []
    counties = sorted({r.get('county', '') for r in all_rows if r.get('county')})
    types    = sorted({r.get('record_type', '') for r in all_rows if r.get('record_type')})

    return render_template(
        'admin_data.html',
        rows=rows, total=total, filtered=len(rows),
        files=files, selected=selected,
        fieldnames=CSV_FIELDNAMES, column_labels=CSV_LABELS,
        filter_county=request.args.get('filter_county', ''),
        filter_type=request.args.get('filter_type', ''),
        filter_q=request.args.get('q', ''),
        sort_col=sort_col, sort_dir=sort_dir,
        counties=counties, types=types,
        settings=settings,
    )

@app.route('/admin/data/download')
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
def csv_dash():
    if STOREFRONT_ONLY:
        return redirect(url_for('index'), code=302)
    listings = current_listings()
    settings = normalize_settings(load_json(SETTINGS_FILE, {}))
    return render_template('csv_dash.html', listings=listings, settings=settings)

@app.route('/api/settings', methods=['POST'])
def update_settings():
    if STOREFRONT_ONLY:
        return jsonify({"status": "disabled", "reason": "storefront_only"}), 403
    settings = request.json
    save_json(SETTINGS_FILE, settings)
    return jsonify({"status": "success"})

@app.route('/api/sources')
def sources_json():
    if STOREFRONT_ONLY:
        return jsonify({"sources": [], "county_sources": {}, "storefront_only": True})
    return jsonify({
        "sources": sorted(
            SOURCE_METADATA.values(),
            key=lambda s: (s.get("region", ""), s.get("label", "")),
        ),
        "county_sources": COUNTY_SCRAPER_MAP,
    })

@app.route('/api/scrape', methods=['POST'])
def run_scraper():
    if STOREFRONT_ONLY:
        return jsonify({"status": "disabled", "reason": "storefront_only"}), 403

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
                    sk for c in counties if c in COUNTY_SCRAPER_MAP
                    for sk in COUNTY_SCRAPER_MAP[c]
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
        request_kill()
        scrape_status["stopping"] = "kill"
        scrape_status["last"] = "kill_requested"
        return jsonify({"status": "killing"})
    scrape_status["stopping"] = True
    scrape_status["last"] = "stopping_requested"
    return jsonify({"status": "stopping"})

@app.route('/api/scrape/status')
def scrape_status_check():
    if STOREFRONT_ONLY:
        return jsonify({"running": False, "last": "storefront_only"})
    return jsonify(scrape_status)

@app.route('/api/listings')
def listings_json():
    if STOREFRONT_ONLY:
        return jsonify({"status": "disabled", "reason": "storefront_only"}), 403
    with listing_lock:
        listings = load_json(DATA_FILE, [])
    return jsonify({"count": len(listings), "listings": listings})

@app.route('/api/listings.csv')
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
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8095"))
    print(f"Foreclosure app running at http://{host}:{port}")
    app.run(host=host, port=port, debug=False, use_reloader=False)

