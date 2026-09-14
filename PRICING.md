# Lead pricing and signup offer

`LEAD_PRICING_PHASE=beta` is the default. Set it to `post_beta` and restart
the app to switch phases. Legacy `LEAD_PRICE_RAW`, `LEAD_PRICE_TRACED`, and
`SIGNUP_GRANT_CENTS` overrides no longer control this pricing or signup offer.

All prices are whole dollars:

| Age in days | Beta raw | Beta skip-traced | Post-beta raw | Post-beta skip-traced |
|---|---:|---:|---:|---:|
| 0–3 | $6 | $15 | $18 | $30 |
| 4–7 | $5 | $13 | $15 | $26 |
| 8–14 | $4 | $10 | $12 | $20 |
| 15–30 | $3 | $7 | $8 | $14 |
| 31–60 | $2 | $4 | $5 | $8 |
| 61+ | $2 | $4 | $3 | $5 |

Age uses UTC calendar days since the earliest valid `first_seen`,
`first_seen_at`, or `scraped_date`. Re-scrape merges preserve that earliest
date. Sale dates are not discovery dates. Missing or invalid discovery dates
use the 15–30 day price and remain ineligible for the signup offer; future
dates count as age 0.

Storefront prices, Stripe checkout totals, and wallet unlocks share the same
engine. When wallet credit does not cover an order, the buyer can apply the
available balance and pay the remainder by card. The wallet reservation is
restored exactly once if Stripe definitively rejects or expires the checkout.

## Signup offer

“Get up to 3 free aged leads when you join, when available.”

New registrations receive an allowance of three leads, not wallet money.
The storefront claim button chooses available inventory older than 60 days,
raw leads first and oldest first within each contact category. No younger
inventory is substituted. A partial claim preserves the unused allowance.

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
