import os
import json
import threading
import csv
import io
from datetime import datetime
from flask import Flask, render_template, request, jsonify, Response
from scraper import scrape_sync

app = Flask(__name__)
scrape_status = {
    "running": False,
    "last": None,
    "count": 0,
    "started_at": None,
    "stopping": False,
    "updated_at": None,
    "current_step": None,
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

@app.route('/')
def index():
    listings = load_json(DATA_FILE, [])
    display_listings = []
    for item in listings:
        masked_item = item.copy()
        masked_item['display_address'] = obfuscate_address(item['address'])
        display_listings.append(masked_item)
    return render_template('index.html', listings=display_listings)

@app.route('/admin')
def admin():
    settings = load_json(SETTINGS_FILE, {"counties": [], "lookback_days": 30})
    return render_template('admin.html', settings=settings)

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

    def do_scrape():
        scrape_status["running"] = True
        scrape_status["started_at"] = datetime.now().isoformat(timespec="seconds")
        scrape_status["updated_at"] = None
        scrape_status["count"] = len(load_json(DATA_FILE, []))
        scrape_status["last"] = "running"

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
    if scrape_control["stop_event"] is not None:
        scrape_control["stop_event"].set()
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
