"""
Configuration management for SatoriCheck.
Loads environment variables and provides app configuration.
"""
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


class Config:
    """Application configuration."""
    
    # Flask
    SECRET_KEY = os.getenv('FLASK_SECRET_KEY')
    ENV = os.getenv('FLASK_ENV', 'production')
    
    # Google Gemini
    GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

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
    DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///satoricheck.db')
    
    # Server
    PORT = int(os.getenv('PORT', 8000))
    HOST = os.getenv('HOST', '127.0.0.1')
    
    # Deepgram (for Live Pro transcription)
    DEEPGRAM_API_KEY = os.getenv('DEEPGRAM_API_KEY')
    
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
    
    # Token costs - NEW: 5x more words per CP for text mode
    WORDS_PER_CP = 1250  # Was 250, now 5x (to maintain margins with Live Pro)
    TOKENS_PER_CP_UNIT = 1
    
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
