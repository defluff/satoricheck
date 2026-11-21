"""
Billing & Stripe Integration Tests.
Tests Payment Flow, Checkout Session Creation, Webhook Handling, Idempotency, and Portal.
"""
import hashlib
import hmac
import json
import time
import pytest
from unittest.mock import patch, MagicMock
import stripe
from backend.config import Config


def _build_signed_webhook_request(
    user_id: int,
    secret: str,
    package_type: str = 'battery_small',
    tokens: int = 86,
    session_id: str = 'cs_test_hmac_123',
    timestamp: int | None = None,
) -> tuple[bytes, str]:
    """Build a raw Stripe webhook JSON payload and HMAC-SHA256 Stripe-Signature header."""
    payload_str = json.dumps({
        'id': f'evt_{session_id}',
        'object': 'event',
        'type': 'checkout.session.completed',
        'data': {
            'object': {
                'id': session_id,
                'object': 'checkout.session',
                'customer': 'cus_test_hmac_123',
                'metadata': {
                    'user_id': str(user_id),
                    'package_type': package_type,
                    'tokens': str(tokens),
                },
            }
        },
    })
    ts = timestamp if timestamp is not None else int(time.time())
    signed_payload = f'{ts}.{payload_str}'.encode('utf-8')
    signature = hmac.new(secret.encode('utf-8'), signed_payload, hashlib.sha256).hexdigest()
    return payload_str.encode('utf-8'), f't={ts},v1={signature}'


class TestStripeWebhook:
    """Test Stripe webhook fulfillment."""
    
    def test_webhook_fulfills_order(self, client, test_user, db_session_fixture, mock_stripe_webhook_payload):
        """Stripe webhook should verify real HMAC signature and add tokens to user balance."""
        from backend.models import TokenBalance, Transaction
        
        # Get initial balance
        initial_balance = db_session_fixture.query(TokenBalance).filter_by(
            user_id=test_user.id
        ).first().balance
        
        body, sig_header = mock_stripe_webhook_payload(
            user_id=test_user.id,
            package_type='battery_small',
            tokens=86,
            session_id='cs_test_unique_123'
        )
        response = client.post(
            '/api/billing/webhook',
            data=body,
            content_type='application/json',
            headers={'Stripe-Signature': sig_header}
        )
        
        assert response.status_code == 200
        
        # Verify balance increased
        new_balance = db_session_fixture.query(TokenBalance).filter_by(
            user_id=test_user.id
        ).first().balance
        
        assert new_balance == initial_balance + 86
        
        # Verify transaction recorded
        transaction = db_session_fixture.query(Transaction).filter_by(
            stripe_session_id='cs_test_unique_123'
        ).first()
        
        assert transaction is not None
        assert transaction.amount == 86
        assert transaction.user_id == test_user.id
    
    def test_webhook_idempotency(self, client, test_user, db_session_fixture, mock_stripe_webhook_payload):
        """Same signed webhook sent twice should only credit once."""
        from backend.models import TokenBalance
        
        initial_balance = db_session_fixture.query(TokenBalance).filter_by(
            user_id=test_user.id
        ).first().balance
        
        body, sig_header = mock_stripe_webhook_payload(
            user_id=test_user.id,
            tokens=100,
            session_id='cs_test_duplicate_456'
        )
        
        # Send identical signed webhook twice
        resp1 = client.post('/api/billing/webhook', data=body,
                            content_type='application/json', headers={'Stripe-Signature': sig_header})
        resp2 = client.post('/api/billing/webhook', data=body,
                            content_type='application/json', headers={'Stripe-Signature': sig_header})
        assert resp1.status_code == 200
        assert resp2.status_code == 200
        
        # Should only add 100 once, not 200
        final_balance = db_session_fixture.query(TokenBalance).filter_by(
            user_id=test_user.id
        ).first().balance
        
        assert final_balance == initial_balance + 100

    def test_webhook_rejects_forged_hmac_signature(self, client, test_user, db_session_fixture):
        """Webhook signed with a wrong secret or tampered payload must be rejected with 400 and credit 0 tokens."""
        from backend.models import TokenBalance, Transaction

        initial_balance = db_session_fixture.query(TokenBalance).filter_by(
            user_id=test_user.id
        ).first().balance

        # 1. Payload signed with a forged secret instead of whsec_test_fake
        forged_body, forged_sig = _build_signed_webhook_request(
            user_id=test_user.id,
            secret='whsec_forged_attacker_secret',
            tokens=500,
            session_id='cs_forged_sig_001',
        )
        resp_forged = client.post(
            '/api/billing/webhook',
            data=forged_body,
            content_type='application/json',
            headers={'Stripe-Signature': forged_sig},
        )
        assert resp_forged.status_code == 400
        assert resp_forged.get_json()['error'] == 'Invalid signature'

        # 2. Valid signature for 86 tokens, but attacker tampers body to 9999 tokens
        valid_body, valid_sig = _build_signed_webhook_request(
            user_id=test_user.id,
            secret='whsec_test_fake',
            tokens=86,
            session_id='cs_tampered_body_002',
        )
        tampered_body = valid_body.replace(b'"tokens": "86"', b'"tokens": "9999"')
        resp_tampered = client.post(
            '/api/billing/webhook',
            data=tampered_body,
            content_type='application/json',
            headers={'Stripe-Signature': valid_sig},
        )
        assert resp_tampered.status_code == 400
        assert resp_tampered.get_json()['error'] == 'Invalid signature'

        # Balance and transaction table must remain completely untouched
        final_balance = db_session_fixture.query(TokenBalance).filter_by(
            user_id=test_user.id
        ).first().balance
        assert final_balance == initial_balance
        assert db_session_fixture.query(Transaction).filter(
            Transaction.stripe_session_id.in_(['cs_forged_sig_001', 'cs_tampered_body_002'])
        ).count() == 0

    def test_webhook_accepts_valid_hmac_signature_end_to_end(self, client, test_user, db_session_fixture):
        """Real stripe.Webhook.construct_event HMAC verification with whsec_test_fake must fulfill the order."""
        from backend.models import TokenBalance, Transaction

        initial_balance = db_session_fixture.query(TokenBalance).filter_by(
            user_id=test_user.id
        ).first().balance

        body, sig_header = _build_signed_webhook_request(
            user_id=test_user.id,
            secret='whsec_test_fake',
            package_type='battery_small',
            tokens=86,
            session_id='cs_real_hmac_verified_123',
        )
        response = client.post(
            '/api/billing/webhook',
            data=body,
            content_type='application/json',
            headers={'Stripe-Signature': sig_header},
        )

        assert response.status_code == 200
        assert response.get_json() == {'success': True}

        new_balance = db_session_fixture.query(TokenBalance).filter_by(
            user_id=test_user.id
        ).first().balance
        assert new_balance == initial_balance + 86

        tx = db_session_fixture.query(Transaction).filter_by(
            stripe_session_id='cs_real_hmac_verified_123'
        ).first()
        assert tx is not None
        assert tx.amount == 86
        assert tx.package_type == 'battery_small'
        assert tx.user_id == test_user.id


