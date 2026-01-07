"""
Stripe billing integration routes.
Handles token purchases and subscription management.
"""
from flask import Blueprint, request, jsonify, redirect
import stripe
import logging
from datetime import datetime, timedelta

from backend.database import db_session
from backend.models import TokenBalance, Transaction
from backend.routes.auth import login_required
from backend.config import Config
from backend.error_handlers import APIError

logger = logging.getLogger(__name__)

billing_bp = Blueprint('billing', __name__, url_prefix='/api/billing')

# Configure Stripe
if Config.STRIPE_SECRET_KEY:
    stripe.api_key = Config.STRIPE_SECRET_KEY


@billing_bp.route('/packages', methods=['GET'])
def get_packages():
    """Get available token packages."""
    return jsonify({
        'success': True,
        'packages': Config.TOKEN_PACKAGES,
        'tokens_per_cp_unit': Config.TOKENS_PER_CP_UNIT,
        'words_per_cp': Config.WORDS_PER_CP
    })


@billing_bp.route('/create-checkout', methods=['POST'])
@login_required
def create_checkout_session():
    """Create Stripe checkout session for token purchase."""
    try:
        data = request.get_json()
        
        if not data:
            raise APIError('No data provided')
        
        package_type = data.get('package_type')
        
        if package_type not in Config.TOKEN_PACKAGES:
            raise APIError('Invalid package type')
        
        user = request.current_user
        package = Config.TOKEN_PACKAGES[package_type]
        
        # Create or get Stripe customer
        token_balance = db_session.query(TokenBalance).filter_by(user_id=user.id).first()
        
        stripe_customer_id = None
        if token_balance:
            # Try to find existing customer from transactions
            last_transaction = db_session.query(Transaction).filter_by(
                user_id=user.id
            ).filter(
                Transaction.stripe_customer_id.isnot(None)
            ).first()
            
            if last_transaction:
                stripe_customer_id = last_transaction.stripe_customer_id
        
        if not stripe_customer_id:
            # Create new Stripe customer
            try:
                customer = stripe.Customer.create(
                    email=user.email,
                    metadata={'user_id': user.id}
                )
                stripe_customer_id = customer.id
            except stripe.error.StripeError as e:
                logger.error(f"Failed to create Stripe customer for {user.email}: {str(e)}", exc_info=True)
                raise APIError('Payment system temporarily unavailable. Please try again.')
        
        # Determine if this is a subscription or one-time payment
        try:
            # Use pre-created Stripe Price IDs for better promo code compatibility
            session = stripe.checkout.Session.create(
                customer=stripe_customer_id,
                payment_method_types=['card'],
                line_items=[{
                    'price': package['stripe_price_id'],
                    'quantity': 1
                }],
                mode='payment',
                allow_promotion_codes=True,
                success_url=request.host_url + 'api/billing/success?session_id={CHECKOUT_SESSION_ID}',
                cancel_url=request.host_url + '?payment=cancelled',
                metadata={
                    'user_id': user.id,
                    'package_type': package_type,
                    'tokens': package['tokens']
                }
            )
        except stripe.error.CardError as e:
            logger.error(f"Stripe card error for user {user.email}: {str(e)}", exc_info=True)
            raise APIError('Payment failed. Please check your card details.')
        except stripe.error.RateLimitError as e:
            logger.error(f"Stripe rate limit hit: {str(e)}", exc_info=True)
            raise APIError('Payment system is busy. Please try again in a moment.')
        except stripe.error.InvalidRequestError as e:
            logger.error(f"Invalid Stripe request for user {user.email}: {str(e)}", exc_info=True)
            raise APIError('Payment request invalid. Please contact support.')
        except stripe.error.AuthenticationError as e:
            logger.error(f"Stripe authentication failed: {str(e)}", exc_info=True)
            raise APIError('Payment system configuration error. Please contact support.')
        except stripe.error.APIConnectionError as e:
            logger.error(f"Stripe API connection failed: {str(e)}", exc_info=True)
            raise APIError('Cannot connect to payment system. Please check your internet connection.')
        except stripe.error.StripeError as e:
            logger.error(f"Generic Stripe error for user {user.email}: {str(e)}", exc_info=True)
            raise APIError('Payment failed. Please try again.')
        
        logger.info(f"Created Stripe checkout session for user {user.email}, package {package_type}")
        
        return jsonify({
            'success': True,
            'session_id': session.id,
            'url': session.url
        })
        
    except APIError:
        raise
    except Exception as e:
        logger.error(f"Unexpected checkout creation error for user {request.current_user.email}: {str(e)}", exc_info=True)
        raise APIError('Payment initialization failed. Please try again.')


