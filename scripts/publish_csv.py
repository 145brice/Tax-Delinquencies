"""Publish a raw scraper CSV to the Railway inventory through authenticated ingestion."""
import argparse
import csv
import os
from pathlib import Path
from run_county_job import post_batch


def read_records(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"county", "property_address", "owner_name", "source_url", "scraped_date"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError("Use a raw scraper CSV, not the masked storefront export")
        rows = list(reader)
    if any("*" in row["property_address"] or "*" in row["owner_name"] for row in rows):
        raise ValueError("Masked records cannot be published as purchasable inventory")
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file")
    parser.add_argument("--county", required=True, help="UI county key, e.g. duval-fl")
    parser.add_argument("--source", required=True, help="Registry source key")
    args = parser.parse_args()
    target, token = os.getenv("SCRAPE_TARGET_URL"), os.getenv("ADMIN_TOKEN")
    if not target or not token:
        parser.error("Set SCRAPE_TARGET_URL and ADMIN_TOKEN")
    rows = read_records(args.file)
    for offset in range(0, len(rows), 250):
        result = post_batch(target, token, args.county, args.source, rows[offset:offset + 250], 2)
        print(f"Published batch: added={result.get('added', 0)}, total={result.get('total', 0)}")


if __name__ == "__main__":
    main()
