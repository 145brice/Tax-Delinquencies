"""Run one county's sources, deliver results, and exit completely."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scraper_runner import run_scrapers


SCHEDULE_PATH = Path(__file__).with_name("county_schedule.json")


def post_batch(
    base_url: str,
    token: str,
    county: str,
    source: str,
    records: list[dict],
    attempts: int,
) -> dict:
    for attempt in range(1, attempts + 1):
        try:
            response = requests.post(
                f"{base_url.rstrip('/')}/api/scrape/ingest",
                headers={"X-Admin-Token": token},
                json={"county": county, "source": source, "records": records},
                timeout=60,
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException:
            if attempt == attempts:
                raise
            time.sleep(min(30, 5 * attempt))
    raise RuntimeError("unreachable")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--county", required=True)
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument("--batch-size", type=int, default=250)
    args = parser.parse_args()
    attempts = max(1, min(args.attempts, 3))

    base_url = os.environ.get("SCRAPE_TARGET_URL", "").strip()
    token = os.environ.get("ADMIN_TOKEN", "").strip()
    if not base_url or not token:
        print("SCRAPE_TARGET_URL and ADMIN_TOKEN are required", file=sys.stderr)
        return 2

    config = json.loads(SCHEDULE_PATH.read_text(encoding="utf-8"))
    county = config["counties"].get(args.county)
    if not county:
        print(f"Unknown scheduled county: {args.county}", file=sys.stderr)
        return 2

    failed: list[str] = []
    for source in county["sources"]:
        records = None
        for attempt in range(1, attempts + 1):
            try:
                print(f"[{args.county}/{source}] scrape attempt {attempt}/{attempts}", flush=True)
                records = run_scrapers([source], args.lookback_days, raise_errors=True)
                break
            except Exception as exc:
                print(f"[{args.county}/{source}] attempt {attempt} failed: {exc}", file=sys.stderr, flush=True)
                if attempt < attempts:
                    time.sleep(min(60, 10 * attempt))

        if records is None:
            failed.append(source)
            continue

        batches = [records[i:i + args.batch_size] for i in range(0, len(records), args.batch_size)] or [[]]
        for number, batch in enumerate(batches, 1):
            result = post_batch(base_url, token, args.county, source, batch, attempts)
            print(f"[{args.county}/{source}] delivered batch {number}/{len(batches)}: {result}", flush=True)

    if failed:
        print(f"County job failed after retries: {', '.join(failed)}", file=sys.stderr)
        return 1
    print(f"County job complete: {args.county}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
