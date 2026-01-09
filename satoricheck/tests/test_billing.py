"""
Billing & Stripe Integration Tests.
Tests Payment Flow, Webhook Handling, and Idempotency.
"""
import pytest
import json
from unittest.mock import patch, MagicMock


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
            payload = mock_stripe_webhook_payload(
                user_id=test_user.id,
                package_type='battery_small',
                tokens=86,
                session_id='cs_test_unique_123'
            )
            mock_construct.return_value = payload
            
            response = client.post(
                '/api/billing/webhook',
                data=json.dumps(payload),
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
        
        payload = mock_stripe_webhook_payload(
            user_id=test_user.id,
            tokens=100,
            session_id='cs_test_duplicate_456'
        )
        
        with patch('stripe.Webhook.construct_event', return_value=payload):
            # Send webhook twice
            client.post('/api/billing/webhook', data=json.dumps(payload),
                       content_type='application/json', headers={'Stripe-Signature': 'sig'})
            client.post('/api/billing/webhook', data=json.dumps(payload),
                       content_type='application/json', headers={'Stripe-Signature': 'sig'})
        
        # Should only add 100 once, not 200
        final_balance = db_session_fixture.query(TokenBalance).filter_by(
            user_id=test_user.id
        ).first().balance
        
        assert final_balance == initial_balance + 100


class TestCheckoutSession:
    """Test checkout session creation."""
    
    def test_create_checkout_requires_auth(self, client):
        """Checkout endpoint should require authentication."""
        response = client.post('/api/billing/checkout', json={
            'package': 'battery_small'
        })
        
        assert response.status_code == 401
    
    def test_create_checkout_valid_package(self, auth_client):
        """Should create checkout session for valid package."""
        with patch('stripe.checkout.Session.create') as mock_create:
            mock_create.return_value = MagicMock(url='https://checkout.stripe.com/test')
            
            response = auth_client.post('/api/billing/checkout', json={
                'package': 'battery_small'
            })
        
        # Either 200 with URL or redirect
        assert response.status_code in [200, 302]
