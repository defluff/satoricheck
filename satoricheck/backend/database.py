"""
Database initialization and session management.
"""
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, scoped_session
from backend.models import Base
from backend.config import Config
import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SQLAlchemy type → DDL mapping for ALTER TABLE statements.
# Covers all types used in models.py. Extend if new types are added.
# ---------------------------------------------------------------------------
_TYPE_MAP = {
    'Integer': 'INTEGER',
    'String': 'VARCHAR',
    'Text': 'TEXT',
    'Boolean': 'BOOLEAN',
    'DateTime': 'TIMESTAMP',
    'Float': 'FLOAT',
}


def _sa_type_to_ddl(sa_column):
    """Convert a SQLAlchemy Column type to a portable DDL string."""
    type_name = type(sa_column.type).__name__
    if type_name == 'String' and hasattr(sa_column.type, 'length') and sa_column.type.length:
        return f'VARCHAR({sa_column.type.length})'
    return _TYPE_MAP.get(type_name, 'TEXT')


# Create database engine with Cloud Run optimized settings
if 'sqlite' in Config.DATABASE_URL:
    # SQLite: file-based, enable WAL mode for better concurrent access
    engine = create_engine(
        Config.DATABASE_URL,
        echo=False,
        connect_args={
            'check_same_thread': False,
            'timeout': 30  # Wait up to 30 seconds if DB is locked
        }
    )

    # Enable WAL mode for better concurrent read/write handling
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")  # 30 seconds
        cursor.close()
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


def _apply_migrations():
    """Auto-migrate missing columns by comparing models to live DB schema.

    Why: SQLAlchemy's create_all() only creates NEW tables — it never alters
    existing ones.  When a Column is added to a model but the table already
    exists, INSERTs fail with "no such column".  This function bridges that
    gap without requiring Alembic.

    Limitations: Only handles column additions (the most common drift).
    Column renames, type changes, or deletions still require manual migration.
    """
    inspector = inspect(engine)
    db_tables = set(inspector.get_table_names())

    for table in Base.metadata.sorted_tables:
        if table.name not in db_tables:
            continue  # create_all() handles brand-new tables

        existing_columns = {col['name'] for col in inspector.get_columns(table.name)}
        model_columns = {col.name: col for col in table.columns}

        for col_name, col_obj in model_columns.items():
            if col_name in existing_columns:
                continue

            ddl_type = _sa_type_to_ddl(col_obj)
            default_clause = ''
            if col_obj.default is not None and col_obj.default.is_scalar:
                default_val = col_obj.default.arg
                if isinstance(default_val, str):
                    default_clause = f" DEFAULT '{default_val}'"
                elif isinstance(default_val, bool):
                    default_clause = f" DEFAULT {int(default_val)}"
                elif isinstance(default_val, (int, float)):
                    default_clause = f" DEFAULT {default_val}"

            stmt = f'ALTER TABLE {table.name} ADD COLUMN {col_name} {ddl_type}{default_clause}'
            logger.info(f"↗ Auto-migrating: {stmt}")

            with engine.begin() as conn:
                conn.execute(text(stmt))

    logger.info("✓ Schema migration check complete")


def init_db():
    """Initialize database tables and apply any pending column migrations."""
    Base.metadata.create_all(bind=engine)
    _apply_migrations()
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
