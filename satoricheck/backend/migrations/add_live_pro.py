"""
Database migration for Live Pro feature.
Adds hide_live_pro_modal to users and creates live_pro_sessions table.
"""
import sqlite3
import os
from pathlib import Path

# Get database path - check both possible locations
DB_PATH = Path(__file__).parent.parent / 'satoricheck.db'
if not DB_PATH.exists():
    DB_PATH = Path(__file__).parent.parent.parent / 'satoricheck.db'

def migrate():
    """Apply Live Pro database migrations."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    try:
        # Migration 1: Add hide_live_pro_modal to users
        print("Adding hide_live_pro_modal column to users table...")
        try:
            cursor.execute("""
                ALTER TABLE users 
                ADD COLUMN hide_live_pro_modal BOOLEAN DEFAULT 0
            """)
            print("✓ Added hide_live_pro_modal column")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e).lower():
                print("⊙ hide_live_pro_modal column already exists")
            else:
                raise
        
        # Migration 2: Create live_pro_sessions table
        print("Creating live_pro_sessions table...")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS live_pro_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_heartbeat TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                ended_at TIMESTAMP,
                cp_consumed INTEGER DEFAULT 0,
                duration_seconds INTEGER DEFAULT 0,
                status VARCHAR(20) DEFAULT 'active',
                language VARCHAR(10) DEFAULT 'en',
                device_id VARCHAR(255),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        print("✓ Created live_pro_sessions table")
        
        # Create indices for performance
        print("Creating indices...")
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS ix_livepro_user_status 
            ON live_pro_sessions(user_id, status)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS ix_livepro_heartbeat 
            ON live_pro_sessions(last_heartbeat)
        """)
        print("✓ Created indices")
        
        conn.commit()
        print("\n✅ Migration completed successfully!")
        
    except Exception as e:
        conn.rollback()
        print(f"\n❌ Migration failed: {e}")
        raise
    finally:
        conn.close()

if __name__ == '__main__':
    migrate()
