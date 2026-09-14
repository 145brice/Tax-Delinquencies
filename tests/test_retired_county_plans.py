import unittest
from unittest.mock import Mock

from app_fixture import isolated_app


class RetiredCountyPlansTests(unittest.TestCase):
    def test_old_subscription_links_cannot_create_or_change_charges(self):
        a = isolated_app(self)
        a.app.before_request_funcs[None] = []
        a._subscriptions_ready = lambda: True
        a.current_user = lambda: {"id": "buyer", "email": "buyer@example.com"}
        create = a.stripe.checkout.Session.create = Mock()
        modify = a.stripe.Subscription.modify = Mock()
        client = a.app.test_client()
        for path in ("/subscribe", "/api/subscriber/change-tier"):
            response = client.post(path, json={"county": "test", "tier": "power"})
            self.assertEqual(response.status_code, 410, response.get_data(as_text=True))
        create.assert_not_called()
        modify.assert_not_called()

    def test_pricing_offers_only_individual_leads_and_credits(self):
        a = isolated_app(self)
        a._subscriptions_ready = lambda: True
        response = a.app.test_client().get('/pricing')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        for removed in ('doSubscribe', 'href="#subscribe"', 'County subscription', 'County lead plans', '/mo', 'monthly allowance'):
            self.assertNotIn(removed, html)
        self.assertIn('credit packs', html)
        self.assertIn('pay by card', html)


if __name__ == '__main__':
    unittest.main()
