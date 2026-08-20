"""
Global error handlers and custom exceptions.
"""
from flask import jsonify
import logging

logger = logging.getLogger(__name__)


class APIError(Exception):
    """Custom API error class."""
    
    def __init__(self, message, status_code=400, payload=None):
        super().__init__()
        self.message = message
        self.status_code = status_code
        self.payload = payload
    
    def to_dict(self):
        """Convert error to dictionary."""
        rv = dict(self.payload or ())
        rv['error'] = self.message
        rv['success'] = False
        return rv


def register_error_handlers(app):
    """Register error handlers with Flask app."""
    
    @app.errorhandler(APIError)
    def handle_api_error(error):
        """Handle custom API errors."""
        logger.error(f"API Error: {error.message}")
        response = jsonify(error.to_dict())
        response.status_code = error.status_code
        return response
    
    @app.errorhandler(400)
    def bad_request(error):
        """Handle bad request errors."""
        logger.warning(f"Bad Request: {error}")
        return jsonify({
            'success': False,
            'error': 'Bad request. Please check your input.'
        }), 400
    
    @app.errorhandler(401)
    def unauthorized(error):
        """Handle unauthorized errors."""
        return jsonify({
            'success': False,
            'error': 'Authentication required. Please log in.'
        }), 401
    
    @app.errorhandler(403)
    def forbidden(error):
        """Handle forbidden errors."""
        return jsonify({
            'success': False,
            'error': 'You do not have permission to access this resource.'
        }), 403
    
    @app.errorhandler(404)
    def not_found(error):
        """Handle not found errors."""
        return jsonify({
            'success': False,
            'error': 'Resource not found.'
        }), 404
    
    @app.errorhandler(429)
    def ratelimit_handler(error):
        """Handle rate limit exceeded errors."""
        logger.warning(f"Rate limit exceeded: {error}")
        return jsonify({
            'success': False,
            'error': 'Too many requests. Please slow down and try again later.'
        }), 429
    
    @app.errorhandler(500)
    def internal_error(error):
        """Handle internal server errors."""
        logger.error(f"Internal Server Error: {error}", exc_info=True)
        return jsonify({
            'success': False,
            'error': 'An internal server error occurred. Please try again later.'
        }), 500
    
    @app.errorhandler(Exception)
    def handle_unexpected_error(error):
        """Handle any unexpected errors."""
        logger.error(f"Unexpected Error: {error}", exc_info=True)
        return jsonify({
            'success': False,
            'error': 'An unexpected error occurred. Please try again.'
        }), 500
