"""
Flask admin portal for Nashville tax delinquency / pre-foreclosure data.
Run:  python app.py
Then open: http://localhost:5000
"""
import os
import sys
import csv
import json
import glob
from datetime import date
from flask import Flask, render_template, request, send_file, jsonify, redirect, url_for, flash
import subprocess
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
app = Flask(__name__)
app.secret_key = "nashville-tax-admin-2026"

FIELDNAMES = [
    "county", "record_type", "owner_name", "property_address", "city",
    "state", "zip_code", "parcel_id", "tax_year", "amount_owed",
    "sale_date", "case_number", "source_url", "scraped_date", "notes",
]

COLUMN_LABELS = {
    "county": "County",
    "record_type": "Type",
    "owner_name": "Owner Name",
    "property_address": "Property Address",
    "city": "City",
    "state": "State",
    "zip_code": "ZIP",
    "parcel_id": "Parcel ID",
    "tax_year": "Tax Year",
    "amount_owed": "Amount Owed",
    "sale_date": "Sale / Record Date",
    "case_number": "Case #",
    "source_url": "Source URL",
    "scraped_date": "Scraped Date",
    "notes": "Notes",
}

scrape_status = {"running": False, "last_message": ""}


def _list_csv_files():
    files = sorted(glob.glob(os.path.join(DATA_DIR, "*.csv")), reverse=True)
    return files


def _load_csv(path: str) -> list[dict]:
    rows = []
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
    except Exception:
        pass
    return rows


def _get_latest_csv() -> str | None:
    files = _list_csv_files()
    return files[0] if files else None


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------

@app.route("/")
def index():
    files = _list_csv_files()
    selected = request.args.get("file", "")
    if not selected and files:
        selected = os.path.basename(files[0])

    rows = []
    total = 0
    if selected:
        full_path = os.path.join(DATA_DIR, selected)
        if os.path.exists(full_path):
            rows = _load_csv(full_path)
            total = len(rows)

    # Filter params
    filter_county = request.args.get("filter_county", "").strip().lower()
    filter_type = request.args.get("filter_type", "").strip().lower()
    filter_q = request.args.get("q", "").strip().lower()

    if filter_county:
        rows = [r for r in rows if filter_county in r.get("county", "").lower()]
    if filter_type:
        rows = [r for r in rows if filter_type in r.get("record_type", "").lower()]
    if filter_q:
        rows = [r for r in rows if any(filter_q in str(v).lower() for v in r.values())]

    # Sort
    sort_col = request.args.get("sort", "county")
    sort_dir = request.args.get("dir", "asc")
    if sort_col in FIELDNAMES:
        rows = sorted(rows, key=lambda r: r.get(sort_col, "").lower(), reverse=(sort_dir == "desc"))

    # Unique counties/types for filter dropdowns
    all_rows_raw = _load_csv(os.path.join(DATA_DIR, selected)) if selected else []
    counties = sorted(set(r.get("county", "") for r in all_rows_raw if r.get("county")))
    types = sorted(set(r.get("record_type", "") for r in all_rows_raw if r.get("record_type")))

    return render_template(
        "index.html",
        rows=rows,
        total=total,
        filtered=len(rows),
        files=[os.path.basename(f) for f in _list_csv_files()],
        selected=selected,
        fieldnames=FIELDNAMES,
        column_labels=COLUMN_LABELS,
        filter_county=request.args.get("filter_county", ""),
        filter_type=request.args.get("filter_type", ""),
        filter_q=request.args.get("q", ""),
        sort_col=sort_col,
        sort_dir=sort_dir,
        counties=counties,
        types=types,
        scrape_running=scrape_status["running"],
        scrape_message=scrape_status["last_message"],
    )


@app.route("/download")
def download():
    filename = request.args.get("file", "")
    if not filename:
        latest = _get_latest_csv()
        if not latest:
            flash("No CSV files available.", "error")
            return redirect(url_for("index"))
        filename = os.path.basename(latest)

    path = os.path.join(DATA_DIR, filename)
    if not os.path.exists(path):
        flash(f"File not found: {filename}", "error")
        return redirect(url_for("index"))

    return send_file(path, as_attachment=True, download_name=filename)


@app.route("/scrape", methods=["POST"])
def trigger_scrape():
    if scrape_status["running"]:
        flash("Scrape already in progress.", "warning")
        return redirect(url_for("index"))

    counties = request.form.getlist("counties") or []
    county_arg = " ".join(counties) if counties else ""

    def run():
        scrape_status["running"] = True
        scrape_status["last_message"] = "Running..."
        try:
            python_exe = os.path.join(BASE_DIR, "venv", "Scripts", "python.exe")
            if not os.path.exists(python_exe):
                python_exe = sys.executable  # fallback to current interpreter
            cmd = [python_exe, os.path.join(BASE_DIR, "scraper_runner.py")]
            if counties:
                cmd += ["--county"] + counties
            result = subprocess.run(cmd, capture_output=True, text=True, cwd=BASE_DIR)
            scrape_status["last_message"] = result.stdout.strip().split("\n")[-1] if result.stdout else "Done."
            if result.returncode != 0:
                scrape_status["last_message"] = f"Error: {result.stderr[:300]}"
        except Exception as e:
            scrape_status["last_message"] = f"Failed: {e}"
        finally:
            scrape_status["running"] = False

    t = threading.Thread(target=run, daemon=True)
    t.start()
    flash("Scrape started in background. Refresh in ~30 seconds.", "success")
    return redirect(url_for("index"))


@app.route("/api/status")
def api_status():
    return jsonify(scrape_status)


@app.route("/api/data")
def api_data():
    """JSON endpoint — returns filtered rows for the selected CSV."""
    selected = request.args.get("file", "")
    if not selected:
        latest = _get_latest_csv()
        selected = os.path.basename(latest) if latest else ""

    rows = []
    if selected:
        full_path = os.path.join(DATA_DIR, selected)
        rows = _load_csv(full_path)

    return jsonify({"total": len(rows), "rows": rows})


if __name__ == "__main__":
    os.makedirs(DATA_DIR, exist_ok=True)
    print("Nashville Tax Admin Portal running at http://localhost:5000")
    app.run(debug=True, port=5000)
