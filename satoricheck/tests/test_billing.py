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
        """Stripe webhook should add tokens to user balance."""
        from backend.models import TokenBalance, Transaction
        
        # Get initial balance
        initial_balance = db_session_fixture.query(TokenBalance).filter_by(
            user_id=test_user.id
        ).first().balance
        
        # Mock Stripe signature verification
        with patch('stripe.Webhook.construct_event') as mock_construct:
            event = mock_stripe_webhook_payload(
                user_id=test_user.id,
                package_type='battery_small',
                tokens=86,
                session_id='cs_test_unique_123'
            )
            mock_construct.return_value = event
            
            # The raw body is irrelevant — construct_event is mocked.
            # Pass a stub so json.dumps doesn't need to serialize StripeObjects.
            response = client.post(
                '/api/billing/webhook',
                data=b'{"stub": true}',
                content_type='application/json',
                headers={'Stripe-Signature': 'test_sig'}
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
        """Same webhook sent twice should only credit once."""
        from backend.models import TokenBalance
        
        initial_balance = db_session_fixture.query(TokenBalance).filter_by(
            user_id=test_user.id
        ).first().balance
        
        event = mock_stripe_webhook_payload(
            user_id=test_user.id,
            tokens=100,
            session_id='cs_test_duplicate_456'
        )
        
        # Raw body is irrelevant — construct_event is mocked.
        stub_body = b'{"stub": true}'
        with patch('stripe.Webhook.construct_event', return_value=event):
            # Send webhook twice
            client.post('/api/billing/webhook', data=stub_body,
                       content_type='application/json', headers={'Stripe-Signature': 'sig'})
            client.post('/api/billing/webhook', data=stub_body,
                       content_type='application/json', headers={'Stripe-Signature': 'sig'})
        
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
        assert data['words_per_cp'] == Config.WORDS_PER_CP

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