class TestCheckoutSession:
    """Test checkout session creation and Stripe API contract."""
    
    def test_create_checkout_requires_auth(self, client):
        """Checkout endpoint should require authentication."""
        response = client.post('/api/billing/checkout', json={
            'package': 'battery_small'
        })
        assert response.status_code == 401

    def test_create_checkout_omits_deprecated_payment_method_types(self, auth_client):
        """
        CRITICAL CONTRACT: Stripe rejects `payment_method_types` on checkout sessions
        (now managed via dashboard dynamic payment methods). Passing it causes InvalidRequestError.
        """
        with patch('stripe.Customer.create') as mock_cust, \
             patch('stripe.checkout.Session.create') as mock_create:
            mock_cust.return_value = MagicMock(id='cus_test_123')
            mock_create.return_value = MagicMock(id='cs_test_123', url='https://checkout.stripe.com/test')
            
            response = auth_client.post('/api/billing/checkout', json={
                'package_type': 'battery_small'
            })

            assert response.status_code == 200
            assert mock_create.called
            call_kwargs = mock_create.call_args.kwargs
            assert 'payment_method_types' not in call_kwargs, (
                "payment_method_types must NEVER be passed to Stripe checkout sessions"
            )
            assert call_kwargs['mode'] == 'payment'
            assert call_kwargs['customer'] == 'cus_test_123'
            assert call_kwargs['allow_promotion_codes'] is True
            assert 'session_id={CHECKOUT_SESSION_ID}' in call_kwargs['success_url']

    @pytest.mark.parametrize('pkg_id', list(Config.TOKEN_PACKAGES.keys()))
    def test_create_checkout_all_token_packages(self, auth_client, pkg_id):
        """Every configured token package must map to its exact stripe_price_id and token quantity."""
        pkg = Config.TOKEN_PACKAGES[pkg_id]
        with patch('stripe.Customer.create', return_value=MagicMock(id='cus_test_123')), \
             patch('stripe.checkout.Session.create') as mock_create:
            mock_create.return_value = MagicMock(id=f'cs_{pkg_id}', url=f'https://checkout.stripe.com/{pkg_id}')
            
            response = auth_client.post('/api/billing/checkout', json={
                'package_type': pkg_id
            })

            assert response.status_code == 200
            data = response.get_json()
            assert data['success'] is True
            assert data['session_id'] == f'cs_{pkg_id}'
            assert data['url'] == f'https://checkout.stripe.com/{pkg_id}'

            call_kwargs = mock_create.call_args.kwargs
            line_items = call_kwargs['line_items']
            assert len(line_items) == 1
            assert line_items[0]['price'] == pkg['stripe_price_id']
            assert line_items[0]['quantity'] == 1
            assert call_kwargs['metadata']['package_type'] == pkg_id
            assert call_kwargs['metadata']['tokens'] == pkg['tokens']

    def test_create_checkout_supports_both_package_keys(self, auth_client):
        """Frontend can send either 'package' or 'package_type' for compatibility."""
        with patch('stripe.Customer.create', return_value=MagicMock(id='cus_123')), \
             patch('stripe.checkout.Session.create') as mock_create:
            mock_create.return_value = MagicMock(id='cs_1', url='https://checkout.stripe.com/1')
            
            # Using 'package'
            resp1 = auth_client.post('/api/billing/checkout', json={'package': 'battery_medium'})
            assert resp1.status_code == 200

            # Using 'package_type'
            resp2 = auth_client.post('/api/billing/checkout', json={'package_type': 'battery_medium'})
            assert resp2.status_code == 200

    def test_create_checkout_reuses_existing_stripe_customer_id(self, auth_client, test_user, db_session_fixture):
        """If user already has a past transaction with a customer ID, reuse it without creating a new customer."""
        from backend.models import Transaction

        # Pre-seed existing transaction with customer ID
        tx = Transaction(
            user_id=test_user.id,
            type='purchase',
            amount=86,
            stripe_customer_id='cus_existing_999',
            stripe_session_id='cs_old_session'
        )
        db_session_fixture.add(tx)
        db_session_fixture.commit()

        with patch('stripe.Customer.create') as mock_cust_create, \
             patch('stripe.checkout.Session.create') as mock_session_create:
            mock_session_create.return_value = MagicMock(id='cs_new', url='https://checkout.stripe.com/new')

            response = auth_client.post('/api/billing/checkout', json={'package_type': 'battery_small'})

            assert response.status_code == 200
            # Ensure Customer.create was NOT called
            mock_cust_create.assert_not_called()
            # Ensure existing customer ID was used
            assert mock_session_create.call_args.kwargs['customer'] == 'cus_existing_999'

    def test_create_checkout_rejects_missing_or_invalid_payload(self, auth_client):
        """Reject missing data and unrecognized package identifiers."""
        # Empty payload
        resp_empty = auth_client.post('/api/billing/checkout', json={})
        assert resp_empty.status_code == 400

        # Unrecognized package
        resp_invalid = auth_client.post('/api/billing/checkout', json={'package_type': 'non_existent_package'})
        assert resp_invalid.status_code == 400

    def test_create_checkout_handles_stripe_invalid_request_error(self, auth_client):
        """Ensure Stripe InvalidRequestError returns clean 400 with helpful error."""
        with patch('stripe.Customer.create', return_value=MagicMock(id='cus_123')), \
             patch('stripe.checkout.Session.create') as mock_create:
            mock_create.side_effect = stripe.error.InvalidRequestError("Parameter no longer supported", "param")

            response = auth_client.post('/api/billing/checkout', json={'package_type': 'battery_small'})
            assert response.status_code == 400
            data = response.get_json()
            assert 'Payment request invalid' in data['error']

    def test_create_checkout_handles_stripe_card_error(self, auth_client):
        """Ensure Stripe CardError returns clean 400 with user-friendly card error."""
        with patch('stripe.Customer.create', return_value=MagicMock(id='cus_123')), \
             patch('stripe.checkout.Session.create') as mock_create:
            mock_create.side_effect = stripe.error.CardError("Card declined", "card", "card_declined")

            response = auth_client.post('/api/billing/checkout', json={'package_type': 'battery_small'})
            assert response.status_code == 400
            data = response.get_json()
            assert 'Please check your card details' in data['error']

    def test_create_checkout_handles_stripe_rate_limit_error(self, auth_client):
        """Ensure Stripe RateLimitError returns clean busy message."""
        with patch('stripe.Customer.create', return_value=MagicMock(id='cus_123')), \
             patch('stripe.checkout.Session.create') as mock_create:
            mock_create.side_effect = stripe.error.RateLimitError("Too many requests")

            response = auth_client.post('/api/billing/checkout', json={'package_type': 'battery_small'})
            assert response.status_code == 400
            data = response.get_json()
            assert 'Payment system is busy' in data['error']


