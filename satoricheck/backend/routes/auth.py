"""
Authentication routes.
Handles user registration, login, logout, and password changes.
Uses JWT tokens for stateless authentication (Cloud Run compatible).
"""
from flask import Blueprint, request, session, jsonify, make_response
from functools import wraps
import bcrypt
import logging
from datetime import datetime

from backend.database import db_session
from backend.models import User, TokenBalance, Streak
from backend.config import Config
from backend.error_handlers import APIError
from backend.services.streak import update_streak
from backend.extensions import oauth
from backend.jwt_utils import create_token, verify_token, refresh_token_if_needed
from flask import url_for, redirect
import secrets

logger = logging.getLogger(__name__)

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


def provision_new_user(user):
    """
    Create TokenBalance and Streak for a new user.
    Called during signup, Google OAuth registration, and test user creation.
    """
    # Create token balance with signup bonus
    token_balance = TokenBalance(
        user_id=user.id,
        balance=Config.SIGNUP_BONUS_TOKENS
    )
    db_session.add(token_balance)
    
    # Create streak
    streak = Streak(
        user_id=user.id,
        current_streak=1,
        longest_streak=1,
        last_active_date=datetime.utcnow()
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
        created_at=datetime.utcnow()
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
    try:
        data = request.get_json()
        
        if not data:
            raise APIError('No data provided')
        
        email = data.get('email')
        password = data.get('password')
        
        if not email or not password:
            raise APIError('Email and password are required')
        
        # Validate email format (basic)
        if '@' not in email or '.' not in email:
            raise APIError('Invalid email format')
        
        # Check if user already exists
        existing_user = db_session.query(User).filter_by(email=email).first()
        if existing_user:
            raise APIError('User already exists')
        
        # Hash password
        password_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
        
        # Generate secure API token
        api_token = secrets.token_hex(32)  # 64 character hex string
        
        # Create user
        user = User(
            email=email,
            password_hash=password_hash.decode('utf-8'),
            api_token=api_token,
            created_at=datetime.utcnow()
        )
        db_session.add(user)
        db_session.commit()
        
        # Provision new user (balance + streak)
        provision_new_user(user)
        
        # Log user in
        session['user_id'] = user.id
        
        logger.info(f"New user registered: {email}")
        
        return jsonify({
            'success': True,
            'message': f'Account created! You received {Config.SIGNUP_BONUS_TOKENS} CP bonus',
            'user': {
                'email': user.email,
                'balance': Config.SIGNUP_BONUS_TOKENS,
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
    try:
        data = request.get_json()
        
        if not data:
            raise APIError('No data provided')
        
        email = data.get('email')
        password = data.get('password')
        
        if not email or not password:
            raise APIError('Email and password are required')
        
        # Find user
        user = db_session.query(User).filter_by(email=email).first()
        if not user:
            raise APIError('Invalid email or password', status_code=401)
        
        # Verify password
        if not bcrypt.checkpw(password.encode('utf-8'), user.password_hash.encode('utf-8')):
            raise APIError('Invalid email or password', status_code=401)
        
        # Update last login
        user.last_login = datetime.utcnow()
        
        # Get or create streak
        streak = db_session.query(Streak).filter_by(user_id=user.id).first()
        
        if streak:
            current_streak = update_streak(streak, streak.last_active_date)
        else:
            # Create streak if doesn't exist
            streak = Streak(
                user_id=user.id,
                current_streak=1,
                longest_streak=1,
                last_active_date=datetime.utcnow()
            )
            db_session.add(streak)
            current_streak = 1
        
        # Check for rewards
        reward_amount = 0
        reward_message = None
        
        # Rewards: 6->100, 14->200, 21->400, 30->1000 (Cycle based)
        cycle_day = (current_streak - 1) % 30 + 1
        
        if cycle_day == 6:
            reward_amount = 100
            reward_message = "Mojo Rising! +100 CP Reward"
        elif cycle_day == 14:
            reward_amount = 200
            reward_message = "Two Weeks Strong! +200 CP Reward"
        elif cycle_day == 21:
            reward_amount = 400
            reward_message = "Habit Master! +400 CP Reward"
        elif cycle_day == 30:
            reward_amount = 1000
            reward_message = "LEGENDARY! +1000 CP Reward"
            
        # Only grant if this is a NEW day (streak increased)
        # Note: update_streak returns new count. We compare with stored DB value before commit?
        # Actually update_streak updates the object. We need to check if we just crossed a boundary TODAY.
        # Simplified: We assume update_streak handles the "once per day" check logic safely.
        # We need to ensure we don't grant rewards multiple times on same day. 
        # Since update_streak only increments once per day, checking the *new* value is safe 
        # IF we only do it when the streak CHANGED. 
        # But here we don't know if it changed easily without refactoring.
        # Let's rely on the fact that update_streak increments only if yesterday was last active.
        # Wait, if I log in 5 times today, streak is same. reward would trigger 5 times?
        # FIX: We need to know if streak *incremented*.
        
        # Refetch to be safe or verify logic.
        # update_streak logic: returns current_streak. It MODIFIES the object.
        # We can check if streak.last_active_date was yesterday before update?
        # Too complex to modify update_streak now.
        # Alternative: Store `last_reward_streak` in DB? No schema change allowed easily.
        # Hack: Check if transaction exists for this streak count? (Expensive)
        # Better: Since update_streak updates `last_active_date` to NOW, 
        # we can check if it WAS yesterday in the `streak.py`.
        
        # Let's Modify `streak.py` to return (count, incremented_bool) instead?
        # Or just move reward logic there?
        # For now, let's assume the user logs in once. 
        # To be ROBUST: check if a "bonus" transaction exists for today?
        from backend.models import Transaction
        
        if reward_amount > 0:
            # Check if we already gave a bonus today
            start_of_day = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            existing_bonus = db_session.query(Transaction).filter(
                Transaction.user_id == user.id,
                Transaction.type == 'bonus',
                Transaction.timestamp >= start_of_day
            ).first()
            
            if not existing_bonus:
                # Grant Reward
                token_balance = db_session.query(TokenBalance).filter_by(user_id=user.id).first()
                if token_balance:
                    token_balance.balance += reward_amount
                    
                    # Record Transaction
                    trans = Transaction(
                        user_id=user.id,
                        type='bonus',
                        amount=reward_amount,
                        description=reward_message
                    )
                    db_session.add(trans)

        
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



@auth_bp.route('/google')
def google_login():
    """Initiate Google OAuth login."""
    redirect_uri = url_for('auth.google_callback', _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@auth_bp.route('/google/callback')
def google_callback():
    """Handle Google OAuth callback."""
    try:
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
            
            user = User(
                email=email,
                password_hash=password_hash.decode('utf-8'),
                api_token=api_token,
                created_at=datetime.utcnow()
            )
            db_session.add(user)
            db_session.commit()
            
            # Provision new user (balance + streak)
            provision_new_user(user)
            
            logger.info(f"New Google user created: {email}")
            
        else:
            # Update last login
            user.last_login = datetime.utcnow()
            
            # Update streak
            streak = db_session.query(Streak).filter_by(user_id=user.id).first()
            if streak:
                update_streak(streak, streak.last_active_date)
            else:
                streak = Streak(
                    user_id=user.id,
                    current_streak=1,
                    longest_streak=1,
                    last_active_date=datetime.utcnow()
                )
                db_session.add(streak)
            
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
