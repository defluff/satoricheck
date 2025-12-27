
from backend.database import engine
from sqlalchemy import text

def run_migration():
    print("Running migration...")
    with engine.connect() as conn:
        # Migration 1: unbilled_words column
        try:
            conn.execute(text("ALTER TABLE token_balances ADD COLUMN unbilled_words INTEGER DEFAULT 0"))
            print("✓ Added unbilled_words column to token_balances")
        except Exception as e:
            print(f"Note (unbilled_words): {e}")
        
        # Migration 2: api_token column for secure auth
        try:
            conn.execute(text("ALTER TABLE users ADD COLUMN api_token VARCHAR(64)"))
            print("✓ Added api_token column to users")
        except Exception as e:
            print(f"Note (api_token): {e}")
        
        # Migration 3: source_reliability column
        try:
            conn.execute(text("ALTER TABLE fact_checks ADD COLUMN source_reliability VARCHAR(20)"))
            print("✓ Added source_reliability column to fact_checks")
        except Exception as e:
            print(f"Note (source_reliability): {e}")
        
        # Commit changes
        conn.commit()
        print("Migration complete!")

if __name__ == "__main__":
    run_migration()