class TestCustomerPortal:
    """Test Stripe Customer Portal session creation."""

    def test_create_portal_requires_auth(self, client):
        response = client.post('/api/billing/create-portal')
        assert response.status_code == 401

    def test_create_portal_rejects_user_without_billing_history(self, auth_client):
        response = auth_client.post('/api/billing/create-portal')
        assert response.status_code == 400
        assert 'No billing history found' in response.get_json()['error']

    def test_create_portal_creates_session_for_user_with_customer_id(self, auth_client, test_user, db_session_fixture):
        from backend.models import Transaction

        tx = Transaction(
            user_id=test_user.id,
            type='purchase',
            amount=86,
            stripe_customer_id='cus_portal_user',
            stripe_session_id='cs_portal_init'
        )
        db_session_fixture.add(tx)
        db_session_fixture.commit()

        with patch('stripe.billing_portal.Session.create') as mock_portal:
            mock_portal.return_value = MagicMock(url='https://billing.stripe.com/session/123')

            response = auth_client.post('/api/billing/create-portal')
            assert response.status_code == 200
            assert response.get_json()['url'] == 'https://billing.stripe.com/session/123'
            assert mock_portal.call_args.kwargs['customer'] == 'cus_portal_user'


class TestBillingPackagesAndRedirects:
    """Test package catalogue and Stripe redirect safety invariants."""

    def test_get_packages_returns_all_token_packages(self, client):
        """Ensure public package catalogue is served correctly."""
        response = client.get('/api/billing/packages')
        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        assert set(data['packages'].keys()) == {'battery_small', 'battery_medium', 'battery_large', 'wizard'}
        assert data['packages']['wizard']['price'] == 190000
        assert 'words_per_cp' not in data
        assert 'tokens_per_cp_unit' not in data

    def test_payment_success_redirect_never_credits_tokens_directly(self, client, test_user, db_session_fixture):
        """
        CORE SECURITY INVARIANT:
        The /success redirect must NEVER credit tokens. Only webhooks with verified
        cryptographic signatures can fulfill balances.
        """
        from backend.models import TokenBalance
        initial_balance = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).first().balance

        # Visit success URL with a forged session ID
        response = client.get('/api/billing/success?session_id=forged_cs_test')
        assert response.status_code == 302
        assert response.location.endswith('/?payment=success')

        # Verify balance remains unchanged
        final_balance = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).first().balance
        assert final_balance == initial_balance

    def test_payment_success_without_session_id_redirects_error(self, client):
        response = client.get('/api/billing/success')
        assert response.status_code == 302
        assert response.location.endswith('/?error=no_session')

    def test_extension_sidepanel_recharge_modal_matches_flat_cp_packages(self):
        """Extension recharge modal must reflect v2.0 flat-CP and Smart Batch (8x) pricing."""
        import pathlib

        html_path = pathlib.Path(__file__).resolve().parents[1] / 'extension' / 'sidepanel' / 'sidepanel.html'
        html_src = html_path.read_text(encoding='utf-8')

        assert '1 CP = 1 Fact-Check (or up to 8 claims in Smart Batch)' in html_src
        assert '1,250 words' not in html_src
        assert '107k words' not in html_src
        assert '607k words' not in html_src
        assert '2.7M words' not in html_src

        small_cp = Config.TOKEN_PACKAGES['battery_small']['tokens']
        med_cp = Config.TOKEN_PACKAGES['battery_medium']['tokens']
        large_cp = Config.TOKEN_PACKAGES['battery_large']['tokens']

        assert f'{small_cp} verification checks (up to {small_cp * 8:,} batch claims)' in html_src
        assert f'{med_cp} verification checks (up to {med_cp * 8:,} batch claims)' in html_src
        assert f'{large_cp:,} verification checks (up to {large_cp * 8:,} batch claims)' in html_src


