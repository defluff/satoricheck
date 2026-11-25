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
    
    # Google Gemini — Module-Boundary Hybrid Architecture:
    # - GEMINI_MODEL (Flash 3.8): text claim extraction, stream triage, verification,
    #   and Flash-pinned context caches (depth set per call via thinking_level: "low" or "high").
    # - GEMINI_MEDIA_MODEL (Argon 4): multimodal media forensics and pitch deck visual extraction.
    # Both use explicit stable IDs (never "-latest" aliases) so behavior only changes on deploy.
    GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
    GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-3.8-flash')
    GEMINI_MEDIA_MODEL = os.getenv('GEMINI_MEDIA_MODEL', 'gemini-4-argon')
    # Hard ceiling (in milliseconds, per google.genai.types.HttpOptions) for any single
    # Gemini SDK HTTP call so a stalled upstream socket cannot wedge a Gunicorn thread.
    GEMINI_HTTP_TIMEOUT_MS = int(os.getenv('GEMINI_HTTP_TIMEOUT_MS', '120000'))

    # Google OAuth
    GOOGLE_CLIENT_ID = os.getenv('GOOGLE_CLIENT_ID')
    GOOGLE_CLIENT_SECRET = os.getenv('GOOGLE_CLIENT_SECRET')
    GOOGLE_PICKER_API_KEY = os.getenv('GOOGLE_PICKER_API_KEY')

    # Cloud Storage OAuth & Token Encryption (optional unless cloud sync is enabled)
    GOOGLE_TOKEN_ENCRYPTION_KEY = os.getenv('GOOGLE_TOKEN_ENCRYPTION_KEY')
    # Strictly non-restricted Public Stage 1 scopes (drive.file + spreadsheets) — zero CASA Tier 2 audit.
    GOOGLE_DRIVE_SCOPES = os.getenv(
        'GOOGLE_DRIVE_SCOPES',
        'https://www.googleapis.com/auth/drive.file '
        'https://www.googleapis.com/auth/spreadsheets',
    )
    DROPBOX_CLIENT_ID = os.getenv('DROPBOX_CLIENT_ID')
    DROPBOX_CLIENT_SECRET = os.getenv('DROPBOX_CLIENT_SECRET')
    ONEDRIVE_CLIENT_ID = os.getenv('ONEDRIVE_CLIENT_ID')
    ONEDRIVE_CLIENT_SECRET = os.getenv('ONEDRIVE_CLIENT_SECRET')
    CLOUD_OAUTH_REDIRECT_BASE = os.getenv('CLOUD_OAUTH_REDIRECT_BASE')
    
    # Stripe
    STRIPE_SECRET_KEY = os.getenv('STRIPE_SECRET_KEY')
    STRIPE_PUBLISHABLE_KEY = os.getenv('STRIPE_PUBLISHABLE_KEY')
    STRIPE_WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET')
    
    # Application
    TEST_MODE = os.getenv('TEST_MODE', 'false').lower() == 'true'
    MAINTENANCE_MODE = os.getenv('MAINTENANCE_MODE', 'false').lower() == 'true'
    # Omit Folder Sync by default in production ('Coming Soon'); active in local dev/testing unless overridden.
    OMIT_FOLDER_SYNC = (
        os.getenv('OMIT_FOLDER_SYNC').lower() == 'true'
        if os.getenv('OMIT_FOLDER_SYNC') is not None
        else (ENV == 'production')
    )
    FOLDER_SYNC_ENABLED = os.getenv('FOLDER_SYNC_ENABLED', 'true').lower() == 'true'
    DATABASE_URL = normalize_database_url(os.getenv('DATABASE_URL', 'sqlite:///authenix.db'))
    # Per-process in-memory counters by default (single-instance Cloud Run). When scaling
    # Cloud Run max-instances > 1, set RATELIMIT_STORAGE_URI to a shared Redis/Memorystore URI.
    RATELIMIT_STORAGE_URI = os.getenv('RATELIMIT_STORAGE_URI', 'memory://')

    # Number of trusted proxies that append to X-Forwarded-For. Cloud Run's front end
    # appends exactly one entry (the real client IP), so 1 is correct for direct
    # *.run.app / domain-mapping ingress. Set to 2 when a Global External Application
    # Load Balancer sits in front (it appends "<client>,<lb-ip>"). Too low → every user
    # shares the proxy's rate-limit bucket; too high → clients can spoof their IP.
    TRUSTED_PROXY_HOPS = int(os.getenv('TRUSTED_PROXY_HOPS', '1'))
    
    # Scheduler auth (Cloud Scheduler cron jobs). The random fallback keeps dev/test
    # endpoints un-guessable, but is per-process, so production must set it explicitly
    # (enforced in validate()).
    SCHEDULER_SECRET = os.getenv('SCHEDULER_SECRET', secrets.token_hex(32))
    
    # Server
    PORT = int(os.getenv('PORT', 8000))
    HOST = os.getenv('HOST', '127.0.0.1')
    
    # xAI Grok API (for Social Context in Smart Mode)
    # Pinned to an explicit watermarked model ID (not a "-latest" alias) so behavior
    # only changes on deploy and responses include the EU AI Act x-watermark header.
    GROK_API_KEY = os.getenv('GROK_API_KEY')
    GROK_MODEL = os.getenv('GROK_MODEL', 'grok-4.7')
    GROK_ENABLED = os.getenv('GROK_ENABLED', 'true').lower() == 'true'
    GROK_TIMEOUT = int(os.getenv('GROK_TIMEOUT', '10'))  # seconds (tightened to guard worker threads in agentic loops)
    
    # Token pricing (batteries)
    TOKEN_PACKAGES = {
        'battery_small': {
            'name': 'Small Battery',
            'tokens': 40,
            'price': 450,  # 4.50 CHF
            'currency': 'chf',
            'stripe_price_id': 'price_1SiEx8DnKgm8pOxQ6XL616H1'
        },
        'battery_medium': {
            'name': 'Medium Battery',
            'tokens': 450,
            'price': 4900,  # 49 CHF
            'currency': 'chf',
            'stripe_price_id': 'price_1UdbvwDnKgm8pOxQLJKUrLtM'
        },
        'battery_large': {
            'name': 'Large Battery',
            'tokens': 1000,
            'price': 9900,  # 99 CHF
            'currency': 'chf',
            'stripe_price_id': 'price_1SiEy2DnKgm8pOxQ0heRz08W'
        },
        'wizard': {
            'name': 'Wizard Energy Plantation',
            'tokens': 500,  # per month
            'price': 190000,  # 1900 CHF ONE-TIME
            'currency': 'chf',
            'is_subscription': False,
            'duration': 60,  # 5 years of monthly refills
            'stripe_price_id': 'price_1UdbzCDnKgm8pOxQSGyIVyB0'
        }
    }
    
    WIZARD_REFILL_AMOUNT = 500
    
    # Token costs
    MEDIA_CP_IMAGE = 4
    MEDIA_CP_AUDIO = 5
    # Video CP tiers follow payload byte size (<= 5 MB @ 4 FPS, 5-14 MB @ 2 FPS, > 14 MB or YouTube @ 2 FPS)
    MEDIA_CP_VIDEO_SHORT = 15   # <= 5 MB (4.0 FPS inline)
    MEDIA_CP_VIDEO_MEDIUM = 25  # 5 MB - 14 MB (2.0 FPS inline)
    MEDIA_CP_VIDEO_LONG = 40    # > 14 MB (Files API) or YouTube URI (2.0 FPS)
    MAX_VIDEO_DURATION_SECONDS = 30  # Gemini ingestion window; longer clips analyze first 30s
    
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
    def get_google_drive_scopes(cls) -> str:
        """Return space-delimited non-restricted Google Drive/Sheets scopes (never drive.readonly)."""
        tokens = [
            tok for tok in (cls.GOOGLE_DRIVE_SCOPES or '').split()
            if tok and tok != 'https://www.googleapis.com/auth/drive.readonly'
        ]
        if not tokens:
            tokens = [
                'https://www.googleapis.com/auth/drive.file',
                'https://www.googleapis.com/auth/spreadsheets',
            ]
        return ' '.join(tokens)

    @classmethod
    def is_folder_sync_enabled(cls) -> bool:
        """Return False when OMIT_FOLDER_SYNC=true or FOLDER_SYNC_ENABLED=false."""
        if cls.OMIT_FOLDER_SYNC:
            return False
        return bool(cls.FOLDER_SYNC_ENABLED)

    @classmethod
    def get_google_picker_app_id(cls) -> str | None:
        """Return GCP project number extracted from GOOGLE_CLIENT_ID for Google Picker setAppId()."""
        client_id = (cls.GOOGLE_CLIENT_ID or '').strip()
        prefix = client_id.split('-', 1)[0]
        return prefix if prefix.isdigit() else None

    @classmethod
    def get_streak_milestone(cls, streak_count):
        """Get the milestone name for a streak count."""
        milestone = 'Novice'
        for count, name in cls.STREAK_MILESTONES:
            if streak_count >= count:
                milestone = name
        return milestone
