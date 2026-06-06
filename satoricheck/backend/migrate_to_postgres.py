#!/usr/bin/env python3
"""
Migration script: SQLite to PostgreSQL

This script exports data from SQLite and imports it into PostgreSQL.
Run this once when migrating to Cloud SQL.

Usage:
    1. Set DATABASE_URL to your PostgreSQL connection string
    2. Run: python -m backend.migrate_to_postgres

The script will:
    1. Read all data from the SQLite database
    2. Create tables in PostgreSQL
    3. Insert all records
    4. Verify record counts match
"""
import os
import sys
from datetime import datetime

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.models import Base, User, TokenBalance, Streak, Transaction, FactCheck


# Configuration
SQLITE_URL = 'sqlite:///authenix.db'
POSTGRES_URL = os.getenv('DATABASE_URL')

if not POSTGRES_URL or 'postgresql' not in POSTGRES_URL:
    print("❌ Error: Set DATABASE_URL to a PostgreSQL connection string")
    print("   Example: DATABASE_URL=postgresql://user:pass@localhost:5432/authenix")
    sys.exit(1)


def migrate():
    """Migrate data from SQLite to PostgreSQL."""
    print("=" * 60)
    print("SQLite → PostgreSQL Migration")
    print("=" * 60)
    
    # Connect to SQLite
    print("\n1. Connecting to SQLite...")
    sqlite_engine = create_engine(SQLITE_URL, connect_args={'check_same_thread': False})
    SqliteSession = sessionmaker(bind=sqlite_engine)
    sqlite_session = SqliteSession()
    
    # Connect to PostgreSQL
    print("2. Connecting to PostgreSQL...")
    pg_engine = create_engine(
        POSTGRES_URL,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True
    )
    PgSession = sessionmaker(bind=pg_engine)
    pg_session = PgSession()
    
    # Create tables in PostgreSQL
    print("3. Creating tables in PostgreSQL...")
    Base.metadata.create_all(bind=pg_engine)
    
    # Migrate each table
    tables = [
        (User, 'users'),
        (TokenBalance, 'token_balances'),
        (Streak, 'streaks'),
        (Transaction, 'transactions'),
        (FactCheck, 'fact_checks')
    ]
    
    for model, name in tables:
        print(f"\n4. Migrating {name}...")
        
        # Read from SQLite
        records = sqlite_session.query(model).all()
        sqlite_count = len(records)
        print(f"   Found {sqlite_count} records in SQLite")
        
        if sqlite_count == 0:
            print(f"   ⏭️  Skipping (no data)")
            continue
        
        # Check if already migrated
        existing = pg_session.query(model).count()
        if existing > 0:
            print(f"   ⚠️  PostgreSQL already has {existing} records. Skipping to avoid duplicates.")
            continue
        
        # Insert into PostgreSQL
        for record in records:
            # Create a new detached instance
            pg_session.merge(record)
        
        pg_session.commit()
        
        # Verify
        pg_count = pg_session.query(model).count()
        if pg_count == sqlite_count:
            print(f"   ✅ Migrated {pg_count} records successfully")
        else:
            print(f"   ❌ Mismatch! SQLite: {sqlite_count}, PostgreSQL: {pg_count}")
    
    # Close sessions
    sqlite_session.close()
    pg_session.close()
    
    print("\n" + "=" * 60)
    print("✅ Migration complete!")
    print("=" * 60)
    print("\nNext steps:")
    print("1. Update .env with DATABASE_URL pointing to PostgreSQL")
    print("2. Restart the server")
    print("3. Verify the app works correctly")
    print("4. (Optional) Delete the local .db file after confirming migration")


if __name__ == '__main__':
    migrate()
