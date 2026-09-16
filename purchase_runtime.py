"""Purchase orchestration; installed into the Flask module after route definitions."""
# Kept in a separate file for readability; functions below receive the app module
# explicitly so tests can use isolated storage and fake payment/account backends.
import hashlib
import json
import os
import threading
import time
import commerce
from stripe import InvalidRequestError


def wallet_price(a, conn, data, county):
    subs = a.purchase_store.read(conn, "subscriptions", [])
    sub = next((s for s in subs if str(s.get("user_id")) == data["user_id"]
                and s.get("status") == "active" and str(s.get("county", "")).lower() == county), None)
    if not a._subscriptions_ready():
        sub = None
    data["covered"] = bool(sub)
    cost = 0
    for lead in data["leads"]:
        charge = int(lead["purchase_price_cents"])
        premium = max(0, lead["skip_price_cents"] - lead["raw_price_cents"]) if lead["purchase_mode"] == "skip" else 0
        if sub:
            charge = 0
            if lead["purchase_mode"] == "skip":
                used = int(sub.get("traces_used", 0))
                if used < a._tier_included(sub.get("tier", "professional")):
                    sub["traces_used"] = used + 1
                    lead["included_trace"] = {"subscription_id": sub["stripe_subscription_id"],
                                              "period": sub.get("period_start", "")}
                    premium = 0
                else:
                    charge = premium
        lead["charged_cents"] = charge
        lead["skiptrace_refund_cents"] = premium
        cost += charge
    data["amount_cents"] = cost
    return subs


def quote(a, user, payload):
    order = a.purchase_store.get(key=wallet_key(a, user, payload))
    if order:
        return {"amount_cents": order["amount_cents"], "covered": order.get("covered", False), "resume": True}
    _, selected, modes, county = selection(a, user, payload)
    data = {"user_id": str(user["id"]), "leads": a._prepare_purchased_leads(selected, modes)}
    with a.purchase_store.transaction() as conn:
        wallet_price(a, conn, data, county)
    return {"amount_cents": data["amount_cents"], "covered": data["covered"]}


def selection(a, user, payload=None):
    a._ensure_purchase_history()
    payload = a.request.get_json(silent=True) or {} if payload is None else payload
    if not isinstance(payload, dict) or not isinstance(payload.get("lead_ids"), list):
        raise ValueError("lead_ids must be a list")
    ids = {str(i) for i in payload["lead_ids"]}
    if not ids or len(ids) > 100:
        raise ValueError("Select between 1 and 100 leads")
    if not isinstance(payload.get("lead_modes", {}), dict):
        raise ValueError("lead_modes must be an object")
    selected = [it for it in a.publishable_storefront_listings(a.current_listings()) if str(it.get("id")) in ids]
    if len(selected) != len(ids):
        raise commerce.Unavailable("Some selected leads are already sold or reserved. Refresh the page.")
    counties = {str(it.get("county") or "").lower().strip() for it in selected}
    if len(counties) != 1 or "" in counties:
        raise ValueError("Select leads from one county")
    county = counties.pop()
    if a._subscriptions_ready() and county in a._claimed_counties(exclude_user=user["id"]):
        raise commerce.Unavailable("This county is reserved for its subscribers")
    return payload, selected, a._requested_lead_modes(payload, ids), county


def create_checkout(a, order):
    # The configured Railway service runs one process. Serialize the browser
    # and recovery worker so a validation rejection cannot race another create.
    with a._checkout_create_lock:
        current = a.purchase_store.get(key=order["id"])
        if current["state"] == "rejected":
            raise commerce.CheckoutRejected("Checkout details were rejected. Please review your account email and retry.")
        if current["session_id"]:
            cs = a.stripe.checkout.Session.retrieve(current["session_id"])
        else:
            try:
                cs = a.stripe.checkout.Session.create(**order["checkout_args"], idempotency_key=order["id"])
            except InvalidRequestError as exc:
                # Never release a reservation for ambiguous transport/server
                # errors or a concurrent idempotency-key conflict.
                if exc.http_status == 400 and exc.code in {
                    "email_invalid", "parameter_invalid_empty", "parameter_invalid_integer",
                    "parameter_invalid_string_blank", "parameter_missing", "parameter_unknown",
                } and exc.param != "expires_at":
                    a.purchase_store.reject_creation(order["id"])
                    raise commerce.CheckoutRejected("Checkout details were rejected. Please review your account email and retry.") from exc
                raise
            a.purchase_store.bind(order["id"], cs.id)
    # The local journal already contains the full snapshot if this service fails.
    a.db.init_db()
    a.db.create_pending_order(user_id=order["user_id"], email=order["email"], stripe_session_id=cs.id,
                              amount_cents=order.get("order_total_cents", order["amount_cents"]), leads=order["leads"])
    return cs


def deliver(a, order):
    if order["state"] == "complete":
        a._schedule_order_delivery_email(order["session_id"])
        return True
    if order["state"] != "ready":
        return False
    a.db.init_db()
    oid = a.db.create_pending_order(user_id=order["user_id"], email=order["email"],
        stripe_session_id=order["session_id"],
        amount_cents=order.get("order_total_cents", order["amount_cents"]), leads=order["leads"])
    if not oid or not a.db.mark_order_paid(order["session_id"]):
        raise RuntimeError("Account backend did not confirm order delivery")
    a._mark_leads_sold([it["id"] for it in order["leads"]])
    a.purchase_store.complete(order["id"])
    a._schedule_order_delivery_email(order["session_id"])
    return True