def _build_signed_stripe_event(event_type: str, obj: dict, secret: str = 'whsec_test_fake', event_id: str = 'evt_test_auto') -> tuple[bytes, str]:
    """Build a raw Stripe webhook event JSON payload and real HMAC-SHA256 Stripe-Signature header."""
    payload_str = json.dumps({
        'id': event_id,
        'object': 'event',
        'type': event_type,
        'data': {'object': obj},
    })
    ts = int(time.time())
    signed_payload = f'{ts}.{payload_str}'.encode('utf-8')
    signature = hmac.new(secret.encode('utf-8'), signed_payload, hashlib.sha256).hexdigest()
    return payload_str.encode('utf-8'), f't={ts},v1={signature}'


class TestAutoRefillConfigAndSetup:
    """Test GET/POST /api/billing/auto-refill, /setup, package validation, and customer reuse."""

    def test_auto_refill_endpoints_require_auth(self, client):
        """All auto-refill configuration and setup routes must reject unauthenticated callers with 401."""
        assert client.get('/api/billing/auto-refill').status_code == 401
        assert client.post('/api/billing/auto-refill', json={'enabled': True}).status_code == 401
        assert client.post('/api/billing/auto-refill/setup', json={}).status_code == 401

    def test_get_auto_refill_defaults_without_live_stripe_calls(self, auth_client, test_user, db_session_fixture):
        """GET /api/billing/auto-refill returns DB-only defaults (threshold=0) with zero live Stripe calls."""
        from backend.models import TokenBalance

        bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        bal.stripe_payment_method_id = 'pm_saved_123'
        db_session_fixture.commit()

        with patch('stripe.PaymentMethod.retrieve') as mock_pm_retrieve:
            response = auth_client.get('/api/billing/auto-refill')

        assert response.status_code == 200
        mock_pm_retrieve.assert_not_called()
        data = response.get_json()
        assert data['success'] is True
        assert data['enabled'] is False
        assert data['package_type'] == 'battery_medium'
        assert data['threshold'] == 0
        assert data['has_payment_method'] is True
        assert data['failed'] is False
        assert data['is_wizard'] is False
        assert [p['id'] for p in data['eligible_packages']] == ['battery_medium', 'battery_large']

    def test_get_auto_refill_wizard_defaults_to_battery_large(self, auth_client, test_user, db_session_fixture):
        """Wizard users without an explicit package must default to battery_large."""
        from backend.models import TokenBalance

        bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        bal.is_wizard = True
        bal.auto_refill_package = None
        db_session_fixture.commit()

        response = auth_client.get('/api/billing/auto-refill')
        assert response.status_code == 200
        data = response.get_json()
        assert data['is_wizard'] is True
        assert data['package_type'] == 'battery_large'

    def test_post_auto_refill_requires_payment_method_when_enabling(self, auth_client, test_user, db_session_fixture):
        """Enabling auto-refill without a saved payment method must return 400 with requires_payment_method=True."""
        from backend.models import TokenBalance

        resp_enable = auth_client.post('/api/billing/auto-refill', json={
            'enabled': True,
            'package_type': 'battery_medium',
        })
        assert resp_enable.status_code == 400
        err_data = resp_enable.get_json()
        assert err_data['success'] is False
        assert err_data['requires_payment_method'] is True

        db_session_fixture.expire_all()
        bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        assert bal.auto_refill_enabled is False

        # Disabling without a saved PM must succeed
        resp_disable = auth_client.post('/api/billing/auto-refill', json={
            'enabled': False,
            'package_type': 'battery_large',
        })
        assert resp_disable.status_code == 200
        assert resp_disable.get_json()['package_type'] == 'battery_large'

    @pytest.mark.parametrize('invalid_pkg', ['battery_small', 'wizard', 'enterprise', ''])
    def test_post_auto_refill_rejects_ineligible_packages(self, auth_client, test_user, db_session_fixture, invalid_pkg):
        """Only battery_medium and battery_large are eligible for auto-refill."""
        from backend.models import TokenBalance

        bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        bal.stripe_payment_method_id = 'pm_valid_1'
        db_session_fixture.commit()

        response = auth_client.post('/api/billing/auto-refill', json={
            'enabled': True,
            'package_type': invalid_pkg,
        })
        assert response.status_code == 400
        assert response.get_json()['success'] is False

        db_session_fixture.expire_all()
        refreshed = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        assert refreshed.auto_refill_enabled is False
        assert refreshed.auto_refill_package is None

    def test_post_auto_refill_enables_and_persists_valid_package(self, auth_client, test_user, db_session_fixture):
        """With a saved payment method, POST /api/billing/auto-refill persists enabled=True and package."""
        from backend.models import TokenBalance

        bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        bal.stripe_payment_method_id = 'pm_valid_2'
        bal.auto_refill_failed = True
        db_session_fixture.commit()

        response = auth_client.post('/api/billing/auto-refill', json={
            'enabled': True,
            'package_type': 'battery_large',
        })
        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        assert data['enabled'] is True
        assert data['package_type'] == 'battery_large'
        assert data['threshold'] == 0
        assert data['failed'] is False

        db_session_fixture.expire_all()
        refreshed = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        assert refreshed.auto_refill_enabled is True
        assert refreshed.auto_refill_package == 'battery_large'
        assert refreshed.auto_refill_threshold == 0
        assert refreshed.auto_refill_failed is False

    def test_setup_session_uses_setup_mode_chf_and_omits_payment_method_types(
        self, auth_client, test_user, db_session_fixture
    ):
        """POST /api/billing/auto-refill/setup creates a Stripe setup Checkout Session in CHF without payment_method_types."""
        from backend.models import TokenBalance

        with patch('stripe.Customer.create', return_value=MagicMock(id='cus_setup_new')) as mock_cust, \
             patch('stripe.checkout.Session.create') as mock_session:
            mock_session.return_value = MagicMock(
                id='cs_setup_123',
                url='https://checkout.stripe.com/c/pay/cs_setup_123',
            )

            response = auth_client.post('/api/billing/auto-refill/setup', json={
                'package_type': 'battery_large',
            })

        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        assert data['session_id'] == 'cs_setup_123'
        assert data['url'] == 'https://checkout.stripe.com/c/pay/cs_setup_123'

        mock_cust.assert_called_once()
        session_kwargs = mock_session.call_args.kwargs
        assert session_kwargs['mode'] == 'setup'
        assert session_kwargs['currency'] == 'chf'
        assert session_kwargs['customer'] == 'cus_setup_new'
        assert 'payment_method_types' not in session_kwargs
        assert session_kwargs['metadata']['purpose'] == 'auto_refill_setup'
        assert session_kwargs['metadata']['user_id'] == str(test_user.id)

        db_session_fixture.expire_all()
        bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        assert bal.stripe_customer_id == 'cus_setup_new'
        assert bal.auto_refill_package == 'battery_large'

    def test_checkout_and_portal_reuse_token_balance_customer_id(self, auth_client, test_user, db_session_fixture):
        """Checkout and Portal reuse TokenBalance.stripe_customer_id even with zero prior Transactions."""
        from backend.models import TokenBalance

        bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        bal.stripe_customer_id = 'cus_from_setup_only'
        db_session_fixture.commit()

        with patch('stripe.Customer.create') as mock_cust, \
             patch('stripe.checkout.Session.create') as mock_checkout, \
             patch('stripe.billing_portal.Session.create') as mock_portal:
            mock_checkout.return_value = MagicMock(id='cs_reuse', url='https://checkout.stripe.com/reuse')
            mock_portal.return_value = MagicMock(url='https://billing.stripe.com/reuse')

            resp_checkout = auth_client.post('/api/billing/checkout', json={'package_type': 'battery_medium'})
            resp_portal = auth_client.post('/api/billing/create-portal')

        assert resp_checkout.status_code == 200
        assert resp_portal.status_code == 200
        mock_cust.assert_not_called()
        assert mock_checkout.call_args.kwargs['customer'] == 'cus_from_setup_only'
        assert mock_portal.call_args.kwargs['customer'] == 'cus_from_setup_only'