@billing_bp.route('/success', methods=['GET'])
def payment_success():
    """Handle successful payment."""
    try:
        session_id = request.args.get('session_id')
        
        if not session_id:
            logger.warning("Payment success called without session_id")
            return redirect('/?error=no_session')
        
        # Retrieve session from Stripe
        try:
            session = stripe.checkout.Session.retrieve(session_id)
        except stripe.error.StripeError as e:
            logger.error(f"Failed to retrieve Stripe session {session_id}: {str(e)}", exc_info=True)
            return redirect('/?error=payment_verification_failed')
        
        if session.payment_status not in ['paid', 'no_payment_required'] and session.status != 'complete':
            logger.warning(f"Payment incomplete for session {session_id}: status={session.status}, payment_status={session.payment_status}")
            return redirect('/?error=payment_not_complete')
        
        # DUPLICATE PROTECTION: Check if this session was already processed
        existing_transaction = db_session.query(Transaction).filter_by(stripe_session_id=session_id).first()
        if existing_transaction:
            logger.info(f"Session {session_id} already processed, redirecting")
            return redirect('/?payment=success')
        
        user_id = int(session.metadata['user_id'])
        package_type = session.metadata['package_type']
        tokens = int(session.metadata['tokens'])
        
        # Get user's token balance
        token_balance = db_session.query(TokenBalance).filter_by(user_id=user_id).first()
        if not token_balance:
            token_balance = TokenBalance(user_id=user_id, balance=0)
            db_session.add(token_balance)
        
        # Add tokens
        token_balance.balance += tokens
        token_balance.last_updated = datetime.utcnow()
        
        # If wizard subscription, set up recurring
        if package_type == 'wizard':
            token_balance.is_wizard = True
            token_balance.wizard_start_date = datetime.utcnow()
            token_balance.wizard_months_remaining = Config.TOKEN_PACKAGES['wizard']['duration']
        
        # Record transaction
        transaction = Transaction(
            user_id=user_id,
            type='purchase',
            amount=tokens,
            description=f"Purchased {Config.TOKEN_PACKAGES[package_type]['name']}",
            stripe_session_id=session_id,
            stripe_customer_id=session.customer,
            package_type=package_type,
            timestamp=datetime.utcnow()
        )
        db_session.add(transaction)
        
        db_session.commit()
        
        logger.info(f"Payment successful for user {user_id}, added {tokens} CP")
        
        return redirect('/?payment=success')
        
    except stripe.error.StripeError as e:
        logger.error(f"Stripe error in payment success handler: {str(e)}", exc_info=True)
        db_session.rollback()
        return redirect('/?error=payment_processing')
    except Exception as e:
        logger.error(f"Unexpected payment success handler error: {str(e)}", exc_info=True)
        db_session.rollback()
        return redirect('/?error=payment_processing')


@billing_bp.route('/create-portal', methods=['POST'])
@login_required
def create_portal_session():
    """Create Stripe billing portal session."""
    try:
        user = request.current_user
        
        # Find Stripe customer ID
        transaction = db_session.query(Transaction).filter_by(
            user_id=user.id
        ).filter(
            Transaction.stripe_customer_id.isnot(None)
        ).first()
        
        if not transaction:
            raise APIError('No billing history found')
        
        # Create portal session
        portal_session = stripe.billing_portal.Session.create(
            customer=transaction.stripe_customer_id,
            return_url=request.host_url
        )
        
        return jsonify({
            'success': True,
            'url': portal_session.url
        })
        
    except APIError:
        raise
    except stripe.error.StripeError as e:
        logger.error(f"Stripe portal error: {e}", exc_info=True)
        raise APIError('Billing portal temporarily unavailable. Please try again.')
    except Exception as e:
        logger.error(f"Portal creation error: {e}", exc_info=True)
        raise APIError('Failed to create billing portal')


