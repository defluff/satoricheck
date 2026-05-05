"""
Universal Database Patch for Authenix.
Fixes missing last_billed_at column in live_pro_sessions.
Works for both SQLite (Local) and PostgreSQL (Production).
"""
import sys
import os
from sqlalchemy import text, inspect

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.server import app
from backend.database import db_session, engine

def patch_database():
    """Apply schema patch."""
    print(f"Applying patch to database: {engine.url}")
    
    with app.app_context():
        inspector = inspect(engine)
        columns = [c['name'] for c in inspector.get_columns('live_pro_sessions')]
        
        if 'last_billed_at' in columns:
            print("✓ column 'last_billed_at' already exists.")
            return
            
        print("! Column 'last_billed_at' missing. Adding it...")
        
        try:
            # PostgreSQL and SQLite syntax differences
            is_sqlite = 'sqlite' in str(engine.url)
            
            if is_sqlite:
                # SQLite: Add as nullable first to avoid "non-constant default" error
                db_session.execute(text("ALTER TABLE live_pro_sessions ADD COLUMN last_billed_at TIMESTAMP"))
                print("✓ Added column (nullable)")
                
                # Backfill
                db_session.execute(text("UPDATE live_pro_sessions SET last_billed_at = started_at"))
                print("✓ Backfilled data")
                
            else:
                # PostgreSQL (Production): Can do it all in one go efficiently
                db_session.execute(text("ALTER TABLE live_pro_sessions ADD COLUMN last_billed_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP"))
                print("✓ Added column with default")
                
                # Backfill any nulls just in case (though default covers new/future, existing rows get default)
                db_session.execute(text("UPDATE live_pro_sessions SET last_billed_at = started_at WHERE last_billed_at IS NULL"))
            
            db_session.commit()
            print("✅ Patch applied successfully!")
            
        except Exception as e:
            db_session.rollback()
            print(f"❌ Patch failed: {e}")
            raise

if __name__ == "__main__":
    patch_database()
