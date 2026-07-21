"""
Migration: Drop Live Pro tables and columns.

Removes the `live_pro_sessions` table and the `hide_live_pro_modal` column
from the `users` table. Run this once after deploying the Live Pro removal.

Usage:
    python -m backend.migrations.drop_live_pro
"""
import logging

from sqlalchemy import inspect, text

from backend.database import db_session, init_db

logger = logging.getLogger(__name__)


def run() -> None:
    """Execute the migration."""
    init_db()
    inspector = inspect(db_session.bind)

    # --- 1. Drop live_pro_sessions table ---
    existing_tables = inspector.get_table_names()
    if 'live_pro_sessions' in existing_tables:
        logger.info("Dropping 'live_pro_sessions' table...")
        db_session.execute(text("DROP TABLE IF EXISTS live_pro_sessions"))
        db_session.commit()
        logger.info("✓ Dropped 'live_pro_sessions' table")
    else:
        logger.info("⊙ 'live_pro_sessions' table does not exist — skipping")

    # --- 2. Drop hide_live_pro_modal column from users ---
    user_columns = [c['name'] for c in inspector.get_columns('users')]
    if 'hide_live_pro_modal' in user_columns:
        logger.info("Dropping 'hide_live_pro_modal' column from 'users'...")
        try:
            # SQLite does not support DROP COLUMN before version 3.35.0.
            # For production PostgreSQL this works directly.
            db_session.execute(text("ALTER TABLE users DROP COLUMN hide_live_pro_modal"))
            db_session.commit()
            logger.info("✓ Dropped 'hide_live_pro_modal' column")
        except Exception as e:
            db_session.rollback()
            logger.warning(
                f"Could not drop column (may be SQLite < 3.35): {e}. "
                "Column is harmless if left — it will be ignored by the ORM."
            )
    else:
        logger.info("⊙ 'hide_live_pro_modal' column does not exist — skipping")

    logger.info("Migration complete.")


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    run()
