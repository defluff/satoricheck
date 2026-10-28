from authlib.integrations.flask_client import OAuth
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from backend.config import Config

oauth = OAuth()

# Initialize Limiter (configured in server.py via init_app)
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=Config.RATELIMIT_STORAGE_URI,
    default_limits=["200 per day", "50 per hour"]
)
