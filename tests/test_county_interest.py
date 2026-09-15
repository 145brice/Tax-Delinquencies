import os
import json
import unittest
from unittest.mock import MagicMock, patch

from app_fixture import isolated_app


class CountyInterestTests(unittest.TestCase):
    def setUp(self):
        self.a = isolated_app(self)
        self.client = self.a.app.test_client()
        with self.client.session_transaction() as session:
            session["_csrf_token"] = "county-csrf"
        self.headers = {"X-CSRF-Token": "county-csrf"}

    def request(self, **changes):
        payload = {
            "county": "Travis County", "state": "TX",
            "email": "buyer@example.com",
            "consent": True,
        }
        payload.update(changes)
        return self.client.post("/api/county-interest", json=payload, headers=self.headers)

    def test_unknown_county_is_saved_and_repeat_requests_raise_demand(self):
        first = self.request()
        second = self.request()
        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        self.assertEqual(first.json["availability"], "waiting")
        self.assertIn("will add the county", first.json["message"])
        self.assertIn("email you", first.json["message"])
        self.assertEqual(second.status_code, 200)
        entries = self.a._sqlite_get(self.a.COUNTY_REQUESTS_KEY, {})
        self.assertEqual(len(entries), 1)
        entry = next(iter(entries.values()))
        self.assertEqual(entry["request_count"], 2)
        self.assertEqual(entry["county"], "Travis")
        self.assertTrue(entry["consent_at"])

    def test_requires_valid_email_and_explicit_consent(self):
        self.assertEqual(self.request(email="bad").status_code, 400)
        self.assertEqual(self.request(consent=False).status_code, 400)
        self.assertEqual(self.a._sqlite_get(self.a.COUNTY_REQUESTS_KEY, {}), {})

    def test_live_county_returns_direct_inventory_link(self):
        self.a._sqlite_set("listings", [{
            "id": "lead-1", "county": "Travis", "state": "TX",
            "status": "Auction", "address": "123 Main St",
            "source": "Travis County public notice",
        }])
        response = self.request()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["availability"], "available")
        self.assertEqual(response.json["count"], 1)
        self.assertIn("county=travis", response.json["url"])

    def test_new_inventory_marks_waiting_request_ready_and_queues_alert(self):
        response = self.request()
        request_id = next(iter(self.a._sqlite_get(self.a.COUNTY_REQUESTS_KEY, {})))
        configured = {
            "SMTP_HOST": "smtp.example", "SMTP_FROM": "alerts@example.com",
        }
        with patch.dict(os.environ, configured):
            changed = self.a._mark_county_requests_ready([{"county": "Travis", "state": "TX"}])
        self.assertEqual(changed, 1)
        entry = self.a._sqlite_get(self.a.COUNTY_REQUESTS_KEY, {})[request_id]
        self.assertEqual(entry["status"], "ready")
        with self.a.purchase_store.transaction() as conn:
            job = conn.execute("SELECT id, kind FROM fulfillment_jobs").fetchone()
        self.assertEqual((job["id"], job["kind"]), ("county-alert:" + request_id, "county_alert"))

    def test_delivery_records_email(self):
        self.request()
        request_id = next(iter(self.a._sqlite_get(self.a.COUNTY_REQUESTS_KEY, {})))
        configured = {
            "SMTP_HOST": "smtp.example", "SMTP_FROM": "alerts@example.com",
        }
        with patch.dict(os.environ, configured):
            self.a._mark_county_requests_ready([{"county": "Travis", "state": "TX"}])
            with patch.object(self.a, "_send_county_email") as email_send:
                self.assertTrue(self.a._deliver_county_alert(request_id))
        email_send.assert_called_once()
        entry = self.a._sqlite_get(self.a.COUNTY_REQUESTS_KEY, {})[request_id]
        self.assertEqual(entry["status"], "notified")
        self.assertEqual(entry["email_status"], "sent")

    def test_resend_adapter_uses_https_and_idempotency(self):
        entry = {"id": "request-1", "county": "Travis", "county_key": "travis",
                 "state": "TX", "email": "buyer@example.com"}
        response = MagicMock()
        response.__enter__.return_value.status = 200
        with patch.dict(os.environ, {"RESEND_API_KEY": "re_test", "RESEND_FROM": "Alerts <alerts@example.com>"}), \
             patch.object(self.a.urllib.request, "urlopen", return_value=response) as send:
            self.a._send_email(entry["email"], "Ready", "Body", "county-alert-request-1",
                               attachment=("leads.csv", "address\n123 Main"))
        request = send.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.resend.com/emails")
        self.assertEqual(request.headers["Idempotency-key"], "county-alert-request-1")
        payload = json.loads(request.data)
        self.assertEqual(payload["to"], ["buyer@example.com"])
        self.assertEqual(payload["attachments"][0]["filename"], "leads.csv")
        self.assertTrue(payload["attachments"][0]["content"])

    def paid_order(self, session_id="cs_delivery"):
        user = self.a.db.create_user("owner@example.com", "password-hash")
        lead = {
            "id": "lead-1", "status": "Auction", "purchase_mode": "skip",
            "county": "Travis", "state": "TX", "address": "123 Main Street",
            "owner": "Owner Name", "primary_phone": "5125550187",
            "email_1": "owner@lead.example", "buyer_notes": "=unsafe formula",
        }
        order_id = self.a.db.create_pending_order(user["id"], user["email"], session_id, 1500, [lead])
        self.a.db.mark_order_paid(session_id)
        return user, order_id

    def test_buyer_can_download_only_their_order_csv(self):
        user, order_id = self.paid_order()
        with self.client.session_transaction() as session:
            session["user_id"] = user["id"]
        response = self.client.get(f"/account/orders/{order_id}/leads.csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        csv_text = response.get_data(as_text=True)
        self.assertIn("123 Main Street", csv_text)
        self.assertIn("'=unsafe formula", csv_text)
        self.assertEqual(self.client.get("/account/orders/not-their-order/leads.csv").status_code, 404)

    def test_order_email_has_csv_attachment_and_dashboard_link(self):
        user, order_id = self.paid_order("cs_email")
        configured = {"RESEND_API_KEY": "re_test", "RESEND_FROM": "Alerts <alerts@example.com>"}
        with patch.dict(os.environ, configured):
            self.assertTrue(self.a._schedule_order_delivery_email("cs_email"))
            with patch.object(self.a, "_send_email") as send:
                self.assertTrue(self.a._deliver_order_email("cs_email"))
        args = send.call_args.args
        self.assertEqual(args[0], user["email"])
        self.assertIn("/account", args[2])
        attachment = send.call_args.kwargs["attachment"]
        self.assertTrue(attachment[0].endswith(".csv"))
        self.assertIn("123 Main Street", attachment[1])
        delivery = self.a._sqlite_get(self.a.ORDER_DELIVERY_EMAILS_KEY, {})["cs_email"]
        self.assertEqual(delivery["status"], "sent")


if __name__ == "__main__":
    unittest.main()
