# Authenix Migrations Architecture

Authenix uses an automated, lightweight schema migration pattern:

1. **New Tables**: Defined as declarative SQLAlchemy models in [`backend/models.py`](../models.py). Initialized automatically via `Base.metadata.create_all()` in [`backend/database.py:init_db()`](../database.py).
2. **Column Additions (Schema Drift)**: Automatically detected and applied at application startup by [`backend/database.py:_apply_migrations()`](../database.py), which inspects live tables against model definitions and executes `ALTER TABLE ADD COLUMN` statements safely across SQLite (development) and PostgreSQL / Cloud SQL (production).
3. **Database Engine Transitions**: For migrating complete SQLite databases to PostgreSQL, use the dedicated CLI utility [`backend/migrate_to_postgres.py`](../migrate_to_postgres.py).
4. **Explicit Cloud SQL SQL Scripts**: Idempotent PostgreSQL `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` scripts for manual/DBA verification or pre-deploy execution:
   - [`001_add_auto_refill_columns.sql`](./001_add_auto_refill_columns.sql) — Adds `auto_refill_enabled`, `auto_refill_package`, `auto_refill_threshold` (`DEFAULT 0`), `stripe_customer_id`, `stripe_payment_method_id`, and `auto_refill_failed` to `token_balances`.

