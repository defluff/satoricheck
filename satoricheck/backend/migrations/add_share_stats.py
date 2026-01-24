"""
Database migration for Social Sharing feature.
Creates share_stats table for anonymous share tracking.

Supports both SQLite (local dev) and PostgreSQL (Cloud SQL).
"""
import os
import sys

# Add parent to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def migrate_sqlite():
    """Apply migration for SQLite (local development)."""
    import sqlite3
    from pathlib import Path
    
    DB_PATH = Path(__file__).parent.parent / 'satoricheck.db'
    if not DB_PATH.exists():
        DB_PATH = Path(__file__).parent.parent.parent / 'satoricheck.db'
    
    if not DB_PATH.exists():
        print("⊙ SQLite database not found, skipping SQLite migration")
        return
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    try:
        print("Creating share_stats table (SQLite)...")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS share_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform VARCHAR(20) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS ix_share_stats_created_at 
            ON share_stats(created_at)
        """)
        
        conn.commit()
        print("✓ Created share_stats table (SQLite)")
        
    except Exception as e:
        conn.rollback()
        print(f"❌ SQLite migration failed: {e}")
        raise
    finally:
        conn.close()


def migrate_postgres():
    """Apply migration for PostgreSQL (Cloud SQL production)."""
    from backend.database import db_session
    from sqlalchemy import text
    
    try:
        print("Creating share_stats table (PostgreSQL)...")
        
        # Check if table exists
        result = db_session.execute(text("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = 'share_stats'
            )
        """)).fetchone()
        
        if result[0]:
            print("⊙ share_stats table already exists")
            return
        
        # Create table
        db_session.execute(text("""
            CREATE TABLE share_stats (
                id SERIAL PRIMARY KEY,
                platform VARCHAR(20) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
        
        # Create index
        db_session.execute(text("""
            CREATE INDEX ix_share_stats_created_at ON share_stats(created_at)
        """))
        
        db_session.commit()
        print("✓ Created share_stats table (PostgreSQL)")
        
    except Exception as e:
        db_session.rollback()
        print(f"❌ PostgreSQL migration failed: {e}")
        raise


def migrate():
    """Apply migrations for the current database."""
    print("\n=== Social Sharing Migration ===\n")
    
    # Try PostgreSQL first (production), fall back to SQLite (dev)
    try:
        migrate_postgres()
    except Exception as e:
        print(f"PostgreSQL not available ({e}), trying SQLite...")
        migrate_sqlite()
    
    print("\n✅ Migration completed successfully!\n")


if __name__ == '__main__':
    migrate()