class TestAutoRefillWebhooks:
    """Test signed Stripe webhook handlers for setup sessions and off-session auto-refill PaymentIntents."""

    def test_setup_webhook_persists_customer_and_pm_with_zero_transactions(
        self, client, test_user, db_session_fixture
    ):
        """checkout.session.completed (mode='setup') saves customer & PM IDs and creates 0 transactions."""
        from backend.models import TokenBalance, Transaction

        bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        initial_balance = bal.balance
        bal.auto_refill_failed = True
        db_session_fixture.commit()

        body, sig = _build_signed_stripe_event(
            'checkout.session.completed',
            {
                'id': 'cs_setup_done_1',
                'object': 'checkout.session',
                'mode': 'setup',
                'customer': 'cus_webhook_setup_1',
                'setup_intent': 'seti_webhook_1',
                'metadata': {
                    'user_id': str(test_user.id),
                    'purpose': 'auto_refill_setup',
                    'package_type': 'battery_medium',
                },
            },
            event_id='evt_setup_1',
        )

        with patch('stripe.SetupIntent.retrieve') as mock_si:
            mock_si.return_value = MagicMock(
                id='seti_webhook_1',
                customer='cus_webhook_setup_1',
                payment_method='pm_webhook_saved_1',
            )
            response = client.post(
                '/api/billing/webhook',
                data=body,
                content_type='application/json',
                headers={'Stripe-Signature': sig},
            )

        assert response.status_code == 200
        assert response.get_json() == {'success': True}
        mock_si.assert_called_once_with('seti_webhook_1')

        db_session_fixture.expire_all()
        refreshed = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        assert refreshed.stripe_customer_id == 'cus_webhook_setup_1'
        assert refreshed.stripe_payment_method_id == 'pm_webhook_saved_1'
        assert refreshed.auto_refill_failed is False
        assert refreshed.balance == initial_balance
        assert db_session_fixture.query(Transaction).filter_by(user_id=test_user.id).count() == 0

    def test_payment_intent_succeeded_auto_refill_fulfills_idempotently(
        self, client, test_user, db_session_fixture
    ):
        """payment_intent.succeeded with metadata.auto_refill=='true' credits CP exactly once."""
        from backend.models import TokenBalance, Transaction

        bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        bal.balance = 0
        bal.auto_refill_failed = True
        db_session_fixture.commit()

        expected_cp = Config.TOKEN_PACKAGES['battery_medium']['tokens']
        body, sig = _build_signed_stripe_event(
            'payment_intent.succeeded',
            {
                'id': 'pi_auto_webhook_1',
                'object': 'payment_intent',
                'customer': 'cus_auto_1',
                'metadata': {
                    'user_id': str(test_user.id),
                    'package_type': 'battery_medium',
                    'tokens': str(expected_cp),
                    'auto_refill': 'true',
                },
            },
            event_id='evt_pi_auto_1',
        )

        resp1 = client.post('/api/billing/webhook', data=body, content_type='application/json', headers={'Stripe-Signature': sig})
        resp2 = client.post('/api/billing/webhook', data=body, content_type='application/json', headers={'Stripe-Signature': sig})

        assert resp1.status_code == 200
        assert resp2.status_code == 200

        db_session_fixture.expire_all()
        refreshed = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        assert refreshed.balance == expected_cp
        assert refreshed.auto_refill_failed is False

        txs = db_session_fixture.query(Transaction).filter_by(stripe_session_id='pi_auto_webhook_1').all()
        assert len(txs) == 1
        assert txs[0].amount == expected_cp
        assert txs[0].package_type == 'battery_medium'

    def test_payment_intent_succeeded_without_auto_refill_metadata_is_ignored(
        self, client, test_user, db_session_fixture
    ):
        """Regular payment_intent.succeeded without auto_refill=='true' must not double-credit."""
        from backend.models import TokenBalance, Transaction

        initial_balance = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one().balance
        body, sig = _build_signed_stripe_event(
            'payment_intent.succeeded',
            {
                'id': 'pi_regular_checkout_1',
                'object': 'payment_intent',
                'customer': 'cus_reg_1',
                'metadata': {
                    'user_id': str(test_user.id),
                    'package_type': 'battery_medium',
                },
            },
            event_id='evt_pi_reg_1',
        )

        response = client.post('/api/billing/webhook', data=body, content_type='application/json', headers={'Stripe-Signature': sig})
        assert response.status_code == 200

        db_session_fixture.expire_all()
        assert db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one().balance == initial_balance
        assert db_session_fixture.query(Transaction).filter_by(stripe_session_id='pi_regular_checkout_1').count() == 0

    def test_payment_intent_failed_disables_auto_refill_and_marks_failed(
        self, client, test_user, db_session_fixture
    ):
        """payment_intent.payment_failed for auto-refill disables auto_refill_enabled and sets auto_refill_failed."""
        from backend.models import TokenBalance

        bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        bal.auto_refill_enabled = True
        bal.auto_refill_failed = False
        db_session_fixture.commit()

        body, sig = _build_signed_stripe_event(
            'payment_intent.payment_failed',
            {
                'id': 'pi_auto_fail_1',
                'object': 'payment_intent',
                'customer': 'cus_auto_1',
                'metadata': {
                    'user_id': str(test_user.id),
                    'package_type': 'battery_medium',
                    'auto_refill': 'true',
                },
            },
            event_id='evt_pi_fail_1',
        )

        response = client.post('/api/billing/webhook', data=body, content_type='application/json', headers={'Stripe-Signature': sig})
        assert response.status_code == 200

        db_session_fixture.expire_all()
        refreshed = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        assert refreshed.auto_refill_enabled is False
        assert refreshed.auto_refill_failed is True


