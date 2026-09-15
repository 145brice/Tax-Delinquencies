import os
import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
