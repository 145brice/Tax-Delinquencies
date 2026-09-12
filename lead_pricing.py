"""Age-based lead prices, independent of account promotions."""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

BASE_CENTS = {"beta": (600, 1500), "post_beta": (1800, 3000)}
FLOOR_CENTS = {"beta": (200, 350), "post_beta": (300, 500)}
TIERS = ((3, 100), (7, 85), (14, 65), (30, 45), (60, 25), (None, 10))


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


def price_cents(item, traced=False, phase="beta", today=None):
    age = age_days(item, today)
    base = BASE_CENTS[phase][bool(traced)]
    floor = FLOOR_CENTS[phase][bool(traced)]
    # Applying the final floor throughout prevents an increase at day 61.
    previous = base
    for limit, percent in TIERS:
        amount = int((Decimal(base) * percent / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        previous = min(previous, max(amount, floor))
        if age is None or limit is None or age <= limit:
            return previous
