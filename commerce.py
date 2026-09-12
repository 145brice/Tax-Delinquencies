"""Durable purchase journal and exclusive inventory, on the Railway SQLite volume.

External account/Stripe writes are retried from this journal. A committed wallet
debit always has a recoverable order; a lead has exactly one owning order.
"""
import json
import re
import time
from contextlib import closing, contextmanager


class Unavailable(ValueError):
    pass


class InsufficientCredit(ValueError):
    pass


class CheckoutRejected(ValueError):
    pass


def identity_keys(lead):
    """Conservative property aliases independent of scraped dates or listing IDs."""
    norm = lambda value: re.sub(r"[^a-z0-9]", "", str(value or "").lower())
    keys = {"id:" + str(lead.get("id") or "")}
    county = norm(lead.get("county"))
    address = str(lead.get("address") or "")
    state = norm(lead.get("state"))
    if not state:
        match = re.search(r",\s*([A-Za-z]{2})(?:[ ,]+\d{5}(?:-\d{4})?)?\s*$", address)
        state = norm(match[1]) if match else ""
    scope = county + ":" + state
    parcel = norm(lead.get("parcel_id"))
    if county and parcel and state:
        # Preserve letters in parcel IDs; AB-12 and CD-12 are different parcels.
        keys.add("parcel:" + scope + ":" + parcel)
    if county and re.match(r"\s*\d+\s+\S", address):
        keys.add("address:" + scope + ":" + norm(address))
    elif county and not parcel and norm(lead.get("case_number")):
        keys.add("case:" + scope + ":" + norm(lead["case_number"]))
    return keys


