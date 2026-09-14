"""Age-based lead prices, independent of account promotions."""
from datetime import datetime, timezone

BASE_CENTS = {"beta": (600, 1500), "post_beta": (1800, 3000)}
FLOOR_CENTS = {"beta": (200, 400), "post_beta": (300, 500)}
TIERS = (3, 7, 14, 30, 60, None)
PRICE_TIERS_CENTS = {
    "beta": ((600, 500, 400, 300, 200, 200),
             (1500, 1300, 1000, 700, 400, 400)),
    "post_beta": ((1800, 1500, 1200, 800, 500, 300),
                  (3000, 2600, 2000, 1400, 800, 500)),
}
UNKNOWN_AGE_TIER = 3


def discovery_date(item):
    dates = []
    for field in ("first_seen", "first_seen_at", "scraped_date"):
        try:
            value = datetime.fromisoformat(str(item.get(field) or "").replace("Z", "+00:00"))
            if value.tzinfo:
                value = value.astimezone(timezone.utc)
            dates.append(value.date())
        except (ValueError, TypeError):
            pass
    return min(dates) if dates else None


def age_days(item, today=None):
    discovered = discovery_date(item)
    if discovered is None:
        return None
    return max(0, ((today or datetime.now(timezone.utc).date()) - discovered).days)


def normalize_listing_dates(item):
    """Separate acquisition dates from event dates on legacy stored listings.

    Older rows used ``date`` as the acquisition date when neither explicit
    date field existed. Never copy it when ``sale_date`` is present because in
    that shape it may be an auction, filing, or publication date.
    """
    changed = False
    if not str(item.get("scraped_date") or "").strip() and not str(item.get("sale_date") or "").strip():
        legacy = discovery_date({"scraped_date": item.get("date")})
        if legacy:
            item["scraped_date"] = legacy.isoformat()
            changed = True
    discovered = discovery_date(item)
    if discovered and not str(item.get("first_seen") or "").strip():
        item["first_seen"] = discovered.isoformat()
        changed = True
    return changed


def price_cents(item, traced=False, phase="beta", today=None):
    age = age_days(item, today)
    if age is None:
        tier = UNKNOWN_AGE_TIER
    else:
        tier = next(i for i, limit in enumerate(TIERS) if limit is None or age <= limit)
    return PRICE_TIERS_CENTS[phase][bool(traced)][tier]
