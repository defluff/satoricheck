"""
Configuration management for Authenix.
Loads environment variables and provides app configuration.
"""
import os
import secrets
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


class Config:
    """Application configuration."""
    
    # Paths
    BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    SKILLS_DIR = os.path.join(BASE_DIR, "backend", "agents", "skills")

    # Flask
    SECRET_KEY = os.getenv('FLASK_SECRET_KEY')
    ENV = os.getenv('FLASK_ENV', 'production')
    
    # Google Gemini
    GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
    GEMINI_MODEL_PRO = os.getenv('GEMINI_MODEL_PRO', 'gemini-3.1-pro-preview')
    GEMINI_MODEL_FLASH = os.getenv('GEMINI_MODEL_FLASH', 'gemini-3-flash-preview')

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
    DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///authenix.db')
    
    # Scheduler auth (Cloud Scheduler cron jobs)
    SCHEDULER_SECRET = os.getenv('SCHEDULER_SECRET', secrets.token_hex(32))
    
    # Server
    PORT = int(os.getenv('PORT', 8000))
    HOST = os.getenv('HOST', '127.0.0.1')
    
    # Deepgram (for Live Pro transcription)
    DEEPGRAM_API_KEY = os.getenv('DEEPGRAM_API_KEY')
    
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
    
    # Live Pro: Time-based billing
    LIVE_PRO_CP_PER_MINUTE = 1  # 1 CP = 1 minute of Deepgram transcription
    
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
