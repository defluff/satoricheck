"""
Authentication routes.
Handles user registration, login, logout, and password changes.
Uses JWT tokens for stateless authentication (Cloud Run compatible).
"""
from flask import Blueprint, request, session, jsonify, make_response, url_for, redirect
from functools import wraps
import bcrypt
import logging
from datetime import datetime, UTC
import secrets
import hashlib
import json
import re

from backend.database import db_session
from backend.models import User, TokenBalance, Streak, DeletedUser
from backend.config import Config
from backend.error_handlers import APIError
from backend.services.streak import handle_login_streak
from backend.extensions import oauth
from backend.jwt_utils import create_token, verify_token, refresh_token_if_needed

logger = logging.getLogger(__name__)

# Import rate limiter from server
def get_limiter():
    """Lazy import to avoid circular dependency."""
    from backend.server import limiter
    return limiter

# Cookie settings
JWT_COOKIE_NAME = 'satori_token'
JWT_COOKIE_SECURE = Config.ENV != 'development'  # True in production
JWT_COOKIE_HTTPONLY = True
JWT_COOKIE_SAMESITE = 'Lax'

auth_bp = Blueprint('auth', __name__, url_prefix='/api/auth')


def login_required(f):
    """Decorator to require authentication via JWT or Bearer token."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Check if in test mode
        if Config.TEST_MODE:
            # Auto-create test user if not exists
            user = db_session.query(User).filter_by(email='test@satoricheck.com').first()
            if not user:
                user = create_test_user()
            request.current_user = user
            return f(*args, **kwargs)
        
        user = None
        
        # 1. Check JWT cookie (primary method for web app)
        jwt_token = request.cookies.get(JWT_COOKIE_NAME)
        if jwt_token:
            payload = verify_token(jwt_token)
            if payload:
                user = db_session.query(User).filter_by(id=payload['user_id']).first()
                if user:
                    request.current_user = user
                    # Refresh token if needed
                    new_token = refresh_token_if_needed(jwt_token)
                    if new_token:
                        request._refresh_token = new_token
                    return f(*args, **kwargs)
        
        # 2. Check Bearer token (for Chrome extension / API)
        auth_header = request.headers.get('Authorization')
        if auth_header and auth_header.startswith('Bearer '):
            token = auth_header.split(' ')[1]
            user = db_session.query(User).filter_by(api_token=token).first()
            if user:
                request.current_user = user
                return f(*args, **kwargs)
        
        # 3. Fallback to Flask session (legacy)
        user_id = session.get('user_id')
        if user_id:
            user = db_session.query(User).filter_by(id=user_id).first()
            if user:
                request.current_user = user
                return f(*args, **kwargs)
        
        raise APIError('Authentication required', status_code=401)
    
    return decorated_function


def provision_new_user(user, bonus_amount=None):
    """
    Create TokenBalance and Streak for a new user.
    Called during signup, Google OAuth registration, and test user creation.
    """
    if bonus_amount is None:
        bonus_amount = Config.SIGNUP_BONUS_TOKENS
        
    # Create token balance with signup bonus
    token_balance = TokenBalance(
        user_id=user.id,
        balance=bonus_amount
    )
    db_session.add(token_balance)
    
    # Create streak
    streak = Streak(
        user_id=user.id,
        current_streak=1,
        longest_streak=1,
        last_active_date=datetime.now(UTC)
    )
    db_session.add(streak)
    db_session.commit()
    
    return token_balance, streak


def create_test_user():
    """Create a test user for TEST_MODE."""
    logger.info("Creating test user for TEST_MODE")
    
    password_hash = bcrypt.hashpw('test123'.encode('utf-8'), bcrypt.gensalt())
    user = User(
        email='test@satoricheck.com',
        password_hash=password_hash.decode('utf-8'),
        created_at=datetime.now(UTC)
    )
    db_session.add(user)
    db_session.commit()
    
    # Provision new user (balance + streak)
    provision_new_user(user)
    
    logger.info(f"Test user created: {user.email}")
    return user


@auth_bp.route('/signup', methods=['POST'])
def signup():
    """Register a new user."""
    # Rate limit: 5 signups per hour per IP (prevent bonus farming)
    try:
        data = request.get_json()
        
        if not data:
            raise APIError('No data provided')
        
        email = data.get('email')
        password = data.get('password')
        
        if not email or not password:
            raise APIError('Email and password are required')
        
        # Validate email format (robust)
        import re
        if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
            raise APIError('Invalid email format')
        
        # Check if user already exists
        existing_user = db_session.query(User).filter_by(email=email).first()
        if existing_user:
            raise APIError('User already exists')
        
        # Hash password
        password_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
        
        # Check if they had an account before (prevent bonus abuse)
        email_hash = hashlib.sha256(email.lower().strip().encode()).hexdigest()
        was_deleted = db_session.query(DeletedUser).filter_by(email_hash=email_hash).first()
        
        bonus = 0 if was_deleted else Config.SIGNUP_BONUS_TOKENS
        
        # Generate secure API token
        api_token = secrets.token_hex(32)
        
        # Create user
        user = User(
            email=email,
            password_hash=password_hash.decode('utf-8'),
            api_token=api_token,
            created_at=datetime.now(UTC)
        )
        db_session.add(user)
        db_session.commit()
        
        # Provision new user (balance + streak)
        provision_new_user(user, bonus_amount=bonus)
        
        # Log user in
        session['user_id'] = user.id
        
        logger.info(f"New user registered: {email}")
        
        msg = f'Account created! You received {bonus} CP bonus'
        if was_deleted:
            msg = 'Welcome back! Your account has been recreated.'
            
        return jsonify({
            'success': True,
            'message': msg,
            'user': {
                'email': user.email,
                'balance': bonus,
                'streak': 1
            }
        })
        
    except APIError:
        raise
    except Exception as e:
        logger.error(f"Signup error: {e}", exc_info=True)
        raise APIError('Failed to create account')


@auth_bp.route('/login', methods=['POST'])
def login():
    """Log in a user."""
    # Rate limit: 10 logins per minute per IP (prevent brute force)
    try:
        data = request.get_json()
        
        if not data:
            raise APIError('No data provided')
        
        email = data.get('email')
        password = data.get('password')
        
        if not email or not password:
            logger.warning("Login: Email or password missing")
            raise APIError('Email and password are required')
        
        # Find user
        user = db_session.query(User).filter_by(email=email).first()
        if not user:
            logger.warning(f"Login: User not found: {email}")
            raise APIError('Invalid email or password', status_code=401)
        
        # Verify password
        if not bcrypt.checkpw(password.encode('utf-8'), user.password_hash.encode('utf-8')):
            logger.warning(f"Login: Invalid password for: {email}")
            raise APIError('Invalid email or password', status_code=401)
        
        # Update last login and handle streak
        user.last_login = datetime.now(UTC)
        streak_result = handle_login_streak(user.id, db_session)
        current_streak = streak_result['streak_count']
        
        db_session.commit()
        
        # Create JWT token
        jwt_token = create_token(user.id, user.email)
        
        # Also set legacy session for backwards compatibility
        session['user_id'] = user.id
        
        # Get token balance
        token_balance = db_session.query(TokenBalance).filter_by(user_id=user.id).first()
        balance = token_balance.balance if token_balance else 0
        
        logger.info(f"User logged in: {email}")
        
        # Create response with JWT cookie
        response = make_response(jsonify({
            'success': True,
            'message': 'Login successful',
            'user': {
                'email': user.email,
                'balance': balance,
                'streak': current_streak
            }
        }))
        
        # Set JWT cookie
        response.set_cookie(
            JWT_COOKIE_NAME,
            jwt_token,
            httponly=JWT_COOKIE_HTTPONLY,
            secure=JWT_COOKIE_SECURE,
            samesite=JWT_COOKIE_SAMESITE,
            max_age=7 * 24 * 60 * 60  # 7 days
        )
        
        return response
        
    except APIError:
        raise
    except Exception as e:
        logger.error(f"Login error: {e}", exc_info=True)
        raise APIError('Login failed')


@auth_bp.route('/logout', methods=['POST'])
@login_required
def logout():
    """Log out the current user."""
    session.clear()
    
    # Create response and clear JWT cookie
    response = make_response(jsonify({
        'success': True,
        'message': 'Logged out successfully'
    }))
    
    response.delete_cookie(JWT_COOKIE_NAME)
    
    return response


@auth_bp.route('/me', methods=['GET'])
@login_required
def get_current_user():
    """Get current user information."""
    user = request.current_user
    
    # Get token balance
    token_balance = db_session.query(TokenBalance).filter_by(user_id=user.id).first()
    balance = token_balance.balance if token_balance else 0
    
    # Get streak
    streak = db_session.query(Streak).filter_by(user_id=user.id).first()
    current_streak = streak.current_streak if streak else 0
    
    return jsonify({
        'success': True,
        'user': {
            'email': user.email,
            'balance': balance,
            'streak': current_streak,
            'is_test_mode': Config.TEST_MODE
        }
    })



@auth_bp.route('/delete-account', methods=['POST'])
@login_required
def delete_account():
    """Permanently delete user account and all related data."""
    try:
        user = request.current_user
        user_email = user.email
        
        logger.warning(f"DELETING ACCOUNT: {user_email} (ID: {user.id})")
        
        # Imports to ensure we have all models needed for cleanup
        from backend.models import TokenBalance, Streak, Transaction, FactCheck, LiveProSession, DeletedUser
        
        # 1. Create a tombstone record to prevent bonus abuse on re-signup
        # We store SHA256 of email to be GDPR/Privacy compliant (no PII kept)
        email_hash = hashlib.sha256(user_email.lower().strip().encode()).hexdigest()
        
        # Only add if not already there (shouldn't be, but safe)
        existing_tombstone = db_session.query(DeletedUser).filter_by(email_hash=email_hash).first()
        if not existing_tombstone:
            tombstone = DeletedUser(email_hash=email_hash)
            db_session.add(tombstone)

        # 2. Delete dependent records explicitly (though cascade should handle it)
        db_session.query(TokenBalance).filter_by(user_id=user.id).delete()
        db_session.query(Streak).filter_by(user_id=user.id).delete()
        db_session.query(Transaction).filter_by(user_id=user.id).delete()
        db_session.query(FactCheck).filter_by(user_id=user.id).delete()
        db_session.query(LiveProSession).filter_by(user_id=user.id).delete()
        
        # 2. Delete the user
        db_session.delete(user)
        db_session.commit()
        
        logger.info(f"Account deleted successfully: {user_email}")
        
        # 3. Clear session and cookies
        session.clear()
        
        response = make_response(jsonify({
            'success': True,
            'message': 'Account and data permanently deleted'
        }))
        
        response.delete_cookie(JWT_COOKIE_NAME)
        return response
        
    except Exception as e:
        logger.error(f"Account deletion error: {e}", exc_info=True)
        db_session.rollback()
        raise APIError('Account deletion failed')


@auth_bp.route('/google')
def google_login():
    """Initiate Google OAuth login."""
    redirect_uri = url_for('auth.google_callback', _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@auth_bp.route('/google/callback')
def google_callback():
    """Handle Google OAuth callback."""
    try:
        # Fast-fail: reject requests without OAuth code/state (bots/crawlers)
        if 'code' not in request.args or 'state' not in request.args:
            logger.warning("OAuth callback hit without code/state - likely a bot")
            return redirect('/?error=invalid_oauth_request')
        
        token = oauth.google.authorize_access_token()
        resp = oauth.google.get('https://www.googleapis.com/oauth2/v3/userinfo')
        user_info = resp.json()
        
        email = user_info.get('email')
        if not email:
            raise APIError("Google provided no email", status_code=400)
            
        # Check if user exists
        user = db_session.query(User).filter_by(email=email).first()
        
        is_new_user = False
        
        if not user:
            # Create new user
            is_new_user = True
            # Generate random password (they can reset it later, or just use Google)
            random_pw = secrets.token_urlsafe(16)
            password_hash = bcrypt.hashpw(random_pw.encode('utf-8'), bcrypt.gensalt())
            api_token = secrets.token_hex(32)  # Secure API token
            
            # Check if they had an account before (prevent bonus abuse)
            email_hash = hashlib.sha256(email.lower().strip().encode()).hexdigest()
            was_deleted = db_session.query(DeletedUser).filter_by(email_hash=email_hash).first()
            
            bonus = 0 if was_deleted else Config.SIGNUP_BONUS_TOKENS

            user = User(
                email=email,
                password_hash=password_hash.decode('utf-8'),
                api_token=api_token,
                created_at=datetime.now(UTC)
            )
            db_session.add(user)
            db_session.commit()
            
            # Provision new user (balance + streak)
            provision_new_user(user, bonus_amount=bonus)
            
            logger.info(f"New Google user created: {email} (Bonus: {bonus} - was_deleted: {was_deleted})")
            
        # Consolidate last login and streak handling for both new and existing users
        user.last_login = datetime.now(UTC)
        handle_login_streak(user.id, db_session)
        db_session.commit()
        logger.info(f"Google user logged in: {email}")
            
        # Create JWT token
        jwt_token = create_token(user.id, user.email)
        
        # Also set legacy session for backwards compatibility
        session['user_id'] = user.id
        
        # Redirect to frontend with JWT cookie
        redirect_url = '/?new_user=true' if is_new_user else '/'
        response = make_response(redirect(redirect_url))
        response.set_cookie(
            JWT_COOKIE_NAME,
            jwt_token,
            httponly=JWT_COOKIE_HTTPONLY,
            secure=JWT_COOKIE_SECURE,
            samesite=JWT_COOKIE_SAMESITE,
            max_age=7 * 24 * 60 * 60  # 7 days
        )
        
        return response
        
    except Exception as e:
        logger.error(f"Google login error: {e}", exc_info=True)
        # Don't leak internal error details - redirect to home with error
        response = make_response(redirect('/?error=google_auth_failed'))
        return response
