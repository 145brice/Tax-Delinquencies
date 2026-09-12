# Lead pricing and signup offer

`LEAD_PRICING_PHASE=beta` is the default. Set it to `post_beta` and restart
the app to switch phases. Legacy `LEAD_PRICE_RAW`, `LEAD_PRICE_TRACED`, and
`SIGNUP_GRANT_CENTS` overrides no longer control this pricing or signup offer.

Prices in dollars, rounded to cents:

| Age in days | Multiplier | Beta raw | Beta skip-traced | Post-beta raw | Post-beta skip-traced |
|---|---:|---:|---:|---:|---:|
| 0–3 | 100% | 6.00 | 15.00 | 18.00 | 30.00 |
| 4–7 | 85% | 5.10 | 12.75 | 15.30 | 25.50 |
| 8–14 | 65% | 3.90 | 9.75 | 11.70 | 19.50 |
| 15–30 | 45% | 2.70 | 6.75 | 8.10 | 13.50 |
| 31–60 | 25% | 2.00 | 3.75 | 4.50 | 7.50 |
| 61+ | 10% | 2.00 | 3.50 | 3.00 | 5.00 |

Floors apply throughout the ladder to preserve both the hard minimum and
non-increasing prices. In particular, beta raw leads remain $2 at 31–60 days
instead of dropping to $1.50 and then rising to $2 at day 61.

Age uses UTC calendar days since the earliest valid `first_seen`,
`first_seen_at`, or `scraped_date`. Re-scrape merges preserve that earliest
date. Sale dates are not discovery dates. Missing or invalid discovery dates
use the lower-middle 45% tier and remain ineligible for the promo; future dates
count as age 0.

Storefront prices, Stripe checkout totals, and wallet unlocks share the same
engine. The static marketing page lists beta and post-beta pricing explicitly.

## Signup offer

“Get up to 3 free aged leads when you join, when available.”

New registrations receive an allowance of three leads, not wallet money.
The storefront claim button chooses available inventory older than 60 days,
raw leads first and oldest first within each contact category. No younger
inventory is substituted. A partial claim preserves the unused allowance.
Leads reserved for other county subscribers are excluded.

Claims create zero-dollar orders visible in the account page. Durable
reservations and a stable order ID allow failed claims to resume. Reserved
leads are excluded from the storefront and subsequent purchase requests.
Existing wallet balances are preserved; existing accounts do not receive a
second signup offer automatically.

Allowances and reservations live in the SQLite `signup_promos` table. Use
the same persistent Railway volume as the app's existing SQLite data store.

Run checks with `python -m unittest discover -s tests -v`. Tests use isolated
storage and mocked payment/account services; they do not charge cards or
modify production inventory.