@billing_bp.route('/webhook', methods=['POST'])
def stripe_webhook():
    """Handle Stripe webhooks for subscription events."""
    try:
        payload = request.get_data()
        sig_header = request.headers.get('Stripe-Signature')
        
        if not Config.STRIPE_WEBHOOK_SECRET:
            logger.warning("Stripe webhook secret not configured")
            return jsonify({'success': True})
        
        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, Config.STRIPE_WEBHOOK_SECRET
            )
        except ValueError:
            raise APIError('Invalid payload', status_code=400)
        except stripe.error.SignatureVerificationError:
            raise APIError('Invalid signature', status_code=400)
        
        # Handle one-time payments (Batteries)
        if event['type'] == 'checkout.session.completed':
            session = event['data']['object']
            
            # Check if already processed (idempotency)
            existing_transaction = db_session.query(Transaction).filter_by(stripe_session_id=session['id']).first()
            if existing_transaction:
                logger.info(f"Webhook: Session {session['id']} already processed")
                return jsonify({'success': True})
            
            # Extract metadata
            if 'metadata' in session and 'user_id' in session['metadata']:
                user_id = int(session['metadata']['user_id'])
                package_type = session['metadata']['package_type']
                tokens = int(session['metadata']['tokens'])
                
                # Fulfill order
                token_balance = db_session.query(TokenBalance).filter_by(user_id=user_id).first()
                if not token_balance:
                    token_balance = TokenBalance(user_id=user_id, balance=0)
                    db_session.add(token_balance)
                
                token_balance.balance += tokens
                token_balance.last_updated = datetime.utcnow()
                
                # Handle Wizard setup via webhook (backup)
                if package_type == 'wizard':
                    token_balance.is_wizard = True
                    token_balance.wizard_start_date = datetime.utcnow()
                    token_balance.wizard_months_remaining = Config.TOKEN_PACKAGES['wizard']['duration']
                
                # Record transaction
                transaction = Transaction(
                    user_id=user_id,
                    type='purchase',
                    amount=tokens,
                    description=f"Webhook: Purchased {Config.TOKEN_PACKAGES[package_type]['name']}",
                    stripe_session_id=session['id'],
                    stripe_customer_id=session.get('customer'),
                    package_type=package_type,
                    timestamp=datetime.utcnow()
                )
                db_session.add(transaction)
                db_session.commit()
                logger.info(f"Webhook: Fulfilled order for user {user_id}")
        
        # Handle subscription renewal (Wizard Refills)
        if event['type'] == 'invoice.payment_succeeded':
            invoice = event['data']['object']
            customer_id = invoice['customer']
            
            # Find user by customer ID
            transaction = db_session.query(Transaction).filter_by(
                stripe_customer_id=customer_id,
                package_type='wizard'
            ).first()
            
            if transaction:
                # Refill wizard tokens
                token_balance = db_session.query(TokenBalance).filter_by(
                    user_id=transaction.user_id
                ).first()
                
                if token_balance and token_balance.is_wizard:
                    # Set balance to refill amount
                    token_balance.balance = Config.WIZARD_REFILL_AMOUNT
                    token_balance.last_updated = datetime.utcnow()
                    
                    # Decrement months remaining
                    if token_balance.wizard_months_remaining > 0:
                        token_balance.wizard_months_remaining -= 1
                    
                    # If subscription complete, disable wizard
                    if token_balance.wizard_months_remaining == 0:
                        token_balance.is_wizard = False
                    
                    # Record transaction
                    refill_transaction = Transaction(
                        user_id=transaction.user_id,
                        type='purchase',
                        amount=Config.WIZARD_REFILL_AMOUNT,
                        description='Wizard monthly refill',
                        stripe_customer_id=customer_id,
                        package_type='wizard',
                        timestamp=datetime.utcnow()
                    )
                    db_session.add(refill_transaction)
                    
                    db_session.commit()
                    
                    logger.info(f"Wizard subscription refilled for user {transaction.user_id}")
        
        return jsonify({'success': True})
        
    except APIError:
        raise
    except Exception as e:
        logger.error(f"Webhook error: {e}", exc_info=True)
        raise APIError('Webhook processing failed')


@billing_bp.route('/wizard-refill', methods=['POST'])
def wizard_monthly_refill():
    """
    Monthly wizard token refill - called by Cloud Scheduler.
    
    Cloud Scheduler setup:
    - Frequency: 0 0 1 * * (1st of every month at midnight)
    - Target: POST /api/billing/wizard-refill
    - Auth: Include SCHEDULER_SECRET in header
    """
    try:
        # Verify scheduler secret (simple auth for cron job)
        scheduler_secret = request.headers.get('X-Scheduler-Secret')
        expected_secret = Config.SECRET_KEY[:16]  # Use first 16 chars of secret key
        
        if scheduler_secret != expected_secret:
            logger.warning("Wizard refill called with invalid secret")
            raise APIError('Unauthorized', status_code=401)
        
        # Find all active wizard users
        wizard_balances = db_session.query(TokenBalance).filter(
            TokenBalance.is_wizard == True,
            TokenBalance.wizard_months_remaining > 0
        ).all()
        
        refilled_count = 0
        expired_count = 0
        
        for balance in wizard_balances:
            # Refill CP
            balance.balance = Config.WIZARD_REFILL_AMOUNT
            balance.last_updated = datetime.utcnow()
            
            # Decrement months remaining
            balance.wizard_months_remaining -= 1
            
            # Check if expired
            if balance.wizard_months_remaining == 0:
                balance.is_wizard = False
                expired_count += 1
            
            # Record transaction
            refill_transaction = Transaction(
                user_id=balance.user_id,
                type='bonus',
                amount=Config.WIZARD_REFILL_AMOUNT,
                description='Wizard monthly refill',
                package_type='wizard',
                timestamp=datetime.utcnow()
            )
            db_session.add(refill_transaction)
            refilled_count += 1
        
        db_session.commit()
        
        logger.info(f"Wizard refill complete: {refilled_count} refilled, {expired_count} expired")
        
        return jsonify({
            'success': True,
            'refilled': refilled_count,
            'expired': expired_count
        })
        
    except APIError:
        raise
    except Exception as e:
        logger.error(f"Wizard refill error: {e}", exc_info=True)
        db_session.rollback()
        raise APIError('Wizard refill failed')