class Store:
    def __init__(self, connect):
        self.connect = connect

    @contextmanager
    def transaction(self):
        with closing(self.connect()) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS purchase_journal (id TEXT PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL, session_id TEXT UNIQUE, updated REAL NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS lead_claims (lead_id TEXT PRIMARY KEY, order_id TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS property_claims (identity TEXT PRIMARY KEY, order_id TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS commerce_events (id TEXT PRIMARY KEY)")
            conn.execute("CREATE TABLE IF NOT EXISTS fulfillment_jobs (id TEXT PRIMARY KEY, kind TEXT NOT NULL, due REAL NOT NULL, lease_until REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0)")
            conn.execute("BEGIN IMMEDIATE")
            yield conn

    @staticmethod
    def read(conn, key, default):
        row = conn.execute("SELECT value FROM app_json WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def write(conn, key, value):
        conn.execute("INSERT INTO app_json VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                     (key, json.dumps(value), str(time.time())))

    def wallet_change(self, conn, uid, delta, event):
        wallets = self.read(conn, "credit_wallets", {})
        entry = wallets.get(str(uid), {"balance_cents": 0, "ledger": []})
        if conn.execute("SELECT 1 FROM commerce_events WHERE id=?", (event,)).fetchone():
            return int(entry["balance_cents"]), False
        balance = int(entry["balance_cents"]) + delta
        if balance < 0:
            raise InsufficientCredit("Not enough wallet credit")
        conn.execute("INSERT INTO commerce_events VALUES (?)", (event,))
        entry["balance_cents"] = balance
        entry.setdefault("ledger", []).append({"delta_cents": delta, "reason": event,
                                               "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                               "balance_after": balance})
        entry["ledger"] = entry["ledger"][-100:]
        wallets[str(uid)] = entry
        self.write(conn, "credit_wallets", wallets)
        return balance, True

    def credit(self, uid, cents, event):
        if cents < 0:
            raise ValueError("Credit must be nonnegative")
        with self.transaction() as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='wallet_credit_events'").fetchone():
                if conn.execute("SELECT 1 FROM wallet_credit_events WHERE event_id=?", (event,)).fetchone():
                    balance = self.read(conn, "credit_wallets", {}).get(str(uid), {}).get("balance_cents", 0)
                    return int(balance), False
            return self.wallet_change(conn, uid, cents, event)

    def credit_recorded(self, event):
        with self.transaction() as conn:
            if conn.execute("SELECT 1 FROM commerce_events WHERE id=?", (event,)).fetchone():
                return True
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='wallet_credit_events'").fetchone():
                return bool(conn.execute("SELECT 1 FROM wallet_credit_events WHERE event_id=?", (event,)).fetchone())
            return False

    def get(self, key=None, session_id=None):
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM purchase_journal WHERE " + ("id=?" if key else "session_id=?"),
                               (key or session_id,)).fetchone()
            return self.decode(row)

    @staticmethod
    def decode(row):
        return {**json.loads(row["payload"]), "id": row["id"], "state": row["state"],
                "session_id": row["session_id"]} if row else None

    @staticmethod
    def enqueue(conn, key, kind):
        conn.execute("INSERT OR IGNORE INTO fulfillment_jobs (id, kind, due) VALUES (?, ?, ?)",
                     (key, kind, time.time()))

    def reserve(self, key, payload, *, wallet=False, prepare=None):
        with self.transaction() as conn:
            prior = conn.execute("SELECT * FROM purchase_journal WHERE id=?", (key,)).fetchone()
            if prior:
                return self.decode(prior)
            # Recheck authoritative sold markers while holding the write lock.
            sold = set()
            for it in self.read(conn, "listings", []):
                if it.get("sold_at"):
                    sold.update(identity_keys(it))
            promo = set()
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='signup_promos'").fetchone():
                for row in conn.execute("SELECT payload FROM signup_promos"):
                    promo.update(json.loads(row[0]).get("reserved_ids", []))
            for lead in payload["leads"]:
                lid = str(lead["id"])
                aliases = identity_keys(lead)
                if aliases & sold or lid in promo or conn.execute("SELECT 1 FROM lead_claims WHERE lead_id=?", (lid,)).fetchone():
                    raise Unavailable("A selected lead is already sold or reserved")
                for alias in aliases:
                    if conn.execute("SELECT 1 FROM property_claims WHERE identity=?", (alias,)).fetchone():
                        raise Unavailable("This property is already sold, reserved, or duplicated in your selection")
                    conn.execute("INSERT INTO property_claims VALUES (?, ?)", (alias, key))
                conn.execute("INSERT INTO lead_claims VALUES (?, ?)", (lid, key))
            if prepare:
                prepare(conn, payload)
            if wallet:
                self.wallet_change(conn, payload["user_id"], -payload["amount_cents"], "purchase:" + key)
            state = "ready" if wallet or payload.get("kind") == "promo" else "creating"
            sid = key if state == "ready" else None
            conn.execute("INSERT INTO purchase_journal VALUES (?, ?, ?, ?, ?)",
                         (key, json.dumps(payload), state, sid, time.time()))
            self.enqueue(conn, key, "purchase")
            return {**payload, "id": key, "state": state, "session_id": sid}

    def bind(self, key, session_id):
        with self.transaction() as conn:
            conn.execute("UPDATE purchase_journal SET session_id=?, state='awaiting', updated=? WHERE id=? AND state='creating'",
                         (session_id, time.time(), key))

    def paid(self, key):
        with self.transaction() as conn:
            conn.execute("UPDATE purchase_journal SET state='ready', updated=? WHERE id=? AND state IN ('awaiting', 'creating')", (time.time(), key))

    def complete(self, key):
        with self.transaction() as conn:
            row = conn.execute("SELECT session_id FROM purchase_journal WHERE id=?", (key,)).fetchone()
            conn.execute("UPDATE purchase_journal SET state='complete', updated=? WHERE id=?", (time.time(), key))
            conn.execute("DELETE FROM fulfillment_jobs WHERE id=?", (key,))
            self.enqueue(conn, "trace:" + row[0], "trace")

    def expire(self, key):
        """Only call after Stripe confirms expiration, never merely on a timer."""
        with self.transaction() as conn:
            row = conn.execute("SELECT state FROM purchase_journal WHERE id=?", (key,)).fetchone()
            if row and row[0] == "awaiting":
                conn.execute("DELETE FROM lead_claims WHERE order_id=?", (key,))
                conn.execute("DELETE FROM property_claims WHERE order_id=?", (key,))
                conn.execute("UPDATE purchase_journal SET state='expired' WHERE id=?", (key,))
                conn.execute("DELETE FROM fulfillment_jobs WHERE id=?", (key,))

    def reject_creation(self, key):
        """Release a checkout only after a definitive pre-execution rejection."""
        with self.transaction() as conn:
            changed = conn.execute("UPDATE purchase_journal SET state='rejected' WHERE id=? AND state='creating'", (key,)).rowcount
            if changed:
                conn.execute("DELETE FROM lead_claims WHERE order_id=?", (key,))
                conn.execute("DELETE FROM property_claims WHERE order_id=?", (key,))
                conn.execute("DELETE FROM fulfillment_jobs WHERE id=?", (key,))

    def claimed_ids(self):
        with self.transaction() as conn:
            return {row[0] for row in conn.execute("SELECT lead_id FROM lead_claims")}

    def claimed_properties(self):
        with self.transaction() as conn:
            return {row[0] for row in conn.execute("SELECT identity FROM property_claims")}

    def import_paid(self, orders):
        with self.transaction() as conn:
            for order in orders:
                sid = order["stripe_session_id"]
                for lead in order.get("leads_json") or []:
                    conn.execute("INSERT OR IGNORE INTO lead_claims VALUES (?, ?)", (str(lead["id"]), sid))
                    for alias in identity_keys(lead):
                        conn.execute("INSERT OR IGNORE INTO property_claims VALUES (?, ?)", (alias, sid))
                if any(it.get("purchase_mode") == "skip" and it.get("skiptrace_status") not in {"completed", "failed_credited"}
                       for it in order.get("leads_json") or []):
                    self.enqueue(conn, "trace:" + sid, "trace")
            # Also backfill reservations from the first journal version.
            for row in conn.execute("SELECT id, payload FROM purchase_journal WHERE state NOT IN ('expired', 'rejected')"):
                for lead in json.loads(row["payload"])["leads"]:
                    for alias in identity_keys(lead):
                        conn.execute("INSERT OR IGNORE INTO property_claims VALUES (?, ?)", (alias, row["id"]))
            for lead in self.read(conn, "listings", []):
                if lead.get("sold_at"):
                    for alias in identity_keys(lead):
                        conn.execute("INSERT OR IGNORE INTO property_claims VALUES (?, ?)", (alias, "legacy-sold:" + str(lead["id"])))
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='signup_promos'").fetchone():
                for row in conn.execute("SELECT payload FROM signup_promos"):
                    pending = json.loads(row[0]).get("pending") or {}
                    for lead in pending.get("leads", []):
                        for alias in identity_keys(lead):
                            conn.execute("INSERT OR IGNORE INTO property_claims VALUES (?, ?)", (alias, pending["session_id"]))
            self.write(conn, "commerce_history_v2", True)

    def adopt_promo(self, uid, email, state, leads):
        key = state["pending"]["session_id"]
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM purchase_journal WHERE id=?", (key,)).fetchone()
            if row:
                return self.decode(row)
            for lead in leads:
                claim = conn.execute("SELECT order_id FROM lead_claims WHERE lead_id=?", (str(lead["id"]),)).fetchone()
                if claim and claim[0] != key:
                    raise Unavailable("Legacy promo lead is already owned")
                conn.execute("INSERT OR IGNORE INTO lead_claims VALUES (?, ?)", (str(lead["id"]), key))
                for alias in identity_keys(lead):
                    owner = conn.execute("SELECT order_id FROM property_claims WHERE identity=?", (alias,)).fetchone()
                    if owner and owner[0] != key:
                        raise Unavailable("Legacy promo property is already owned")
                    conn.execute("INSERT OR IGNORE INTO property_claims VALUES (?, ?)", (alias, key))
            payload = {"kind": "promo", "user_id": str(uid), "email": email, "leads": leads, "amount_cents": 0}
            conn.execute("INSERT INTO purchase_journal VALUES (?, ?, 'ready', ?, ?)", (key, json.dumps(payload), key, time.time()))
            state.pop("pending", None)
            state["pending_key"] = key
            conn.execute("UPDATE signup_promos SET payload=? WHERE user_id=?", (json.dumps(state), str(uid)))
            self.enqueue(conn, key, "purchase")
            return {**payload, "id": key, "session_id": key, "state": "ready"}

    def add_job(self, key, kind):
        with self.transaction() as conn:
            self.enqueue(conn, key, kind)

    def take_job(self):
        with self.transaction() as conn:
            now = time.time()
            row = conn.execute("SELECT * FROM fulfillment_jobs WHERE due<=? AND lease_until<=? ORDER BY due LIMIT 1", (now, now)).fetchone()
            if not row:
                return None
            conn.execute("UPDATE fulfillment_jobs SET lease_until=?, attempts=attempts+1 WHERE id=?", (now + 90, row["id"]))
            return dict(row)

    def renew_job(self, job):
        with self.transaction() as conn:
            conn.execute("UPDATE fulfillment_jobs SET lease_until=? WHERE id=? AND attempts=?",
                         (time.time() + 90, job["id"], job["attempts"] + 1))

    def finish_job(self, key, success, attempt=None):
        with self.transaction() as conn:
            if attempt is not None:
                row = conn.execute("SELECT attempts FROM fulfillment_jobs WHERE id=?", (key,)).fetchone()
                if not row or row[0] != attempt:
                    return
            if success:
                conn.execute("DELETE FROM fulfillment_jobs WHERE id=?", (key,))
            else:
                conn.execute("UPDATE fulfillment_jobs SET due=?, lease_until=0 WHERE id=?", (time.time() + 30, key))