class TestAutoRefillTriggerEngineAndRoutes:
    """Test maybe_trigger_auto_refill() strict 0-CP / insufficient-balance rules and route integration."""

    def test_maybe_trigger_never_fires_while_sufficient_nonzero_cp_remain(
        self, app, test_user, db_session_fixture
    ):
        """Auto-refill must NOT trigger while sufficient non-zero CP remain."""
        from backend.models import TokenBalance
        from backend.routes.billing import maybe_trigger_auto_refill

        bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        bal.balance = 5
        bal.auto_refill_enabled = True
        bal.auto_refill_package = 'battery_medium'
        bal.stripe_customer_id = 'cus_no_trigger'
        bal.stripe_payment_method_id = 'pm_no_trigger'
        db_session_fixture.commit()

        with patch('stripe.PaymentIntent.create') as mock_pi:
            assert maybe_trigger_auto_refill(test_user.id, db_session_fixture, required_cost=0) is False
            assert maybe_trigger_auto_refill(test_user.id, db_session_fixture, required_cost=1) is False
            assert maybe_trigger_auto_refill(test_user.id, db_session_fixture, required_cost=5) is False

        mock_pi.assert_not_called()
        db_session_fixture.expire_all()
        assert db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one().balance == 5

    def test_factcheck_analyze_triggers_auto_refill_only_when_balance_reaches_zero(
        self, auth_client, test_user, db_session_fixture, mock_gemini
    ):
        """Draining balance to 0 CP on /api/factcheck/analyze triggers off-session refill; non-zero remainder does not."""
        from backend.models import TokenBalance, Transaction

        bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        bal.balance = 5
        bal.auto_refill_enabled = True
        bal.auto_refill_package = 'battery_medium'
        bal.stripe_customer_id = 'cus_fc_1'
        bal.stripe_payment_method_id = 'pm_fc_1'
        db_session_fixture.commit()

        # 1. With 5 CP -> 4 CP remaining -> must NOT call PaymentIntent.create
        with patch('stripe.PaymentIntent.create') as mock_pi_nonzero:
            resp_ok = auth_client.post('/api/factcheck/analyze', json={'text': 'Water boils at 100C.'})
        assert resp_ok.status_code == 200
        assert resp_ok.get_json()['new_balance'] == 4
        mock_pi_nonzero.assert_not_called()

        # 2. Drain to 1 CP -> post-action balance hits 0 CP -> triggers off-session battery_medium refill
        bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        bal.balance = 1
        db_session_fixture.commit()

        expected_pkg = Config.TOKEN_PACKAGES['battery_medium']
        with patch('stripe.PaymentIntent.create') as mock_pi_zero:
            mock_pi_zero.return_value = MagicMock(id='pi_fc_zero_1', status='succeeded')
            resp_zero = auth_client.post('/api/factcheck/analyze', json={'text': 'The Earth orbits the Sun.'})

        assert resp_zero.status_code == 200
        data_zero = resp_zero.get_json()
        assert data_zero['new_balance'] == expected_pkg['tokens']

        pi_kwargs = mock_pi_zero.call_args.kwargs
        assert pi_kwargs['amount'] == expected_pkg['price']
        assert pi_kwargs['currency'] == expected_pkg['currency']
        assert pi_kwargs['customer'] == 'cus_fc_1'
        assert pi_kwargs['payment_method'] == 'pm_fc_1'
        assert pi_kwargs['off_session'] is True
        assert pi_kwargs['confirm'] is True
        assert pi_kwargs['metadata']['auto_refill'] == 'true'
        assert pi_kwargs['metadata']['package_type'] == 'battery_medium'

        db_session_fixture.expire_all()
        refreshed = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        assert refreshed.balance == expected_pkg['tokens']
        tx = db_session_fixture.query(Transaction).filter_by(stripe_session_id='pi_fc_zero_1').one()
        assert tx.amount == expected_pkg['tokens']

    def test_insufficient_nonzero_balance_triggers_refill_before_media_upload(
        self, auth_client, test_user, db_session_fixture
    ):
        """If balance=2 CP is insufficient for a 4 CP image check, auto-refill runs first and action succeeds."""
        import io
        from backend.models import TokenBalance
        from backend.services import get_gemini_service

        bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        bal.balance = 2
        bal.auto_refill_enabled = True
        bal.auto_refill_package = 'battery_medium'
        bal.stripe_customer_id = 'cus_media_1'
        bal.stripe_payment_method_id = 'pm_media_1'
        db_session_fixture.commit()

        gemini_svc = get_gemini_service()
        mock_gemini_client = MagicMock()
        mock_gemini_client.models.generate_content.return_value = MagicMock(
            text=json.dumps({
                'verdict': 'authentic',
                'confidence': 95,
                'explanation': 'No manipulation artifacts detected.',
                'criteria': {},
                'claims': [],
            })
        )

        with patch.object(gemini_svc, 'client', mock_gemini_client), \
             patch('stripe.PaymentIntent.create') as mock_pi:
            mock_pi.return_value = MagicMock(id='pi_media_refill_1', status='succeeded')
            response = auth_client.post(
                '/api/media/analyze-upload',
                data={'file': (io.BytesIO(b'\x89PNG\r\n\x1a\nfake'), 'photo.png')},
                content_type='multipart/form-data',
            )

        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        assert data['cost_incurred'] == 4
        assert data['new_balance'] == 2 + Config.TOKEN_PACKAGES['battery_medium']['tokens'] - 4
        mock_pi.assert_called_once()

        db_session_fixture.expire_all()
        assert db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one().balance == 448

    def test_wizard_user_auto_refill_falls_back_to_battery_large(self, app, test_user, db_session_fixture):
        """Wizard users with unset auto_refill_package must charge battery_large (1,000 CP / 9900 CHF cents)."""
        from backend.models import TokenBalance
        from backend.routes.billing import maybe_trigger_auto_refill

        bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        bal.balance = 0
        bal.is_wizard = True
        bal.auto_refill_enabled = True
        bal.auto_refill_package = None
        bal.stripe_customer_id = 'cus_wiz_1'
        bal.stripe_payment_method_id = 'pm_wiz_1'
        db_session_fixture.commit()

        large_pkg = Config.TOKEN_PACKAGES['battery_large']
        with patch('stripe.PaymentIntent.create') as mock_pi:
            mock_pi.return_value = MagicMock(id='pi_wiz_large_1', status='succeeded')
            triggered = maybe_trigger_auto_refill(test_user.id, db_session_fixture, required_cost=1)

        assert triggered is True
        pi_kwargs = mock_pi.call_args.kwargs
        assert pi_kwargs['amount'] == large_pkg['price']
        assert pi_kwargs['currency'] == large_pkg['currency']
        assert pi_kwargs['metadata']['package_type'] == 'battery_large'

        db_session_fixture.expire_all()
        assert db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one().balance == large_pkg['tokens']

    def test_card_error_disables_auto_refill_sets_failed_flag_and_surfaces_on_balance(
        self, auth_client, test_user, db_session_fixture, mock_gemini
    ):
        """Off-session CardError disables auto_refill_enabled, sets auto_refill_failed=True, and exposes it on /tokens/balance."""
        from backend.models import TokenBalance

        bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        bal.balance = 0
        bal.auto_refill_enabled = True
        bal.auto_refill_package = 'battery_medium'
        bal.stripe_customer_id = 'cus_fail_1'
        bal.stripe_payment_method_id = 'pm_fail_1'
        db_session_fixture.commit()

        with patch('stripe.PaymentIntent.create') as mock_pi:
            mock_pi.side_effect = stripe.error.CardError('Your card was declined.', 'card', 'card_declined')
            resp_analyze = auth_client.post('/api/factcheck/analyze', json={'text': 'Mars is a planet.'})

        assert resp_analyze.status_code == 403
        err_json = resp_analyze.get_json()
        assert err_json['success'] is False
        assert err_json['auto_refill_failed'] is True

        db_session_fixture.expire_all()
        refreshed = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        assert refreshed.auto_refill_enabled is False
        assert refreshed.auto_refill_failed is True
        assert refreshed.balance == 0

        resp_balance = auth_client.get('/api/tokens/balance')
        assert resp_balance.status_code == 200
        assert resp_balance.get_json()['auto_refill_failed'] is True

    def test_webhook_after_sync_auto_refill_does_not_double_credit(
        self, client, test_user, db_session_fixture
    ):
        """Subsequent payment_intent.succeeded webhook for an already synchronously fulfilled PI must be idempotent."""
        from backend.models import TokenBalance, Transaction
        from backend.routes.billing import maybe_trigger_auto_refill

        bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        bal.balance = 0
        bal.auto_refill_enabled = True
        bal.auto_refill_package = 'battery_medium'
        bal.stripe_customer_id = 'cus_sync_1'
        bal.stripe_payment_method_id = 'pm_sync_1'
        db_session_fixture.commit()

        with patch('stripe.PaymentIntent.create', return_value=MagicMock(id='pi_sync_dup_1', status='succeeded')):
            assert maybe_trigger_auto_refill(test_user.id, db_session_fixture, required_cost=1) is True

        expected_cp = Config.TOKEN_PACKAGES['battery_medium']['tokens']
        body, sig = _build_signed_stripe_event(
            'payment_intent.succeeded',
            {
                'id': 'pi_sync_dup_1',
                'object': 'payment_intent',
                'customer': 'cus_sync_1',
                'metadata': {
                    'user_id': str(test_user.id),
                    'package_type': 'battery_medium',
                    'tokens': str(expected_cp),
                    'auto_refill': 'true',
                },
            },
            event_id='evt_sync_dup_1',
        )
        resp_wh = client.post('/api/billing/webhook', data=body, content_type='application/json', headers={'Stripe-Signature': sig})
        assert resp_wh.status_code == 200

        db_session_fixture.expire_all()
        assert db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one().balance == expected_cp
        assert db_session_fixture.query(Transaction).filter_by(stripe_session_id='pi_sync_dup_1').count() == 1

    def test_missing_token_balance_row_is_never_created_by_maybe_trigger(self, app, test_user, db_session_fixture):
        """maybe_trigger_auto_refill must return False without creating a missing TokenBalance row."""
        from backend.models import TokenBalance
        from backend.routes.billing import maybe_trigger_auto_refill

        db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).delete()
        db_session_fixture.commit()

        with patch('stripe.PaymentIntent.create') as mock_pi:
            assert maybe_trigger_auto_refill(test_user.id, db_session_fixture, required_cost=2) is False

        mock_pi.assert_not_called()
        assert db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).first() is None


