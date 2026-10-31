"""
Configuration management for Authenix.
Loads environment variables and provides app configuration.
"""
import os
import secrets
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


def normalize_database_url(url: str | None) -> str:
    """Ensure bare postgresql:// or postgres:// URLs explicitly use the psycopg2 driver.

    Why: SQLAlchemy 2.1 changed the default dialect driver for bare 'postgresql://'
    URLs from 'psycopg2' to 'psycopg' (v3). Since we package psycopg2-binary,
    normalizing the scheme prevents ModuleNotFoundError('psycopg') in production.
    """
    if not url:
        return 'sqlite:///authenix.db'
    if url.startswith('postgres://'):
        return url.replace('postgres://', 'postgresql+psycopg2://', 1)
    if url.startswith('postgresql://'):
        return url.replace('postgresql://', 'postgresql+psycopg2://', 1)
    return url


class Config:
    """Application configuration."""
    
    # Paths
    BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    SKILLS_DIR = os.path.join(BASE_DIR, "backend", "agents", "skills")

    # Flask
    SECRET_KEY = os.getenv('FLASK_SECRET_KEY')
    ENV = os.getenv('FLASK_ENV', 'production')
    
    # Google Gemini — one model for all calls; workload depth is set per call via
    # thinking_level ("low" for extraction/triage, "high" for verification/forensics).
    # Pinned to an explicit stable ID (not a "-latest" alias) so verdict behavior only
    # changes on deploy. Upgrade by setting GEMINI_MODEL in the environment.
    GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
    GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-3.8-flash')

    # Google OAuth
    GOOGLE_CLIENT_ID = os.getenv('GOOGLE_CLIENT_ID')
    GOOGLE_CLIENT_SECRET = os.getenv('GOOGLE_CLIENT_SECRET')
    
    # Stripe
    STRIPE_SECRET_KEY = os.getenv('STRIPE_SECRET_KEY')
    STRIPE_PUBLISHABLE_KEY = os.getenv('STRIPE_PUBLISHABLE_KEY')
    STRIPE_WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET')
    
    # Application
    TEST_MODE = os.getenv('TEST_MODE', 'false').lower() == 'true'
    MAINTENANCE_MODE = os.getenv('MAINTENANCE_MODE', 'false').lower() == 'true'
    DATABASE_URL = normalize_database_url(os.getenv('DATABASE_URL', 'sqlite:///authenix.db'))
    RATELIMIT_STORAGE_URI = os.getenv('RATELIMIT_STORAGE_URI', 'memory://')
    
    # Scheduler auth (Cloud Scheduler cron jobs). The random fallback keeps dev/test
    # endpoints un-guessable, but is per-process, so production must set it explicitly
    # (enforced in validate()).
    SCHEDULER_SECRET = os.getenv('SCHEDULER_SECRET', secrets.token_hex(32))
    
    # Server
    PORT = int(os.getenv('PORT', 8000))
    HOST = os.getenv('HOST', '127.0.0.1')
    
    # xAI Grok API (for Social Context in Smart Mode)
    GROK_API_KEY = os.getenv('GROK_API_KEY')
    GROK_ENABLED = os.getenv('GROK_ENABLED', 'true').lower() == 'true'
    GROK_TIMEOUT = int(os.getenv('GROK_TIMEOUT', '30'))  # seconds (API can be slow)
    GROK_MAX_CALLS_PER_DAY = 1000
    
    # Token pricing (batteries) - NEW: Reduced by 5x for Live Pro margins
    TOKEN_PACKAGES = {
        'battery_small': {
            'name': 'Small Battery',
            'tokens': 86,  # Was 432, now ÷5
            'price': 450,  # 4.50 CHF
            'currency': 'chf',
            'stripe_price_id': 'price_1SiEx8DnKgm8pOxQ6XL616H1'
        },
        'battery_medium': {
            'name': 'Medium Battery',
            'tokens': 486,  # Was 2432, now ÷5
            'price': 2400,  # 24 CHF
            'currency': 'chf',
            'stripe_price_id': 'price_1SiExTDnKgm8pOxQIx7ONzbb'
        },
        'battery_large': {
            'name': 'Large Battery',
            'tokens': 2222,  # Was 11111, now ÷5
            'price': 9900,  # 99 CHF
            'currency': 'chf',
            'stripe_price_id': 'price_1SiEy2DnKgm8pOxQ0heRz08W'
        },
        'wizard': {
            'name': 'Wizard Energy Plantation',
            'tokens': 1000,  # Was 5000, now ÷5 (per month)
            'price': 89000,  # 890 CHF ONE-TIME
            'currency': 'chf',
            'is_subscription': False,
            'duration': 60,  # 5 years of monthly refills
            'stripe_price_id': 'price_1SiEyODnKgm8pOxQIlGabEOQ'
        }
    }
    
    WIZARD_REFILL_AMOUNT = 1000  # Was 5000, now ÷5
    
    # Token costs
    WORDS_PER_CP = 1250
    TOKENS_PER_CP_UNIT = 1
    MEDIA_ANALYSIS_COST = 1  # 1 CP per media analysis
    
    # Signup bonus
    SIGNUP_BONUS_TOKENS = 3  # Freemium: 3 free CP to test the app
    
    # Streak milestones
    STREAK_MILESTONES = [
        (1, 'Novice'),
        (3, 'Beginner'),
        (7, 'Intermediate'),
        (14, 'Advanced'),
        (30, 'Expert'),
        (60, 'Master'),
        (100, 'Legend')
    ]
    
    @classmethod
    def validate(cls):
        """Validate required configuration."""
        # Safety: TEST_MODE must never be active in production
        if cls.TEST_MODE and cls.ENV == 'production':
            raise ValueError(
                'TEST_MODE cannot be enabled in production. '
                'Set TEST_MODE=false or change FLASK_ENV.'
            )
        
        required = []
        
        if not cls.SECRET_KEY:
            required.append('FLASK_SECRET_KEY')
        if not cls.GEMINI_API_KEY and not cls.TEST_MODE:
            required.append('GEMINI_API_KEY')
        if not cls.STRIPE_SECRET_KEY and not cls.TEST_MODE:
            required.append('STRIPE_SECRET_KEY')
        if cls.ENV == 'production' and not os.getenv('SCHEDULER_SECRET'):
            required.append('SCHEDULER_SECRET')
            
        if required:
            raise ValueError(f"Missing required environment variables: {', '.join(required)}")
    
    @classmethod
    def get_streak_milestone(cls, streak_count):
        """Get the milestone name for a streak count."""
        milestone = 'Novice'
        for count, name in cls.STREAK_MILESTONES:
            if streak_count >= count:
                milestone = name
        return milestone
