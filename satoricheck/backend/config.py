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
    DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///satoricheck.db')
    
    # Server
    PORT = int(os.getenv('PORT', 8000))
    HOST = os.getenv('HOST', '127.0.0.1')
    
    # Token pricing (batteries)
    TOKEN_PACKAGES = {
        'battery_small': {
            'name': 'Small Battery',
            'tokens': 432,  # 96 CP/CHF
            'price': 450,  # in cents (4.50 CHF)
            'currency': 'chf'
        },
        'battery_large': {
            'name': 'Medium Battery',
            'tokens': 2432,  # 101 CP/CHF (better than small!)
            'price': 2400,  # 24 CHF
            'currency': 'chf'
        },
        'generator': {
            'name': 'Large Battery',
            'tokens': 11111,
            'price': 9900,  # 99 CHF
            'currency': 'chf'
        },
        'wizard': {
            'name': 'Wizard Energy Plantation',
            'tokens': 5000,  # Initial + monthly refills
            'price': 89000,  # 890 CHF ONE-TIME (not monthly!)
            'currency': 'chf',
            'is_subscription': False,  # One-time payment
            'duration': 60  # 5 years of monthly refills
        }
    }
    
    # Token costs
    TOKENS_PER_250_WORDS = 1
    SIGNUP_BONUS_TOKENS = 5  # Freemium: 5 free CP to test the app
    
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
