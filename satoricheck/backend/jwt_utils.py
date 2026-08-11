"""
JWT utility functions for stateless authentication.
Enables Cloud Run scaling by removing server-side session dependency.
"""
import jwt
import logging
from datetime import datetime, timedelta, UTC
from backend.config import Config

logger = logging.getLogger(__name__)

# Token expiry (7 days)
TOKEN_EXPIRY_DAYS = 7

# Cookie settings
JWT_COOKIE_NAME = 'authenix_token'
JWT_COOKIE_SECURE = Config.ENV != 'development'  # True in production
JWT_COOKIE_HTTPONLY = True
JWT_COOKIE_SAMESITE = 'Lax'


def set_jwt_cookie(response, token: str):
    """Set the JWT authentication cookie with standard security settings on a Flask response."""
    response.set_cookie(
        JWT_COOKIE_NAME,
        token,
        httponly=JWT_COOKIE_HTTPONLY,
        secure=JWT_COOKIE_SECURE,
        samesite=JWT_COOKIE_SAMESITE,
        max_age=TOKEN_EXPIRY_DAYS * 24 * 60 * 60,
    )


def create_token(user_id: int, email: str) -> str:
    """
    Create a signed JWT token for a user.
    
    Args:
        user_id: Database user ID
        email: User's email address
        
    Returns:
        Signed JWT token string
    """
    payload = {
        'user_id': user_id,
        'email': email,
        'iat': datetime.now(UTC),
        'exp': datetime.now(UTC) + timedelta(days=TOKEN_EXPIRY_DAYS)
    }
    
    token = jwt.encode(payload, Config.SECRET_KEY, algorithm='HS256')
    logger.debug(f"Created JWT for user {user_id}")
    return token


def verify_token(token: str) -> dict | None:
    """
    Verify and decode a JWT token.
    
    Args:
        token: JWT token string
        
    Returns:
        Decoded payload dict if valid, None if invalid/expired
    """
    try:
        payload = jwt.decode(token, Config.SECRET_KEY, algorithms=['HS256'])
        return payload
    except jwt.ExpiredSignatureError:
        logger.debug("JWT expired")
        return None
    except jwt.InvalidTokenError as e:
        logger.debug(f"Invalid JWT: {e}")
        return None


def refresh_token_if_needed(token: str) -> str | None:
    """
    Refresh token if it's close to expiry (less than 1 day remaining).
    
    Args:
        token: Current JWT token
        
    Returns:
        New token if refreshed, None if no refresh needed
    """
    payload = verify_token(token)
    if not payload:
        return None
    
    exp = datetime.fromtimestamp(payload['exp'], tz=UTC)
    remaining = exp - datetime.now(UTC)
    
    # Refresh if less than 1 day remaining
    if remaining < timedelta(days=1):
        return create_token(payload['user_id'], payload['email'])
    
    return None
