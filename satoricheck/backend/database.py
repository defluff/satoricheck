"""
Database initialization and session management.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session
from backend.models import Base
from backend.config import Config
import logging

logger = logging.getLogger(__name__)



# Create database engine with Cloud Run optimized settings
if 'sqlite' in Config.DATABASE_URL:
    # SQLite: file-based, no connection pooling needed
    engine = create_engine(
        Config.DATABASE_URL,
        echo=False,
        connect_args={'check_same_thread': False}
    )
else:
    # PostgreSQL/MySQL: Optimized for Cloud Run
    engine = create_engine(
        Config.DATABASE_URL,
        echo=Config.ENV == 'development',
        pool_size=5,              # Persistent connections
        max_overflow=10,          # Additional connections during spikes
        pool_pre_ping=True,       # Test connections before use
        pool_recycle=3600,        # Recycle connections every hour
        pool_timeout=30           # Wait for connection availability
    )

# Create session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Create scoped session for thread safety
db_session = scoped_session(SessionLocal)


def init_db():
    """Initialize database tables."""
    Base.metadata.create_all(bind=engine)
    logger.info("✓ Database initialized")


def get_db():
    """Get database session for dependency injection."""
    db = db_session()
    try:
        yield db
    finally:
        db.close()


def cleanup_db():
    """Clean up database connections."""
    db_session.remove()
