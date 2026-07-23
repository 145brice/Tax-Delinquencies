"""
Triggers a scrape run on the live app via /api/scrape, for one frequency
bucket (daily / weekly / monthly) from county_schedule.json.

Usage:
    python scripts/run_scheduled_scrape.py --frequency daily

Env vars required:
    SCRAPE_TARGET_URL   base URL of the deployed app, e.g. https://tax-delinquencies-production.up.railway.app
    ADMIN_TOKEN         matches ADMIN_TOKEN / SKIPTRACE_ADMIN_TOKEN set on the server
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

SCHEDULE_PATH = Path(__file__).parent / "county_schedule.json"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--frequency", required=True, choices=["daily", "weekly", "monthly"])
    parser.add_argument("--lookback-days", type=int, default=None)
    args = parser.parse_args()

    base_url = os.environ["SCRAPE_TARGET_URL"].rstrip("/")
    token = os.environ["ADMIN_TOKEN"]

    counties = json.loads(SCHEDULE_PATH.read_text(encoding="utf-8"))[args.frequency]
    print(f"Triggering {args.frequency} scrape for {len(counties)} counties: {', '.join(counties)}")

    payload = {"counties": counties}
    if args.lookback_days:
        payload["lookback_days"] = args.lookback_days

    resp = requests.post(f"{base_url}/api/scrape", params={"token": token}, json=payload, timeout=30)
    resp.raise_for_status()
    print(resp.json())

    if resp.json().get("status") == "already_running":
        print("A scrape was already in progress; nothing new started.")
        return

    # Poll status until it finishes so the Action log shows the outcome.
    for _ in range(120):
        time.sleep(15)
        status = requests.get(f"{base_url}/api/scrape/status", params={"token": token}, timeout=15).json()
        print(f"  {status.get('current_step') or status.get('last')}")
        if not status.get("running"):
            print("Scrape finished:", status.get("last"))
            break
    else:
        print("Timed out waiting for scrape to finish (still running server-side).")


if __name__ == "__main__":
    sys.exit(main())
