"""
Stripe billing integration routes.
Handles token purchases, subscription management, and automated off-session CP refills.
"""
from flask import Blueprint, request, jsonify, redirect
import stripe
import logging
import threading
from datetime import datetime, UTC

from backend.database import db_session
from backend.models import TokenBalance, Transaction, record_audit_event
from backend.routes.auth import login_required, scheduler_secret_required
from backend.config import Config
from backend.error_handlers import APIError
from backend.extensions import limiter

logger = logging.getLogger(__name__)

billing_bp = Blueprint('billing', __name__, url_prefix='/api/billing')

_default_db_session = db_session

AUTO_REFILL_PACKAGES = ('battery_medium', 'battery_large')
_AUTO_REFILL_LOCKS_GUARD = threading.Lock()
_AUTO_REFILL_USER_LOCKS: dict[int, threading.Lock] = {}

# Configure Stripe
if Config.STRIPE_SECRET_KEY:
    stripe.api_key = Config.STRIPE_SECRET_KEY


def _get_user_refill_lock(user_id: int) -> threading.Lock:
    """Return a per-user lock to serialize concurrent auto-refill triggers."""
    with _AUTO_REFILL_LOCKS_GUARD:
        lock = _AUTO_REFILL_USER_LOCKS.get(user_id)
        if lock is None:
            lock = threading.Lock()
            _AUTO_REFILL_USER_LOCKS[user_id] = lock
        return lock


def _extract_stripe_metadata(obj) -> dict:
    """Safely convert StripeObject metadata to a plain dict (stripe-python v15+)."""
    raw_metadata = getattr(obj, 'metadata', None)
    if raw_metadata is None:
        return {}
    if hasattr(raw_metadata, 'to_dict'):
        return raw_metadata.to_dict() or {}
    if isinstance(raw_metadata, dict):
        return raw_metadata
    return dict(raw_metadata)


def _default_auto_refill_package(token_balance) -> str:
    """Wizard accounts default to battery_large; all others default to battery_medium."""
    if token_balance and token_balance.is_wizard:
        return 'battery_large'
    return 'battery_medium'


def _resolve_auto_refill_package(token_balance, requested=None) -> str:
    """Validate requested package or resolve stored/default eligible auto-refill package."""
    if requested is not None:
        if not isinstance(requested, str) or requested.strip() not in AUTO_REFILL_PACKAGES:
            raise APIError(
                'Eligible auto-refill packages are battery_medium and battery_large only.',
                status_code=400,
            )
        return requested.strip()

    if token_balance and token_balance.auto_refill_package in AUTO_REFILL_PACKAGES:
        return token_balance.auto_refill_package

    return _default_auto_refill_package(token_balance)


def _serialize_auto_refill_state(token_balance) -> dict:
    """Serialize TokenBalance auto-refill configuration for API responses."""
    is_wizard = bool(token_balance and token_balance.is_wizard)
    enabled = bool(token_balance and token_balance.auto_refill_enabled)
    package_type = _resolve_auto_refill_package(token_balance)
    threshold = (
        token_balance.auto_refill_threshold
        if token_balance and token_balance.auto_refill_threshold is not None
        else 0
    )
    has_payment_method = bool(token_balance and token_balance.stripe_payment_method_id)
    failed = bool(token_balance and token_balance.auto_refill_failed)

    return {
        'success': True,
        'enabled': enabled,
        'package_type': package_type,
        'threshold': threshold,
        'has_payment_method': has_payment_method,
        'failed': failed,
        'auto_refill_failed': failed,
        'is_wizard': is_wizard,
        'eligible_packages': [
            {
                'id': pkg_id,
                'name': Config.TOKEN_PACKAGES[pkg_id]['name'],
                'tokens': Config.TOKEN_PACKAGES[pkg_id]['tokens'],
                'price': Config.TOKEN_PACKAGES[pkg_id]['price'],
                'currency': Config.TOKEN_PACKAGES[pkg_id]['currency'],
            }
            for pkg_id in AUTO_REFILL_PACKAGES
        ],
    }