def wallet_key(a, user, payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("lead_ids"), list):
        raise ValueError("lead_ids must be a list")
    if not isinstance(payload.get("lead_modes", {}), dict):
        raise ValueError("lead_modes must be an object")
    ids = sorted({str(i) for i in payload["lead_ids"]})
    modes = a._requested_lead_modes(payload, ids)
    # An exclusive lead can only be purchased once; deterministic retry identity
    # works even when the browser loses the response and has no request token.
    return "credit_" + hashlib.sha256(json.dumps([str(user["id"]), ids, modes], sort_keys=True).encode()).hexdigest()[:32]


def wallet_purchase(a, user, payload):
    key = wallet_key(a, user, payload)
    order = a.purchase_store.get(key=key)
    if not order:
        _, selected, modes, county = selection(a, user, payload)
        prepared = a._prepare_purchased_leads(selected, modes)

        def price(conn, data):
            subs = wallet_price(a, conn, data, county)
            if "max_charge_cents" in payload and data["amount_cents"] > int(payload["max_charge_cents"]):
                raise commerce.Unavailable("The price or included allowance changed. Review the updated quote.")
            a.purchase_store.write(conn, "subscriptions", subs)

        order = a.purchase_store.reserve(key, {"kind": "wallet", "user_id": str(user["id"]),
            "email": user["email"], "amount_cents": 0, "leads": prepared}, wallet=True, prepare=price)
    deliver(a, order)
    return {"ok": True, "unlocked": len(order["leads"]), "charged_cents": order["amount_cents"],
            "balance_cents": a._wallet_balance_cents(user["id"]), "covered_by_subscription": order.get("covered", False)}


def safe_csv_path(a, filename):
    if not filename or os.path.basename(filename) != filename or "/" in filename or "\\" in filename:
        return None
    if not filename.lower().endswith(".csv"):
        return None
    root = os.path.realpath(a.DATA_DIR)
    path = os.path.realpath(os.path.join(root, filename))
    if os.path.commonpath([root, path]) != root or not os.path.isfile(path):
        return None
    return path


def process_job(a, job):
    if job["kind"] == "trace":
        return a._fulfill_order_skiptraces(job["id"][6:])
    if job["kind"] == "county_alert":
        return a._deliver_county_alert(job["id"].removeprefix("county-alert:"))
    if job["kind"] == "county_confirmation":
        request_id, request_count = job["id"].removeprefix("county-confirmation:").rsplit(":", 1)
        return a._deliver_county_confirmation(request_id, int(request_count))
    if job["kind"] == "order_email":
        return a._deliver_order_email(job["id"].removeprefix("order-email:"))
    order = a.purchase_store.get(key=job["id"])
    if not order or order["state"] in {"complete", "expired", "rejected"}:
        return True
    if order["state"] == "ready":
        return deliver(a, order)
    if not a.stripe.api_key:
        return False
    if order["state"] == "creating":
        create_checkout(a, order)
        order = a.purchase_store.get(key=order["id"])
    cs = a._stripe_mapping(a.stripe.checkout.Session.retrieve(order["session_id"]))
    if cs.get("payment_status") == "paid":
        return a._fulfill_session(cs["id"])
    if cs.get("status") == "expired":
        a.purchase_store.expire(order["id"])
        return True
    return False


def install(a):
    a._checkout_create_lock = threading.Lock()
    a._purchase_selection = lambda user, payload=None: selection(a, user, payload)
    a._create_reserved_checkout = lambda order: create_checkout(a, order)
    a._deliver_purchase = lambda order: deliver(a, order)
    a._wallet_purchase = lambda user, payload: wallet_purchase(a, user, payload)
    a._safe_csv_path = lambda filename: safe_csv_path(a, filename)
    lock = threading.Lock()
    started = False
    history_lock = threading.Lock()

    def ensure_history():
        with history_lock:
            if a._sqlite_get("commerce_history_v2", False):
                return
            # Import paid inventory before accepting any new purchase. This also
            # repairs old Postgres sales whose sold markers were never written.
            orders = a.db.get_paid_orders()
            a.purchase_store.import_paid(orders)
            a._mark_leads_sold([it["id"] for order in orders for it in order.get("leads_json") or []])
    a._ensure_purchase_history = ensure_history

    def worker():
        try:
            a._enqueue_ready_county_alerts()
        except Exception:
            a.app.logger.exception("Could not enqueue pending county alerts")
        while True:
            try:
                if a.db.is_configured():
                    ensure_history()
                job = a.purchase_store.take_job()
                if job:
                    stop = threading.Event()
                    def heartbeat():
                        while not stop.wait(25):
                            try:
                                a.purchase_store.renew_job(job)
                            except Exception:
                                a.app.logger.exception("Could not renew fulfillment lease")
                    pulse = threading.Thread(target=heartbeat, daemon=True)
                    pulse.start()
                    try:
                        success = process_job(a, job)
                    except Exception:
                        a.app.logger.exception("Durable fulfillment job failed: %s", job["id"])
                        success = False
                    finally:
                        stop.set()
                        pulse.join(timeout=1)
                    a.purchase_store.finish_job(job["id"], success, job["attempts"] + 1)
            except Exception:
                a.app.logger.exception("Fulfillment worker storage unavailable")
            time.sleep(2)

    @a.app.before_request
    def start_worker():
        nonlocal started
        if a.app.testing or not a._persistent_storage_ready() or os.getenv("DISABLE_FULFILLMENT_WORKER") == "1":
            return
        with lock:
            if not started:
                threading.Thread(target=worker, name="purchase-recovery", daemon=True).start()
                started = True
