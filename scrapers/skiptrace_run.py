"""
Local batch skip tracer -- run on YOUR machine (residential IP), headed.

Reads the same skip-trace queue that /admin/skiptrace shows, looks up each
pending lead's phone/email for FREE via Google-snippet harvesting
(scrapers/skiptrace_search.py), then:
  * writes a review CSV (data/skiptrace_results.csv by default), AND
  * populates primary_phone / phone_2 / email_1 back into the queue leads,
    using the exact same write path as the admin "update" form.

WHY LOCAL + HEADED: Google rate-limits by IP/velocity. From your residential IP,
headed, at human pace, it runs clean; when Google throws a CAPTCHA the browser
window pauses so you can solve it, then it resumes. Never run this on the server.

Usage (from project root):
  python -m scrapers.skiptrace_run                    # process the /admin/skiptrace queue
  python -m scrapers.skiptrace_run --county davidson  # run ALL of a county from listings.json
  python -m scrapers.skiptrace_run --county davidson --limit 50   # first 50 of a county
  python -m scrapers.skiptrace_run --no-write         # CSV only, don't touch leads
  python -m scrapers.skiptrace_run --all              # include already-traced leads
  python -m scrapers.skiptrace_run --gap-min 30 --gap-max 75

Anti-flag safety (county runs): human gaps (default 30-75s) + a long break
(default 3-7 min) every 25 leads. It SKIPS leads already traced and caches every
result, so you can stop (Ctrl-C) and re-run to resume -- it won't redo work.
County write-back goes into listings.json (a one-time listings.json.bak is made).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

# project root on path so "import app" / "scrapers.*" work when run as a module or script
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scrapers import skiptrace_search as ss  # noqa: E402

DEFAULT_CSV = ROOT / "data" / "skiptrace_results.csv"
LISTINGS_PATH = ROOT / "listings.json"
CONTROL_PATH = ROOT / "data" / ".skiptrace_control.json"
STATUS_PATH = ROOT / "data" / ".skiptrace_status.json"


def _write_status(path: str, data: dict) -> None:
    try:
        Path(path).write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def _control_action(path: str) -> str:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")).get("action", "") or ""
    except Exception:
        return ""


def _split_city_state(lead: dict) -> tuple[str, str]:
    """Prefer explicit fields; else parse from a 'street, City, ST' address."""
    city = str(lead.get("city") or "").strip()
    state = str(lead.get("state") or "").strip()
    if city and state:
        return city, state
    addr = str(lead.get("address") or "").strip()
    parts = [p.strip() for p in addr.split(",") if p.strip()]
    if len(parts) >= 2:
        city = city or parts[-2]
        # last chunk may be "TN" or "TN 37207"
        m = re.match(r"([A-Za-z]{2})\b", parts[-1])
        state = state or (m.group(1) if m else parts[-1])
    return city, state


def _needs_trace(lead: dict) -> bool:
    return not (str(lead.get("primary_phone") or "").strip()
                or str(lead.get("email_1") or "").strip())


def load_queue():
    """Return (dev_mode, list of (order_id, lead_dict)) from the live queue."""
    import app  # safe: Flask server only starts under app's __main__
    dev_mode = app._skiptrace_dev_enabled()
    if dev_mode:
        orders = app._skiptrace_dev_orders()
    else:
        app.db.init_db()
        orders = app.db.get_paid_orders()
    pairs = []
    for order in orders:
        for lead in order.get("leads_json") or []:
            pairs.append((str(order.get("id")), lead))
    return dev_mode, pairs


def write_back(dev_mode: bool, order_id: str, lead: dict, fields: dict) -> bool:
    import app
    if dev_mode:
        return app._skiptrace_dev_update(order_id, str(lead.get("id")), fields)
    return app.db.update_order_lead_contacts(order_id, str(lead.get("id")), fields)


def load_listings(county: str, state: str, city: str):
    """Return (all_rows, selected_leads) from the APP's data store. The app keeps
    listings in SQLite (when enabled), with listings.json only a seed -- so we must
    read via app.current_listings(), NOT the raw file, or we'd miss everything the
    app saved (e.g. the scraper refresh). Selected leads are references into all_rows,
    so mutating them in place + save_listings(all_rows) persists the change."""
    import app
    rows = app.current_listings()

    def match(l):
        if county and str(l.get("county") or "").strip().lower() != county.lower():
            return False
        if state and str(l.get("state") or "").strip().lower() != state.lower():
            return False
        if city and str(l.get("city") or "").strip().lower() != city.lower():
            return False
        return bool(str(l.get("owner") or "").strip())

    return rows, [l for l in rows if match(l)]


def save_listings(rows) -> None:
    """Persist through the app's data layer (SQLite when enabled, else the file)."""
    import app
    app.save_json(app.DATA_FILE, rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Free local batch skip tracer (Google-snippet).")
    ap.add_argument("--county", help="run ALL leads in this county from listings.json (e.g. davidson)")
    ap.add_argument("--state", help="filter listings by state (e.g. TN)")
    ap.add_argument("--city", help="filter listings by city")
    ap.add_argument("--limit", type=int, default=0, help="max leads to process (0 = no limit)")
    ap.add_argument("--all", action="store_true", help="include leads that already have a phone/email")
    ap.add_argument("--no-write", action="store_true", help="write CSV only, don't modify leads")
    ap.add_argument("--engine", choices=["ddg", "google", "combo"], default="ddg",
                    help="ddg = DuckDuckGo (no browser/CAPTCHA, default); google = HTTP+headed "
                         "browser (CAPTCHA-prone); combo = DDG first, Google for the misses")
    ap.add_argument("--headless", action="store_true", help="google/combo: run browser without a window")
    ap.add_argument("--csv", default=str(DEFAULT_CSV), help="review CSV output path")
    ap.add_argument("--gap-min", type=float, default=None, help="min seconds between searches")
    ap.add_argument("--gap-max", type=float, default=None, help="max seconds between searches")
    # --- anti-flag safety controls ---
    ap.add_argument("--break-every", type=int, default=25, help="take a long break after this many leads")
    ap.add_argument("--break-min", type=float, default=180, help="min break length (s)")
    ap.add_argument("--break-max", type=float, default=420, help="max break length (s)")
    ap.add_argument("--control-file", default=str(CONTROL_PATH), help="JSON stop-signal file")
    ap.add_argument("--status-file", default=str(STATUS_PATH), help="JSON progress file")
    args = ap.parse_args(argv)

    if args.gap_min is not None:
        os.environ["SKIPTRACE_DDG_MIN_GAP"] = str(args.gap_min)
        os.environ["SKIPTRACE_GOOGLE_MIN_GAP"] = str(args.gap_min)
    if args.gap_max is not None:
        os.environ["SKIPTRACE_DDG_MAX_GAP"] = str(args.gap_max)
        os.environ["SKIPTRACE_GOOGLE_MAX_GAP"] = str(args.gap_max)
    import importlib
    importlib.reload(ss)                       # re-read pacing env

    # --- pick the lead source ---
    listings_mode = bool(args.county or args.city)
    listings_data = None
    if listings_mode:
        listings_data, leads = load_listings(args.county or "", args.state or "", args.city or "")
        pairs = [(str(l.get("id")), l) for l in leads]
        src_label = f"listings.json county={args.county or '-'} city={args.city or '-'} state={args.state or '-'}"
        dev_mode = None
    else:
        dev_mode, pairs = load_queue()
        src_label = f"queue ({'dev-sqlite' if dev_mode else 'db'})"

    todo = [(k, ld) for k, ld in pairs if args.all or _needs_trace(ld)]
    if args.limit:
        todo = todo[:args.limit]

    print(f"Source: {src_label} | engine: {args.engine} | matched: {len(pairs)} | "
          f"to process: {len(todo)} | write-back: {not args.no_write}")
    if listings_mode:
        g0, g1 = (ss.GOOGLE_MIN_GAP, ss.GOOGLE_MAX_GAP) if args.engine == "google" else (ss.DDG_MIN_GAP, ss.DDG_MAX_GAP)
        eta_min = len(todo) * (g0 + g1) / 2 / 60
        print(f"Pacing ({args.engine}): {g0:.0f}-{g1:.0f}s gaps, {args.break_min:.0f}-{args.break_max:.0f}s "
              f"break every {args.break_every} leads (~{eta_min:.0f} min). Ctrl-C any time; progress saved.")
    if not todo:
        print("Nothing to do.")
        return 0

    rows = []
    processed = wrote = 0
    csv_path = Path(args.csv)
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    def flush_csv():
        if rows:
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)

    status = {
        "state": "running", "county": (args.county or args.city or "queue"),
        "total": len(todo), "i": 0, "processed": 0, "wrote": 0, "with_phone": 0,
        "current": "", "pid": os.getpid(),
        "started_at": datetime.now().isoformat(timespec="seconds"), "updated_at": "",
    }

    def push_status():
        status["with_phone"] = sum(1 for r in rows if r["best_phone"])
        status["processed"] = processed
        status["wrote"] = wrote
        status["updated_at"] = datetime.now().isoformat(timespec="seconds")
        _write_status(args.status_file, status)

    push_status()
    stopped = False

    try:
        with ss.get_session(args.engine, headless=args.headless) as session:
            for i, (key, lead) in enumerate(todo, 1):
                # cooperative stop: "Kill After" lets the in-flight lead finish, then halts here
                act = _control_action(args.control_file)
                if act in ("stop", "stop_after"):
                    print(f"  Stop requested ({act}); halting cleanly. Progress saved.")
                    stopped = True
                    break

                owner = str(lead.get("owner") or "").strip()
                street = str(lead.get("street") or lead.get("address") or "").strip()
                city, state = _split_city_state(lead)
                print(f"\n[{i}/{len(todo)}] {owner}  |  {street}  |  {city}, {state}")
                status["i"] = i
                status["current"] = owner
                push_status()

                try:
                    r = session.lookup(owner, street, city, state)
                except ss.Blocked as b:
                    print(f"  BLOCKED: {b}. Stopping; progress saved. Re-run later to resume "
                          f"(done leads are skipped).")
                    status["state"] = "blocked"
                    break
                except KeyboardInterrupt:
                    print("\n  Interrupted; progress saved.")
                    stopped = True
                    break

                phones = r.get("phones") or []
                emails = r.get("emails") or []
                best = phones[0] if phones else ""
                second = phones[1] if len(phones) > 1 else ""
                details = r.get("phone_details") or []
                best_score = details[0]["score"] if details else ""
                sourced = "; ".join(
                    f"{d['phone']} [{d.get('source') or '?'}|{d['score']}]" for d in details[:6]
                )
                print(f"  -> best={best!r}({best_score})  all: {sourced or 'none'}"
                      + ("  [cached]" if r.get("cached") else ""))
                processed += 1

                rows.append({
                    "key": key, "lead_id": lead.get("id"), "owner": owner, "address": street,
                    "city": city, "state": state, "county": lead.get("county", ""),
                    "best_phone": best, "best_score": best_score,
                    "all_phones": "; ".join(phones[:6]), "phones_sourced": sourced,
                    "emails": "; ".join(emails[:4]), "query": r.get("query", ""),
                })

                if not args.no_write and (best or emails):
                    fields = {
                        "primary_phone": best,
                        "phone_2": second,
                        "email_1": emails[0] if emails else "",
                        "email_2": emails[1] if len(emails) > 1 else "",
                        "mailing_address": str(lead.get("mailing_address") or ""),
                        "skiptrace_notes": f"auto ({r.get('engine', args.engine)}-snippet): {r.get('query','')}",
                        "skiptrace_source": {"ddg": "DuckDuckGo", "google": "Google",
                                             "combo": "DuckDuckGo/Google"}.get(
                            r.get("engine", args.engine), "DuckDuckGo"),
                        "skiptraced_at": datetime.now().isoformat(timespec="seconds"),
                        "skiptrace_status": "completed" if (best or emails) else "pending",
                    }
                    try:
                        if listings_mode:
                            lead.update(fields)        # mutates listings_data in place
                            wrote += 1
                        elif write_back(dev_mode, key, lead, fields):
                            wrote += 1
                    except Exception as exc:
                        print(f"  write-back failed: {type(exc).__name__}: {exc}")

                push_status()

                # periodic persistence so an interrupt never loses work
                if processed % 10 == 0:
                    flush_csv()
                    if listings_mode and not args.no_write:
                        save_listings(listings_data)

                # anti-flag long break (interruptible by a stop request)
                if args.break_every and processed and processed % args.break_every == 0 and i < len(todo):
                    nap = random.uniform(args.break_min, args.break_max)
                    print(f"  --- safety break {nap/60:.1f} min after {processed} leads ---")
                    status["state"] = "break"
                    push_status()
                    waited = 0.0
                    while waited < nap:
                        if _control_action(args.control_file) in ("stop", "stop_after"):
                            stopped = True
                            break
                        time.sleep(min(5.0, nap - waited))
                        waited += 5.0
                    status["state"] = "running"
                    if stopped:
                        print("  Stop requested during break; halting.")
                        break
    finally:
        flush_csv()
        if listings_mode and not args.no_write and rows:
            save_listings(listings_data)
        if status.get("state") not in ("blocked",):
            status["state"] = "stopped" if stopped else "done"
        push_status()
        if rows:
            print(f"\nWrote {len(rows)} rows -> {csv_path}")
        print(f"Processed {processed} | populated {wrote} lead(s) | "
              f"with phone: {sum(1 for r in rows if r['best_phone'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
