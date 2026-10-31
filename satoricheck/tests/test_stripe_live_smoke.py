"""
Opt-in live smoke test for Stripe checkout and configured price IDs.

Why: Unit tests mock Stripe SDK calls, so they cannot detect when Stripe deprecates
API parameters (e.g. `payment_method_types` triggering InvalidRequestError on modern
Stripe accounts) or when a configured `stripe_price_id` is deactivated in the Dashboard.

Run with:
    STRIPE_LIVE_SMOKE=true .venv/bin/pytest satoricheck/tests/test_stripe_live_smoke.py
"""
import os
import pytest
import stripe
from backend.config import Config

SMOKE_STRIPE_KEY = os.getenv('STRIPE_SMOKE_API_KEY')

pytestmark = pytest.mark.skipif(
    not SMOKE_STRIPE_KEY,
    reason="Set STRIPE_SMOKE_API_KEY=<real key> to run live Stripe API smoke tests",
)


@pytest.mark.parametrize("pkg_id", list(Config.TOKEN_PACKAGES.keys()))
def test_live_stripe_prices_exist_and_match_currency(pkg_id):
    """Every configured stripe_price_id must exist in Stripe and match configured currency and amount."""
    stripe.api_key = SMOKE_STRIPE_KEY
    pkg = Config.TOKEN_PACKAGES[pkg_id]
    price_id = pkg['stripe_price_id']

    price = stripe.Price.retrieve(price_id)
    assert price.id == price_id
    assert price.currency.lower() == pkg['currency'].lower()
    assert price.unit_amount == pkg['price']
    assert price.active is True


def test_live_stripe_checkout_session_creation():
    """Verify live Stripe API accepts checkout session payload without parameter deprecation errors."""
    stripe.api_key = SMOKE_STRIPE_KEY
    pkg = Config.TOKEN_PACKAGES['battery_small']

    session = stripe.checkout.Session.create(
        line_items=[{
            'price': pkg['stripe_price_id'],
            'quantity': 1,
        }],
        mode='payment',
        allow_promotion_codes=True,
        success_url='https://authenix.ai/api/billing/success?session_id={CHECKOUT_SESSION_ID}',
        cancel_url='https://authenix.ai/?payment=cancelled',
        metadata={
            'user_id': 999999,
            'package_type': 'battery_small',
            'tokens': pkg['tokens'],
            'smoke_test': 'true',
        }
    )

    assert session.id.startswith('cs_')
    assert session.url and 'checkout.stripe.com' in session.url