def _find_existing_stripe_customer_id(user_id: int, token_balance=None, active_db=None) -> str | None:
    """Locate an existing Stripe customer ID from TokenBalance or past Transactions."""
    session_db = active_db if active_db is not None else _default_db_session
    if token_balance and token_balance.stripe_customer_id:
        return token_balance.stripe_customer_id

    last_transaction = session_db.query(Transaction).filter_by(
        user_id=user_id
    ).filter(
        Transaction.stripe_customer_id.isnot(None)
    ).first()
    if last_transaction:
        return last_transaction.stripe_customer_id
    return None


def _fulfill_purchase(
    user_id,
    package_type,
    tokens,
    session_id,
    customer_id,
    db_session,
    description=None,
    clear_auto_refill_failed=False,
):
    """
    Internal helper to fulfill a token purchase or auto-refill.
    Used by webhooks and synchronous off-session auto-refill.
    """
    # DUPLICATE PROTECTION: Check if this session/intent was already processed
    existing_transaction = db_session.query(Transaction).filter_by(stripe_session_id=session_id).first()
    if existing_transaction:
        logger.info(f"Purchase already fulfilled for session {session_id}")
        return True

    # Get user's token balance
    token_balance = db_session.query(TokenBalance).filter_by(user_id=user_id).first()
    if not token_balance:
        token_balance = TokenBalance(user_id=user_id, balance=0)
        db_session.add(token_balance)
    
    # Add tokens
    token_balance.balance += tokens
    token_balance.last_updated = datetime.now(UTC)
    if clear_auto_refill_failed:
        token_balance.auto_refill_failed = False
    if customer_id and not token_balance.stripe_customer_id:
        token_balance.stripe_customer_id = customer_id
    
    # If wizard subscription, set up recurring
    # Note: Wizard refill is handled separately by monthly cron/webhook
    if package_type == 'wizard':
        token_balance.is_wizard = True
        token_balance.wizard_start_date = datetime.now(UTC)
        token_balance.wizard_months_remaining = Config.TOKEN_PACKAGES['wizard']['duration']
    
    # Record transaction
    tx_description = description or f"Purchased {Config.TOKEN_PACKAGES.get(package_type, {}).get('name', package_type)}"
    transaction = Transaction(
        user_id=user_id,
        type='purchase',
        amount=tokens,
        description=tx_description,
        stripe_session_id=session_id,
        stripe_customer_id=customer_id,
        package_type=package_type,
        timestamp=datetime.now(UTC)
    )
    db_session.add(transaction)
    
    db_session.commit()
    logger.info(f"Fulfillment successful for user {user_id}, added {tokens} CP (Session: {session_id})")
    return True


def maybe_trigger_auto_refill(user_id: int, db_session=None, required_cost: int = 0) -> bool:
    """Trigger an off-session Stripe PaymentIntent ONLY when CP run out or are insufficient.

    Returns True if an off-session refill succeeded and credited CP, False otherwise.
    Never triggers while sufficient non-zero CP remain.
    Never creates a missing TokenBalance row.
    """
    active_db = db_session if db_session is not None else _default_db_session

    with _get_user_refill_lock(user_id):
        token_balance = active_db.query(TokenBalance).filter_by(
            user_id=user_id
        ).with_for_update().first()

        if not token_balance:
            return False
        if not token_balance.auto_refill_enabled:
            return False
        if not token_balance.stripe_customer_id or not token_balance.stripe_payment_method_id:
            return False

        current_balance = token_balance.balance or 0
        is_out_of_cp = current_balance <= 0
        is_insufficient_for_action = required_cost > 0 and current_balance < required_cost
        if not (is_out_of_cp or is_insufficient_for_action):
            return False

        package_type = _resolve_auto_refill_package(token_balance)
        pkg = Config.TOKEN_PACKAGES[package_type]
        amount = pkg['price']
        currency = pkg['currency']
        tokens = pkg['tokens']

        try:
            intent = stripe.PaymentIntent.create(
                amount=amount,
                currency=currency,
                customer=token_balance.stripe_customer_id,
                payment_method=token_balance.stripe_payment_method_id,
                off_session=True,
                confirm=True,
                metadata={
                    'user_id': str(user_id),
                    'package_type': package_type,
                    'tokens': str(tokens),
                    'auto_refill': 'true',
                },
            )
        except (stripe.error.CardError, stripe.error.StripeError) as exc:
            logger.warning(
                f"Auto-refill off-session charge failed for user {user_id}: {type(exc).__name__}"
            )
            token_balance.auto_refill_enabled = False
            token_balance.auto_refill_failed = True
            token_balance.last_updated = datetime.now(UTC)
            active_db.commit()
            return False

        intent_status = getattr(intent, 'status', None)
        if intent_status == 'succeeded':
            intent_id = getattr(intent, 'id', None)
            _fulfill_purchase(
                user_id=user_id,
                package_type=package_type,
                tokens=tokens,
                session_id=intent_id,
                customer_id=token_balance.stripe_customer_id,
                db_session=active_db,
                description=f"Auto-refill: {pkg['name']}",
                clear_auto_refill_failed=True,
            )
            logger.info(
                f"Auto-refill succeeded for user {user_id}: +{tokens} CP ({package_type}, intent={intent_id})"
            )
            return True

        logger.warning(
            f"Auto-refill PaymentIntent returned non-succeeded status '{intent_status}' for user {user_id}"
        )
        token_balance.auto_refill_enabled = False
        token_balance.auto_refill_failed = True
        token_balance.last_updated = datetime.now(UTC)
        active_db.commit()
        return False


