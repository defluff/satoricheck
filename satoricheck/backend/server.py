"""
SatoriCheck - Live Fact Checker Backend Server
Clean, modular Flask application with robust error handling.
"""
import os
from flask import Flask, jsonify, send_from_directory, request, redirect
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix
import logging
import sys
from datetime import datetime
from sqlalchemy import text # For DB health check

from backend.config import Config
from backend.database import init_db, cleanup_db, db_session
from backend.error_handlers import register_error_handlers

# Import blueprints
from backend.routes.auth import auth_bp
from backend.routes.tokens import tokens_bp
from backend.routes.billing import billing_bp
from backend.routes.factcheck import factcheck_bp
from backend.routes.export import export_bp

# Configure logging
logging.basicConfig(
    level=logging.INFO if Config.ENV == 'development' else logging.WARNING,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger(__name__)

# Create Flask app
app = Flask(__name__, static_folder='../frontend', static_url_path='')
app.config['SECRET_KEY'] = Config.SECRET_KEY
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = Config.ENV == 'production'

# Trust proxy headers (Cloud Run, nginx, etc.)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# Enable CORS (Production + Extension + Local)
CORS(app, supports_credentials=True, origins=[
    'https://satoricheck-829698588154.europe-west6.run.app',  # Cloud Run production
    'https://satoricheck.com',  # Future custom domain (if configured)
    'chrome-extension://*',
    'http://localhost:*', 
    'http://127.0.0.1:*'
])

@app.before_request
def redirect_https():
    """Redirect HTTP to HTTPS in production (Cloud Run)"""
    if not Config.TEST_MODE and request.headers.get('X-Forwarded-Proto') == 'http':
        url = request.url.replace('http://', 'https://', 1)
        return redirect(url, code=301)

@app.route('/health')
def health():
    """Robust health check for Cloud Run"""
    try:
        # Check DB connection
        db_session.execute(text('SELECT 1'))
        return jsonify({'status': 'healthy', 'db': 'connected'}), 200
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return jsonify({'status': 'unhealthy'}), 500

from backend.extensions import oauth

# Initialize OAuth
oauth.init_app(app)

# Register Google
if Config.GOOGLE_CLIENT_ID and Config.GOOGLE_CLIENT_SECRET:
    oauth.register(
        name='google',
        client_id=Config.GOOGLE_CLIENT_ID,
        client_secret=Config.GOOGLE_CLIENT_SECRET,
        server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
        client_kwargs={'scope': 'openid email profile'}
    )
    logger.info("✓ Google OAuth registered")
else:
    logger.warning("! Google OAuth credentials missing")

# Validate configuration
try:
    Config.validate()
    logger.info("✓ Configuration validated")
except ValueError as e:
    logger.error(f"Configuration error: {e}")
    if not Config.TEST_MODE:
        sys.exit(1)

# Register error handlers
register_error_handlers(app)

# Register blueprints
app.register_blueprint(auth_bp)
app.register_blueprint(tokens_bp)
app.register_blueprint(billing_bp)
app.register_blueprint(factcheck_bp)
app.register_blueprint(export_bp)

# Import and register Live Pro blueprint
from backend.routes.live_pro import live_pro_bp
app.register_blueprint(live_pro_bp)

logger.info("✓ Blueprints registered")

# Initialize global service instances
from backend.services import init_services
init_services()
logger.info("✓ External API services initialized")

# Initialize Deepgram service
from backend.services.deepgram_service import init_deepgram_service
init_deepgram_service()

# Initialize database (create tables if they don't exist)
logger.info("Initializing database...")
init_db()
logger.info("✓ Database initialized")

# Initialize background scheduler for cleanup tasks
from apscheduler.schedulers.background import BackgroundScheduler
from backend.routes.live_pro import cleanup_abandoned_sessions

scheduler = BackgroundScheduler()
scheduler.add_job(
    func=cleanup_abandoned_sessions,
    trigger='interval',
    seconds=60,
    id='cleanup_abandoned_sessions',
    name='Cleanup abandoned Live Pro sessions',
    replace_existing=True
)

# Only start scheduler in main process (not reloader)
import os
if os.environ.get('WERKZEUG_RUN_MAIN') != 'true' or not app.debug:
    scheduler.start()
    logger.info("✓ Background scheduler started (cleanup every 60s)")


@app.route('/api/health')
def health_check():
    """Health check endpoint."""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.utcnow().isoformat(),
        'test_mode': Config.TEST_MODE
    })


@app.route('/')
def serve_index():
    """Serve the main HTML page."""
    return send_from_directory(app.static_folder, 'index.html')


@app.route('/<path:path>')
def serve_static(path):
    """Serve static files."""
    return send_from_directory(app.static_folder, path)


@app.teardown_appcontext
def shutdown_session(exception=None):
    """Clean up database session."""
    cleanup_db()


def main():
    """Main entry point."""
    logger.info("=" * 60)
    logger.info("SatoriCheck - Live Fact Checker")
    logger.info("=" * 60)
    
    if Config.TEST_MODE:
        logger.warning("⚠️  TEST MODE ENABLED - Authentication bypassed")
        logger.warning("⚠️  Disable TEST_MODE in production!")
    
    # Initialize database
    logger.info("Initializing database...")
    init_db()
    
    # Start server
    logger.info(f"Starting server on {Config.HOST}:{Config.PORT}")
    logger.info("=" * 60)
    
    app.run(
        host=Config.HOST,
        port=Config.PORT,
        debug=Config.ENV == 'development'
    )


if __name__ == '__main__':
    main()
