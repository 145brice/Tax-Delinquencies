"""Select county jobs whose local posting window has just closed."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


SCHEDULE_PATH = Path(__file__).with_name("county_schedule.json")


def load_config() -> dict:
    return json.loads(SCHEDULE_PATH.read_text(encoding="utf-8"))


def due_counties(config: dict, now: datetime) -> list[str]:
    scheduled: list[str] = []
    known = set(config["counties"])
    for window in config.get("posting_windows", {}).values():
        local = now.astimezone(ZoneInfo(window["timezone"]))
        if local.hour != int(window["local_hour"]):
            continue
        if "weekdays" in window and local.weekday() not in window["weekdays"]:
            continue
        if "month_days" in window and local.day not in window["month_days"]:
            continue
        scheduled.extend(c for c in window["counties"] if c in known)
    return list(dict.fromkeys(scheduled))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--county", help="Plan one county, or 'all', for a manual run")
    parser.add_argument("--now", help="UTC ISO timestamp used for deterministic checks")
    args = parser.parse_args()
    config = load_config()

    if args.county == "all":
        counties = list(config["counties"])
    elif args.county:
        if args.county not in config["counties"]:
            parser.error(f"unknown county: {args.county}")
        counties = [args.county]
    else:
        now = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else datetime.now(timezone.utc)
        counties = due_counties(config, now)

    print(json.dumps({"include": [{"county": county} for county in counties]}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
