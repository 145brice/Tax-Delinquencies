import os
import re
import json
import hashlib
import threading
import csv
import io
from datetime import datetime
from flask import Flask, render_template, request, jsonify, Response
from scraper import scrape_sync
from scraper_runner import run_scrapers
from scrapers.base_scraper import request_kill, clear_kill, ScraperKilled

# Maps Flask county keys → scraper_runner county names
# UI key → one or more scraper_runner keys (a single UI selection may fan
# out into multiple scrapers, e.g. San Diego has separate tax-sale and
# legal-notice sources).
COUNTY_SCRAPER_MAP = {
    "davidson":   ["davidson"],
    "wilson-tn":  ["wilson"],
    "williamson": ["williamson"],
    "rutherford": ["rutherford"],
    "sumner":     ["sumner"],
    "robertson":  ["robertson"],
    "cheatham":   ["cheatham"],
    "sandiego":   ["sandiego_taxsale", "sandiego_legalnotices"],
    "losangeles": ["losangeles_legalnotices"],
    "orange":     ["orange_taxsale", "orange_legalnotices"],
    "riverside":  ["riverside_legalnotices"],
}


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
            # No street address — use APN so each parcel gets a unique listing
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
        bid = int(re.sub(r'[^\d]', '', amount_str)) if amount_str else 0

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

DATA_FILE = 'listings.json'
SETTINGS_FILE = 'settings.json'
DEFAULT_SOURCES = {
    "include_tax_records": True,
    "include_hud": True,
    "include_homepath": True,
}

def load_json(file, default):
    if os.path.exists(file):
        with open(file, 'r') as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return default
    return default

def save_json(file, data):
    with open(file, 'w') as f:
        json.dump(data, f, indent=4)

def merge_listings(existing, incoming):
    merged = list(existing)
    seen = {str(item.get("id")) for item in merged if item.get("id") is not None}
    for item in incoming:
        item_id = str(item.get("id"))
        if item_id and item_id not in seen:
            merged.append(item)
            seen.add(item_id)
    return merged

def prioritize_counties(counties, sources):
    if not sources.get("include_tax_records"):
        return counties
    tax_first = ["davidson", "wilson-tn"]
    return sorted(counties, key=lambda county: 0 if county in tax_first else 1)

def normalize_settings(settings):
    settings.setdefault("counties", ["miami-dade", "davidson", "wilson-tn"])
    settings.setdefault("lookback_days", 30)
    settings["sources"] = {**DEFAULT_SOURCES, **settings.get("sources", {})}
    return settings

def obfuscate_address(address):
    parts = address.split(' ', 1)
    if len(parts) > 1:
        return f"XXXX {parts[1]}"
    return "Address Hidden"

_ADDR_PARSE_RE = re.compile(r'^(.*?),\s*([A-Za-z\.\- ]+),\s*([A-Z]{2})(?:\s+(\d{5}))?\s*$')

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
    # Ensure all keys exist (None → '') so template doesn't crash
    for k in ['city','state','zip','owner','parcel_id','case_number',
              'amount_owed','sale_date','scraped_date','county','link',
              'bid','record_type','street']:
        if item.get(k) is None:
            item[k] = '' if k != 'bid' else 0
    return item

@app.route('/')
def index():
    listings = load_json(DATA_FILE, [])
    display_listings = []
    for item in listings:
        masked_item = _backfill_fields(item.copy())
        masked_item['display_address'] = obfuscate_address(item['address'])
        display_listings.append(masked_item)
    return render_template('index.html', listings=display_listings)

@app.route('/admin')
def admin():
    settings = load_json(SETTINGS_FILE, {"counties": [], "lookback_days": 30})
    return render_template('admin.html', settings=settings)

DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
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
    files = sorted(glob.glob(os.path.join(DATA_DIR, '*.csv')), reverse=True)
    return [os.path.basename(f) for f in files]

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
    files = _list_csv_files()
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
        rows = sorted(rows, key=lambda r: r.get(sort_col, '').lower(), reverse=(sort_dir == 'desc'))

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
    )

@app.route('/admin/data/download')
def admin_data_download():
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
    listings = load_json(DATA_FILE, [])
    settings = normalize_settings(load_json(SETTINGS_FILE, {}))
    return render_template('csv_dash.html', listings=listings, settings=settings)

@app.route('/api/settings', methods=['POST'])
def update_settings():
    settings = request.json
    save_json(SETTINGS_FILE, settings)
    return jsonify({"status": "success"})

@app.route('/api/scrape', methods=['POST'])
def run_scraper():
    if scrape_status["running"]:
        return jsonify({"status": "already_running"})

    payload = request.get_json(silent=True) or {}
    settings = normalize_settings(load_json(SETTINGS_FILE, {}))
    counties = payload.get("counties") or settings.get("counties") or ["miami-dade", "harris", "fulton"]
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
                merged = merge_listings(current, batch)
                save_json(DATA_FILE, merged)
                scrape_status["count"] = len(merged)
                scrape_status["updated_at"] = datetime.now().isoformat(timespec="seconds")
                scrape_status["last"] = f"saved {len(batch)} new listing(s)"

        def update_progress(message):
            scrape_status["current_step"] = message
            scrape_status["updated_at"] = datetime.now().isoformat(timespec="seconds")
            scrape_status["last"] = message

        try:
            scrape_status["scraper_results"] = {}

            # ── Phase 1: Tax delinquency PDFs via scrapers/ ──────────────────
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
                            recs = run_scrapers([sk])
                            listings = property_records_to_listings(recs)
                            real = [r for r in recs if r.get("owner_name") or r.get("parcel_id") or r.get("property_address")]
                            stub_only = len(real) == 0
                            scrape_status["scraper_results"][sk] = {
                                "count": len(listings),
                                "raw": len(recs),
                                "status": "empty" if stub_only else "ok",
                                "note": "stub/portal link only" if stub_only else f"{len(listings)} leads",
                            }
                            if listings:
                                save_batch(listings)
                        except ScraperKilled:
                            scrape_status["scraper_results"][sk] = {
                                "count": 0, "raw": 0, "status": "error",
                                "note": "killed mid-run",
                            }
                            update_progress(f"{sk} killed mid-run")
                            break  # exit the per-scraper loop entirely
                        except Exception as e:
                            scrape_status["scraper_results"][sk] = {
                                "count": 0, "raw": 0, "status": "error", "note": str(e)[:120]
                            }
                            update_progress(f"Error scraping {sk}: {e}")

            # ── Phase 2: HUD + HomePath REO via scraper.py ───────────────────
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
                        merged = merge_listings(current, results)
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
    return jsonify(scrape_status)

@app.route('/api/listings')
def listings_json():
    with listing_lock:
        listings = load_json(DATA_FILE, [])
    return jsonify({"count": len(listings), "listings": listings})

@app.route('/api/listings.csv')
def listings_csv():
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
    print("Foreclosure Scraper running at http://127.0.0.1:8095")
    app.run(host='127.0.0.1', port=8095, debug=False, use_reloader=False)