class TestAutoRefillFrontendUI:
    """Verify #billing-account-modal controls and #auto-refill-failure-banner placement."""

    def test_billing_modal_replaces_coming_soon_and_surfaces_banner(self):
        """Controls must live in #billing-account-modal only (never #buy-tokens-modal or extension)."""
        import pathlib
        import re

        frontend_dir = pathlib.Path(__file__).resolve().parents[1] / 'frontend'
        index_html = (frontend_dir / 'index.html').read_text(encoding='utf-8')
        ext_html = (
            pathlib.Path(__file__).resolve().parents[1] / 'extension' / 'sidepanel' / 'sidepanel.html'
        ).read_text(encoding='utf-8')

        assert 'Subscriptions coming soon!' not in index_html
        assert 'id="auto-refill-failure-banner"' in index_html
        assert 'Automatic refill failed. Check billing.' in index_html
        assert 'id="auto-refill-banner-link"' in index_html
        assert 'href="#billing-account-modal"' in index_html

        billing_modal_match = re.search(
            r'id="billing-account-modal"(.*?)id="streak-modal"',
            index_html,
            re.DOTALL,
        )
        assert billing_modal_match is not None
        billing_modal_html = billing_modal_match.group(1)

        assert 'id="auto-refill-toggle"' in billing_modal_html
        assert 'id="auto-refill-package"' in billing_modal_html
        assert 'value="battery_medium"' in billing_modal_html
        assert 'value="battery_large"' in billing_modal_html
        assert 'value="battery_small"' not in billing_modal_html
        assert 'value="wizard"' not in billing_modal_html
        assert 'id="auto-refill-card-status"' in billing_modal_html
        assert 'id="auto-refill-card-btn"' in billing_modal_html

        buy_modal_match = re.search(
            r'id="buy-tokens-modal"(.*?)id="settings-modal"',
            index_html,
            re.DOTALL,
        )
        assert buy_modal_match is not None
        assert 'auto-refill-toggle' not in buy_modal_match.group(1)
        assert 'auto-refill-toggle' not in ext_html
