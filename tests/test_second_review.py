import copy
import json
from concurrent.futures import ThreadPoolExecutor
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, MagicMock, patch
from stripe import InvalidRequestError

import db
import test_purchase_reliability as fixtures
import os


class ReviewTests(unittest.TestCase):
    setUp = fixtures.PurchaseTests.setUp

    def test_postgres_history_includes_session_identifier(self):
        conn = MagicMock()
        conn.__enter__.return_value = conn
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = []
        with patch.object(db, "_use_sqlite", return_value=False), patch.object(db, "_use_appwrite", return_value=False), patch.object(db, "get_conn", return_value=conn):
            db.get_paid_orders()
        self.assertIn("stripe_session_id", cursor.execute.call_args.args[0])

    def test_browser_can_quote_then_resume_interrupted_wallet_order(self):
        self.a.db.mark_order_paid.side_effect = [False, True]
        payload = {"lead_ids": ["10"]}
        self.assertEqual(self.client.post("/api/unlock", json=payload).status_code, 503)
        quote = self.client.post("/api/unlock/quote", json=payload)
        self.assertEqual(quote.status_code, 200)
        self.assertTrue(quote.json["resume"])
        self.assertEqual(self.client.post("/api/unlock", json=payload).status_code, 200)
        self.assertEqual(self.a._wallet_balance_cents("buyer"), 9800)

    def test_same_property_with_new_listing_id_cannot_be_sold_again(self):
        self.assertEqual(self.client.post("/api/unlock", json={"lead_ids": ["10"]}).status_code, 200)
        new = {**self.leads[0], "id": "new-id", "scraped_date": "2026-09-12"}
        self.a._sqlite_set("listings", [new])
        self.assertEqual(self.client.post("/api/unlock", json={"lead_ids": ["new-id"]}).status_code, 409)

    def test_same_property_two_ids_in_one_cart_is_rejected(self):
        duplicate = {**self.leads[0], "id": "duplicate"}
        self.a._sqlite_set("listings", [self.leads[0], duplicate])
        response = self.client.post("/api/unlock", json={"lead_ids": ["10", "duplicate"]})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.a._wallet_balance_cents("buyer"), 10000)

    def test_revisiting_subscription_checkout_uses_current_paid_tier(self):
        self.a.TIER_PRICES = {"starter": "price_s", "professional": "price_p", "power": "price_w"}
        self.a.stripe.Subscription = SimpleNamespace(retrieve=Mock(return_value={"status": "active",
            "items": {"data": [{"price": {"id": "price_w"}, "current_period_end": int(time.time()) + 1000}]}}))
        self.assertTrue(self.a._activate_subscription("sub", "cus", "buyer", "test", tier="starter"))
        self.assertEqual(self.a._load_subs()[0]["tier"], "power")

    def test_definitively_rejected_checkout_releases_inventory(self):
        self.a.stripe.checkout.Session.create.side_effect = InvalidRequestError(
            "Invalid email", "customer_email", code="email_invalid", http_status=400)
        response = self.client.post("/api/create-checkout-session", json={"lead_ids": ["10"]})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("10", self.a.purchase_store.claimed_ids())
        self.assertFalse(self.a.purchase_store.claimed_properties())

    def test_ambiguous_checkout_failure_keeps_reservation(self):
        self.a.stripe.checkout.Session.create.side_effect = ConnectionError("response lost")
        response = self.client.post("/api/create-checkout-session", json={"lead_ids": ["10"]})
        self.assertEqual(response.status_code, 503)
        self.assertIn("10", self.a.purchase_store.claimed_ids())

    def test_subscription_status_patch_preserves_other_records_and_usage(self):
        self.a._sqlite_set("subscriptions", [
            {"stripe_subscription_id": "first", "status": "active", "traces_used": 5, "last_renewal": 900},
            {"stripe_subscription_id": "second", "status": "canceled", "traces_used": 2}])
        self.a._patch_subscription("first", {"tier": "power", "traces_used": 0, "last_renewal": 0})
        records = self.a._load_subs()
        self.assertEqual(records[0]["traces_used"], 5)
        self.assertEqual(records[0]["last_renewal"], 900)
        self.assertEqual(records[1]["status"], "canceled")

    def test_rescrape_merges_new_id_and_preserves_sold_status(self):
        old = {**self.leads[0], "sold_at": "2026-09-01"}
        new = {**self.leads[0], "id": "new-id", "scraped_date": "2026-09-12"}
        merged, added = self.a.merge_listings([old], [new])
        self.assertEqual(added, 0)
        self.assertEqual(merged[0]["id"], old["id"])
        self.assertEqual(merged[0]["sold_at"], old["sold_at"])

    def test_parcel_identity_preserves_state_and_letters(self):
        base = {**self.leads[0], "county": "orange", "state": "CA", "parcel_id": "AB-12"}
        key = self.a._parcel_date_key(base)
        self.assertNotEqual(key, self.a._parcel_date_key({**base, "state": "FL"}))
        self.assertNotEqual(key, self.a._parcel_date_key({**base, "parcel_id": "CD-12"}))
        raw = {"county": "Orange", "state": "CA", "parcel_id": "AB-12", "property_address": "123 Main Street"}
        ca = self.a.property_records_to_listings([raw])[0]
        fl = self.a.property_records_to_listings([{**raw, "state": "FL"}])[0]
        self.assertNotEqual(ca["id"], fl["id"])

    def test_refund_decision_survives_failed_remote_status_write(self):
        self.client.post("/api/unlock", json={"lead_ids": ["10"], "lead_modes": {"10": "skip"}})
        sid = next(iter(self.orders))
        update = self.a.db.update_order_lead_contacts.side_effect
        calls = []
        def fail_once(*args):
            calls.append(args)
            return False if len(calls) == 1 else update(*args)
        self.a.db.update_order_lead_contacts.side_effect = fail_once
        with patch("scrapers.skiptrace_search.lookup", side_effect=[{"phones": [], "emails": []}, {"phones": ["5551234567"], "emails": []}]) as lookup:
            self.assertFalse(self.a._fulfill_order_skiptraces(sid))
            self.assertTrue(self.a._fulfill_order_skiptraces(sid))
            self.assertEqual(lookup.call_count, 1)
        self.assertEqual(self.orders[sid]["leads_json"][0]["skiptrace_status"], "failed_credited")
        self.assertEqual(self.a._wallet_balance_cents("buyer"), 9800)

    def test_untraceable_owner_gets_refund_not_infinite_retry(self):
        self.client.post("/api/unlock", json={"lead_ids": ["10"], "lead_modes": {"10": "skip"}})
        sid = next(iter(self.orders))
        with patch("scrapers.skiptrace_search.lookup", return_value={"phones": [], "emails": [], "error": "no usable owner name"}):
            self.assertTrue(self.a._fulfill_order_skiptraces(sid))
        self.assertEqual(self.a._wallet_balance_cents("buyer"), 9800)

    def test_contact_completion_does_not_overwrite_buyer_notes(self):
        document = {"$id": "order", "user_id": "buyer", "status": "paid", "leads_json": json.dumps([{"id": "lead"}])}
        def request(method, path, **kwargs):
            if method == "GET":
                snapshot = copy.deepcopy(document)
                time.sleep(0.025)
                return snapshot
            document.update(kwargs["data"]["data"])
            return copy.deepcopy(document)
        with patch.object(db, "_use_sqlite", return_value=False), patch.object(db, "_use_appwrite", return_value=True), patch.object(db, "_appwrite_request", side_effect=request):
            with ThreadPoolExecutor(2) as pool:
                a = pool.submit(db.update_order_lead_contacts, "order", "lead", {"primary_phone": "5551234567"})
                b = pool.submit(db.update_order_lead_tracking, "order", "buyer", "lead", {"buyer_notes": "Call tomorrow"})
                self.assertTrue(a.result())
                self.assertTrue(b.result())
        lead = json.loads(document["leads_json"])[0]
        self.assertEqual(lead["primary_phone"], "5551234567")
        self.assertEqual(lead["buyer_notes"], "Call tomorrow")

    def test_railway_sqlite_account_and_order_backend(self):
        self.assertEqual(db.backend_name(), "sqlite")
        user = db.create_user("buyer@example.com", "password-hash")
        self.assertEqual(db.get_user_by_email("BUYER@example.com")["id"], user["id"])
        order_id = db.create_pending_order(user["id"], user["email"], "cs_sqlite", 600,
                                           [{"id": "lead", "address": "123 Main"}])
        self.assertTrue(db.mark_order_paid("cs_sqlite"))
        self.assertEqual(db.get_order_by_session("cs_sqlite")["id"], order_id)
        self.assertEqual(db.get_order_leads("cs_sqlite")[0]["address"], "123 Main")
        self.assertTrue(db.update_order_lead_contacts(order_id, "lead", {"primary_phone": "5551234567"}))
        self.assertTrue(db.update_order_lead_tracking(order_id, user["id"], "lead", {"buyer_notes": "Call"}))
        lead = db.get_paid_orders_for_user(user["id"])[0]["leads_json"][0]
        self.assertEqual(lead["primary_phone"], "5551234567")
        self.assertEqual(lead["buyer_notes"], "Call")

    def test_google_identity_uses_immutable_subject_and_links_verified_email(self):
        user = db.create_user("buyer@example.com", "password-hash")
        linked, created = db.get_or_create_oauth_user("google", "google-sub-1", "buyer@example.com")
        self.assertFalse(created)
        self.assertEqual(linked["id"], user["id"])
        same, created = db.get_or_create_oauth_user("google", "google-sub-1", "changed@example.com")
        self.assertFalse(created)
        self.assertEqual(same["id"], user["id"])
        with self.assertRaises(ValueError):
            db.get_or_create_oauth_user("google", "different-google-sub", "buyer@example.com")

    def test_google_callback_creates_session_and_rejects_unverified_email(self):
        os.environ["GOOGLE_OAUTH_CLIENT_ID"] = "client-id"
        os.environ["GOOGLE_OAUTH_CLIENT_SECRET"] = "client-secret"
        self.a.current_user = lambda: None
        self.a.db = SimpleNamespace(backend_name=lambda: "sqlite", is_configured=lambda: True,
            appwrite_configured=lambda: False,
            get_or_create_oauth_user=Mock(return_value=({"id": "google-user", "email": "buyer@example.com"}, True)))
        self.a.google_oauth = SimpleNamespace(authorize_access_token=Mock(return_value={"userinfo": {
            "sub": "google-sub", "email": "buyer@example.com", "email_verified": True}}))
        with self.client.session_transaction() as sess:
            sess["oauth_next"] = "//evil.example"
        response = self.client.get("/auth/google/callback")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/account")
        with self.client.session_transaction() as sess:
            self.assertEqual(sess["user_id"], "google-user")

        self.a.google_oauth.authorize_access_token.return_value = {"userinfo": {
            "sub": "other", "email": "unverified@example.com", "email_verified": False}}
        response = self.client.get("/auth/google/callback")
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("unverified@example.com", response.get_data(as_text=True))

    def test_google_login_uses_state_nonce_and_safe_return_path(self):
        os.environ["GOOGLE_OAUTH_CLIENT_ID"] = "client-id"
        os.environ["GOOGLE_OAUTH_CLIENT_SECRET"] = "client-secret"
        self.a.current_user = lambda: None
        self.a.db.backend_name = lambda: "sqlite"
        authorize = Mock(side_effect=lambda uri, **kwargs: self.a.redirect("https://accounts.google.test/auth"))
        self.a.google_oauth = SimpleNamespace(authorize_redirect=authorize)
        response = self.client.get("/auth/google?next=//evil.example")
        self.assertEqual(response.status_code, 302)
        redirect_uri = authorize.call_args.args[0]
        self.assertEqual(redirect_uri, "http://localhost/auth/google/callback")
        self.assertTrue(authorize.call_args.kwargs["nonce"])
        with self.client.session_transaction() as sess:
            self.assertEqual(sess["oauth_next"], "/account")

    def test_async_payment_failure_releases_reserved_property(self):
        self.client.post("/api/create-checkout-session", json={"lead_ids": ["10"]})
        cs = next(iter(self.sessions.values()))
        os.environ["STRIPE_WEBHOOK_SECRET"] = "test"
        self.a.stripe.Webhook.construct_event.return_value = {"id": "evt_failed", "type": "checkout.session.async_payment_failed",
            "data": {"object": {"id": cs.id}}}
        response = self.client.post("/webhook/stripe")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("10", self.a.purchase_store.claimed_ids())

    def test_subscription_lifecycle_events_preserve_exact_status(self):
        os.environ["STRIPE_WEBHOOK_SECRET"] = "test"
        self.a.TIER_PRICES = {"starter": "price_s", "professional": "price_p", "power": "price_w"}
        sub = {"id": "sub_new", "customer": "cus", "status": "paused",
            "metadata": {"kind": "county_subscription", "user_id": "buyer", "county": "test"},
            "items": {"data": [{"price": {"id": "price_s"}, "current_period_end": int(time.time()) + 1000}]}}
        self.a.stripe.Subscription = SimpleNamespace(retrieve=Mock(return_value=sub))
        self.a.stripe.Webhook.construct_event.return_value = {"id": "evt_created", "type": "customer.subscription.created",
            "data": {"object": {"id": "sub_new"}}}
        self.assertEqual(self.client.post("/webhook/stripe").status_code, 200)
        self.assertEqual(self.a._load_subs()[0]["status"], "paused")
        sub["status"] = "past_due"
        self.a.stripe.Webhook.construct_event.return_value = {"id": "evt_updated", "type": "customer.subscription.updated",
            "data": {"object": {"id": "sub_new"}}}
        self.assertEqual(self.client.post("/webhook/stripe").status_code, 200)
        self.assertEqual(self.a._load_subs()[0]["status"], "past_due")
        self.assertEqual(self.a._user_active_sub_counties("buyer"), set())
        sub["status"] = "active"
        self.a.stripe.Webhook.construct_event.return_value = {"id": "evt_resumed", "type": "customer.subscription.resumed",
            "data": {"object": {"id": "sub_new"}}}
        self.assertEqual(self.client.post("/webhook/stripe").status_code, 200)
        self.assertEqual(self.a._load_subs()[0]["status"], "active")

    def test_invoice_failures_are_recorded_and_sync_billing_status(self):
        os.environ["STRIPE_WEBHOOK_SECRET"] = "test"
        self.a.TIER_PRICES = {"starter": "price_s", "professional": "price_p", "power": "price_w"}
        self.a._sqlite_set("subscriptions", [{"stripe_subscription_id": "sub", "user_id": "buyer",
            "county": "test", "status": "active", "tier": "starter", "traces_used": 3}])
        sub = {"id": "sub", "status": "past_due", "items": {"data": [{"price": {"id": "price_s"}}]}}
        self.a.stripe.Subscription = SimpleNamespace(retrieve=Mock(return_value=sub))
        for event_type in ("invoice.payment_failed", "invoice.payment_action_required", "invoice.finalization_failed"):
            event_id = "evt_" + event_type
            self.a.stripe.Webhook.construct_event.return_value = {"id": event_id, "type": event_type,
                "data": {"object": {"id": "invoice", "subscription": "sub", "attempt_count": 2}}}
            self.assertEqual(self.client.post("/webhook/stripe").status_code, 200)
        self.assertEqual(self.a._load_subs()[0]["status"], "past_due")
        self.assertEqual(self.a._load_subs()[0]["traces_used"], 3)
        self.assertEqual(len(self.a._sqlite_get("stripe_billing_problems", {})), 3)

    def test_unrelated_subscription_events_are_acknowledged(self):
        os.environ["STRIPE_WEBHOOK_SECRET"] = "test"
        unrelated = {"id": "sub_other", "status": "active", "metadata": {},
            "items": {"data": [{"price": {"id": "price_other"}}]}}
        self.a.stripe.Subscription = SimpleNamespace(retrieve=Mock(return_value=unrelated))
        for event_type in ("customer.subscription.created", "customer.subscription.updated",
                           "customer.subscription.paused", "customer.subscription.resumed"):
            self.a.stripe.Webhook.construct_event.return_value = {"id": "evt_" + event_type,
                "type": event_type, "data": {"object": {"id": "sub_other"}}}
            self.assertEqual(self.client.post("/webhook/stripe").status_code, 200)
        self.a.stripe.Webhook.construct_event.return_value = {"id": "evt_deleted_other",
            "type": "customer.subscription.deleted", "data": {"object": unrelated}}
        self.assertEqual(self.client.post("/webhook/stripe").status_code, 200)
        self.assertEqual(self.a._load_subs(), [])

    def test_unrelated_paid_invoice_is_ignored(self):
        os.environ["STRIPE_WEBHOOK_SECRET"] = "test"
        unrelated = {"id": "sub_other", "status": "active", "metadata": {},
            "items": {"data": [{"price": {"id": "price_other"}}]}}
        self.a.stripe.Subscription = SimpleNamespace(retrieve=Mock(return_value=unrelated))
        self.a.stripe.Webhook.construct_event.return_value = {"id": "evt_invoice_other",
            "type": "invoice.paid", "data": {"object": {"id": "in_other", "subscription": "sub_other"}}}
        self.assertEqual(self.client.post("/webhook/stripe").status_code, 200)
        self.assertEqual(self.a._load_subs(), [])


if __name__ == "__main__":
    unittest.main()