@billing_bp.route('/packages', methods=['GET'])
def get_packages():
    """Get available token packages."""
    return jsonify({
        'success': True,
        'packages': Config.TOKEN_PACKAGES
    })


@billing_bp.route('/checkout', methods=['POST'])
@login_required
@limiter.limit("10 per minute")
def create_checkout_session():
    """Create Stripe checkout session for token purchase."""
    try:
        data = request.get_json()
        
        if not data:
            logger.warning("Checkout: No data provided")
            raise APIError('No data provided')
        
        # Support both 'package' and 'package_type' for compatibility
        package_type = data.get('package_type') or data.get('package')
        
        if not package_type or package_type not in Config.TOKEN_PACKAGES:
            logger.warning(f"Checkout: Invalid package type '{package_type}'")
            raise APIError(f"Invalid package type: {package_type}")
        
        user = request.current_user
        package = Config.TOKEN_PACKAGES[package_type]
        
        # Create or get Stripe customer
        token_balance = db_session.query(TokenBalance).filter_by(user_id=user.id).first()
        stripe_customer_id = _find_existing_stripe_customer_id(user.id, token_balance)
        
        if not stripe_customer_id:
            # Create new Stripe customer
            try:
                customer = stripe.Customer.create(
                    email=user.email,
                    metadata={'user_id': user.id}
                )
                stripe_customer_id = customer.id
            except stripe.error.StripeError as e:
                logger.error(f"Failed to create Stripe customer for user_id={user.id}: {type(e).__name__}", exc_info=True)
                raise APIError('Payment system temporarily unavailable. Please try again.')
        
        # Determine if this is a subscription or one-time payment
        try:
            # Use pre-created Stripe Price IDs for better promo code compatibility
            # Note: Do not pass `payment_method_types` — Stripe manages enabled payment
            # methods dynamically via Dashboard settings (passing it triggers InvalidRequestError).
            checkout_session = stripe.checkout.Session.create(
                customer=stripe_customer_id,
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
            logger.error(f"Stripe card error for user_id={user.id}: {type(e).__name__}", exc_info=True)
            raise APIError('Payment failed. Please check your card details.')
        except stripe.error.RateLimitError as e:
            logger.error(f"Stripe rate limit hit: {type(e).__name__}", exc_info=True)
            raise APIError('Payment system is busy. Please try again in a moment.')
        except stripe.error.InvalidRequestError as e:
            logger.error(f"Invalid Stripe request for user_id={user.id}: {type(e).__name__}", exc_info=True)
            raise APIError('Payment request invalid. Please contact support.')
        except stripe.error.AuthenticationError as e:
            logger.error(f"Stripe authentication failed: {type(e).__name__}", exc_info=True)
            raise APIError('Payment system configuration error. Please contact support.')
        except stripe.error.APIConnectionError as e:
            logger.error(f"Stripe API connection failed: {type(e).__name__}", exc_info=True)
            raise APIError('Cannot connect to payment system. Please check your internet connection.')
        except stripe.error.StripeError as e:
            logger.error(f"Generic Stripe error for user_id={user.id}: {type(e).__name__}", exc_info=True)
            raise APIError('Payment failed. Please try again.')
        
        logger.info(f"Created Stripe checkout session for user_id={user.id}, package={package_type}")
        
        return jsonify({
            'success': True,
            'session_id': checkout_session.id,
            'url': checkout_session.url
        })
        
    except APIError:
        raise
    except Exception as e:
        logger.error(f"Unexpected checkout creation error for user_id={request.current_user.id}: {type(e).__name__}", exc_info=True)
        raise APIError('Payment initialization failed. Please try again.')

@billing_bp.route('/success', methods=['GET'])
def payment_success():
    """Handle successful payment redirect from Stripe.

    This endpoint is the Stripe success_url callback. It ONLY redirects the
    user back to the app with a status indicator. Actual token fulfillment
    is handled exclusively by the webhook (with signature verification).
    """
    session_id = request.args.get('session_id')

    if not session_id:
        logger.warning("Payment success called without session_id")
        return redirect('/?error=no_session')

    # Just redirect — the webhook handles fulfillment
    return redirect('/?payment=success')


@billing_bp.route('/create-portal', methods=['POST'])
@login_required
@limiter.limit("10 per minute")
def create_portal_session():
    """Create Stripe billing portal session."""
    try:
        user = request.current_user
        
        # Find Stripe customer ID from TokenBalance or past Transactions
        token_balance = db_session.query(TokenBalance).filter_by(user_id=user.id).first()
        customer_id = _find_existing_stripe_customer_id(user.id, token_balance)
        
        if not customer_id:
            raise APIError('No billing history found')
        
        # Create portal session
        portal_session = stripe.billing_portal.Session.create(
            customer=customer_id,
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


@billing_bp.route('/auto-refill', methods=['GET'])
@login_required
def get_auto_refill():
    """Return the user's automated off-session CP refill configuration (DB-only, no live Stripe calls)."""
    user = request.current_user
    token_balance = db_session.query(TokenBalance).filter_by(user_id=user.id).first()
    return jsonify(_serialize_auto_refill_state(token_balance))


@billing_bp.route('/auto-refill', methods=['POST'])
@login_required
@limiter.limit("30 per minute")
def update_auto_refill():
    """Update automated off-session CP refill configuration."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not data:
        raise APIError('No data provided', status_code=400)

    user = request.current_user
    token_balance = db_session.query(TokenBalance).filter_by(user_id=user.id).first()
    if not token_balance:
        token_balance = TokenBalance(user_id=user.id, balance=0)
        db_session.add(token_balance)
        db_session.flush()

    if 'enabled' in data and not isinstance(data['enabled'], bool):
        raise APIError('enabled must be a boolean', status_code=400)

    if 'package_type' in data or 'package' in data:
        raw_pkg = data['package_type'] if 'package_type' in data else data['package']
        resolved_pkg = _resolve_auto_refill_package(
            token_balance,
            requested=raw_pkg if raw_pkg is not None else '',
        )
    else:
        resolved_pkg = _resolve_auto_refill_package(token_balance)

    if 'threshold' in data:
        threshold_val = data['threshold']
        if isinstance(threshold_val, bool) or not isinstance(threshold_val, int) or threshold_val < 0:
            raise APIError('threshold must be a non-negative integer', status_code=400)

    target_enabled = data['enabled'] if 'enabled' in data else bool(token_balance.auto_refill_enabled)
    if target_enabled and not token_balance.stripe_payment_method_id:
        raise APIError(
            'A saved payment method is required to enable automatic refill.',
            status_code=400,
            payload={'requires_payment_method': True},
        )

    token_balance.auto_refill_package = resolved_pkg
    if 'enabled' in data:
        token_balance.auto_refill_enabled = data['enabled']
    if 'threshold' in data:
        token_balance.auto_refill_threshold = data['threshold']
    elif token_balance.auto_refill_threshold is None:
        token_balance.auto_refill_threshold = 0
    token_balance.auto_refill_failed = False
    token_balance.last_updated = datetime.now(UTC)
    db_session.commit()

    logger.info(
        f"Updated auto-refill config for user {user.id}: "
        f"enabled={token_balance.auto_refill_enabled}, package={resolved_pkg}"
    )
    return jsonify(_serialize_auto_refill_state(token_balance))


@billing_bp.route('/auto-refill/setup', methods=['POST'])
@login_required
@limiter.limit("10 per minute")
def create_auto_refill_setup_session():
    """Create a Stripe Checkout Session in setup mode to save a card for off-session auto-refill."""
    try:
        data = request.get_json(silent=True)
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise APIError('Invalid request payload', status_code=400)

        user = request.current_user
        token_balance = db_session.query(TokenBalance).filter_by(user_id=user.id).first()
        if not token_balance:
            token_balance = TokenBalance(user_id=user.id, balance=0)
            db_session.add(token_balance)
            db_session.flush()

        if 'package_type' in data or 'package' in data:
            raw_pkg = data['package_type'] if 'package_type' in data else data['package']
            resolved_pkg = _resolve_auto_refill_package(
                token_balance,
                requested=raw_pkg if raw_pkg is not None else '',
            )
            token_balance.auto_refill_package = resolved_pkg
        else:
            resolved_pkg = _resolve_auto_refill_package(token_balance)

        stripe_customer_id = _find_existing_stripe_customer_id(user.id, token_balance)
        if not stripe_customer_id:
            try:
                customer = stripe.Customer.create(
                    email=user.email,
                    metadata={'user_id': user.id},
                )
                stripe_customer_id = customer.id
            except stripe.error.StripeError as exc:
                logger.error(f"Failed to create Stripe customer for user {user.id}: {type(exc).__name__}")
                raise APIError('Payment system temporarily unavailable. Please try again.')

        token_balance.stripe_customer_id = stripe_customer_id
        db_session.commit()

        try:
            # Note: Do not pass `payment_method_types` — Stripe manages enabled payment
            # methods dynamically via Dashboard settings.
            checkout_session = stripe.checkout.Session.create(
                customer=stripe_customer_id,
                mode='setup',
                currency='chf',
                success_url=request.host_url + '?auto_refill=setup_success',
                cancel_url=request.host_url + '?auto_refill=setup_cancelled',
                metadata={
                    'user_id': str(user.id),
                    'purpose': 'auto_refill_setup',
                    'package_type': resolved_pkg,
                },
            )
        except stripe.error.InvalidRequestError as exc:
            logger.error(f"Invalid Stripe setup request for user {user.id}: {type(exc).__name__}")
            raise APIError('Payment setup request invalid. Please contact support.')
        except stripe.error.StripeError as exc:
            logger.error(f"Stripe setup session error for user {user.id}: {type(exc).__name__}")
            raise APIError('Payment setup failed. Please try again.')

        logger.info(f"Created auto-refill setup session for user {user.id}")
        return jsonify({
            'success': True,
            'session_id': checkout_session.id,
            'url': checkout_session.url,
        })

    except APIError:
        db_session.rollback()
        raise
    except Exception as exc:
        db_session.rollback()
        logger.error(f"Unexpected auto-refill setup error for user {request.current_user.id}: {exc}", exc_info=True)
        raise APIError('Payment setup failed. Please try again.')


@billing_bp.route('/webhook', methods=['POST'])
def stripe_webhook():
    """Handle Stripe webhooks for purchases, auto-refill setup, off-session charges, and legacy wizard events."""
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
        
        # Handle Checkout completion (setup mode BEFORE purchase fulfillment)
        if event['type'] == 'checkout.session.completed':
            session = event['data']['object']
            session_mode = getattr(session, 'mode', None)
            metadata = _extract_stripe_metadata(session)

            if session_mode == 'setup':
                user_id_raw = metadata.get('user_id')
                if user_id_raw:
                    try:
                        user_id = int(user_id_raw)
                    except (TypeError, ValueError):
                        logger.warning("Webhook setup session had invalid user_id metadata")
                        return jsonify({'success': True})

                    setup_intent_ref = getattr(session, 'setup_intent', None)
                    setup_intent = None
                    if isinstance(setup_intent_ref, str) and setup_intent_ref:
                        setup_intent = stripe.SetupIntent.retrieve(setup_intent_ref)
                    elif setup_intent_ref is not None:
                        setup_intent = setup_intent_ref

                    pm_obj = getattr(setup_intent, 'payment_method', None) if setup_intent else None
                    payment_method_id = getattr(pm_obj, 'id', pm_obj) if pm_obj else None
                    customer_id = getattr(session, 'customer', None) or (
                        getattr(setup_intent, 'customer', None) if setup_intent else None
                    )

                    token_balance = db_session.query(TokenBalance).filter_by(user_id=user_id).first()
                    if not token_balance:
                        token_balance = TokenBalance(user_id=user_id, balance=0)
                        db_session.add(token_balance)

                    if customer_id:
                        token_balance.stripe_customer_id = customer_id
                    if payment_method_id:
                        token_balance.stripe_payment_method_id = payment_method_id
                        token_balance.auto_refill_failed = False

                    meta_pkg = metadata.get('package_type')
                    if meta_pkg in AUTO_REFILL_PACKAGES and not token_balance.auto_refill_package:
                        token_balance.auto_refill_package = meta_pkg

                    token_balance.last_updated = datetime.now(UTC)
                    db_session.commit()
                    logger.info(f"Webhook: Saved auto-refill payment method for user {user_id}")
            elif metadata and 'user_id' in metadata:
                user_id = int(metadata['user_id'])
                package_type = metadata.get('package_type')
                tokens = int(metadata.get('tokens', 0))
                
                # Fulfill purchase using centralized helper
                session_id = getattr(session, 'id', None)
                customer_id = getattr(session, 'customer', None)
                _fulfill_purchase(user_id, package_type, tokens, session_id, customer_id, db_session)
                record_audit_event(
                    db_session,
                    'billing.webhook_fulfilled',
                    user_id=user_id,
                    provider='stripe',
                    detail=f'package={package_type} tokens={tokens}',
                    commit=True,
                )
                logger.info(f"Webhook: Fulfilled order for user {user_id}")

        # Handle off-session auto-refill PaymentIntent fulfillment
        if event['type'] == 'payment_intent.succeeded':
            intent = event['data']['object']
            metadata = _extract_stripe_metadata(intent)
            if metadata.get('auto_refill') == 'true':
                user_id_raw = metadata.get('user_id')
                package_type = metadata.get('package_type')
                if user_id_raw and package_type in AUTO_REFILL_PACKAGES:
                    user_id = int(user_id_raw)
                    pkg = Config.TOKEN_PACKAGES[package_type]
                    intent_id = getattr(intent, 'id', None)
                    customer_id = getattr(intent, 'customer', None)
                    _fulfill_purchase(
                        user_id=user_id,
                        package_type=package_type,
                        tokens=pkg['tokens'],
                        session_id=intent_id,
                        customer_id=customer_id,
                        db_session=db_session,
                        description=f"Auto-refill: {pkg['name']}",
                        clear_auto_refill_failed=True,
                    )
                    record_audit_event(
                        db_session,
                        'billing.webhook_fulfilled',
                        user_id=user_id,
                        provider='stripe',
                        detail=f'auto_refill package={package_type} tokens={pkg["tokens"]}',
                        commit=True,
                    )
                    logger.info(f"Webhook: Fulfilled auto-refill PaymentIntent for user {user_id}")

        # Handle off-session auto-refill PaymentIntent failure
        if event['type'] == 'payment_intent.payment_failed':
            intent = event['data']['object']
            metadata = _extract_stripe_metadata(intent)
            if metadata.get('auto_refill') == 'true':
                user_id_raw = metadata.get('user_id')
                if user_id_raw:
                    user_id = int(user_id_raw)
                    token_balance = db_session.query(TokenBalance).filter_by(user_id=user_id).first()
                    if token_balance:
                        token_balance.auto_refill_enabled = False
                        token_balance.auto_refill_failed = True
                        token_balance.last_updated = datetime.now(UTC)
                        db_session.commit()
                        logger.warning(f"Webhook: Auto-refill payment failed for user {user_id}")
        
        # Handle subscription renewal (Wizard Refills)
        if event['type'] == 'invoice.payment_succeeded':
            invoice = event['data']['object']
            # Attribute access for stripe-python v15 compatibility
            customer_id = getattr(invoice, 'customer', None)
            
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
                    token_balance.last_updated = datetime.now(UTC)
                    
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
                        timestamp=datetime.now(UTC)
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
@scheduler_secret_required
def wizard_monthly_refill():
    """
    Monthly wizard token refill - called by Cloud Scheduler.
    
    Cloud Scheduler setup:
    - Frequency: 0 0 1 * * (1st of every month at midnight)
    - Target: POST /api/billing/wizard-refill
    - Auth: Include SCHEDULER_SECRET in X-Scheduler-Secret header
    """
    try:
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
            balance.last_updated = datetime.now(UTC)
            
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
                timestamp=datetime.now(UTC)
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

