"""
Main runner for source-registered public-record scrapers.

Usage:
    python scraper_runner.py
    python scraper_runner.py --county duval_jaxdailyrecord
    python scraper_runner.py --county davidson wilson
"""
import argparse
import csv
import os
from dataclasses import fields
from datetime import date

from scrapers.base_scraper import PropertyRecord
from scrapers.source_registry import ALL_SCRAPERS, REGION_BY_KEY, SOURCE_METADATA


DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
CSV_FIELDNAMES = [f.name for f in fields(PropertyRecord)]


def run_scrapers(county_names: list[str]) -> list[dict]:
    all_records = []
    for name in county_names:
        key = name.lower()
        cls = ALL_SCRAPERS.get(key)
        if not cls:
            print(f"[WARNING] Unknown scraper source: {name}")
            continue
        scraper = cls()
        label = SOURCE_METADATA.get(key, {}).get("label", key)
        try:
            records = scraper.scrape()
            all_records.extend([r.to_dict() for r in records])
            print(f"[{label}] {len(records)} records scraped.")
        except Exception as e:
            print(f"[{label}] ERROR: {e}")
    return all_records


def save_csv(records: list[dict], output_path: str):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        writer.writerows(records)
    print(f"[CSV] Saved {len(records)} records to {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Public-record foreclosure source runner")
    parser.add_argument(
        "--county",
        nargs="+",
        default=list(ALL_SCRAPERS.keys()),
        choices=list(ALL_SCRAPERS.keys()),
        help="Scraper source key or keys to scrape (default: all)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output CSV file path (default: data/<region>_<date>.csv)",
    )
    args = parser.parse_args()

    if args.output:
        output_path = args.output
    else:
        regions = {REGION_BY_KEY.get(c, "scrape") for c in args.county}
        prefix = next(iter(regions)) if len(regions) == 1 else "multi"
        output_path = os.path.join(DATA_DIR, f"{prefix}_{date.today()}.csv")

    print(f"Scraping sources: {', '.join(args.county)}")
    records = run_scrapers(args.county)
    save_csv(records, output_path)
    print(f"\nDone. {len(records)} total records. CSV: {output_path}")
    return output_path


if __name__ == "__main__":
    main()
