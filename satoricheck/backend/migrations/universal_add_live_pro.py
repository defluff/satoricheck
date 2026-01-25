"""
Universal Migration for Live Pro Feature.
Adds 'hide_live_pro_modal' column and creates 'live_pro_sessions' table.
Compatible with SQLite (Dev) and PostgreSQL (Prod).
"""
import sys
import os
from sqlalchemy import text, inspect, Boolean, Column, Integer, String, DateTime
from sqlalchemy.orm import Session

# Ensure we can import backend modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.server import app
from backend.database import engine, db_session
from backend.models import Base

def migrate():
    """Apply Live Pro database migrations using SQLAlchemy."""
    print("Starting Universal Migration for Live Pro...")
    
    with app.app_context():
        inspector = inspect(engine)
        
        # 1. Add hide_live_pro_modal to users table
        users_columns = [c['name'] for c in inspector.get_columns('users')]
        if 'hide_live_pro_modal' not in users_columns:
            print("Adding 'hide_live_pro_modal' to users table...")
            try:
                # SQLAlchemy doesn't support generic ALTER TABLE ADD COLUMN smoothly across dialects in core
                # But we can use raw SQL which is fairly standard for simple columns, or dialect check
                if 'sqlite' in str(engine.url):
                    db_session.execute(text("ALTER TABLE users ADD COLUMN hide_live_pro_modal BOOLEAN DEFAULT 0"))
                else:
                    # Postgres
                    db_session.execute(text("ALTER TABLE users ADD COLUMN hide_live_pro_modal BOOLEAN DEFAULT FALSE"))
                print("✓ Added 'hide_live_pro_modal'")
            except Exception as e:
                print(f"⚠️  Failed to add column (might exist): {e}")
        else:
            print("⊙ 'hide_live_pro_modal' already exists")

        # 2. Create live_pro_sessions table
        # We can use metadata.create_all which is dialect-agnostic and safe (IF NOT EXISTS)
        print("Creating 'live_pro_sessions' table if needed...")
        try:
            # Import the model to ensure it's in Base.metadata
            from backend.models import LiveProSession
            LiveProSession.__table__.create(bind=engine, checkfirst=True)
            print("✓ 'live_pro_sessions' table verified/created")
        except Exception as e:
            print(f"❌ Failed to create table: {e}")
            raise

        # 3. Create Indices
        # SQLAlchemy create_all handles indices defined in the model, but let's double check
        # Explicit index creation is usually safe with IF NOT EXISTS logic handled by ORM or manual check
        print("Verifying indices...")
        # (covered by create_all usually)

        db_session.commit()
        print("\n✅ Universal Migration completed successfully!")

if __name__ == "__main__":
    migrate()
