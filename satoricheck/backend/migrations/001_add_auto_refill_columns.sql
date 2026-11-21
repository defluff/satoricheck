-- Cloud SQL (PostgreSQL) migration: automated off-session CP refill columns on token_balances.
-- Idempotent: safe to run repeatedly or alongside backend/database.py:_apply_migrations().

ALTER TABLE token_balances ADD COLUMN IF NOT EXISTS auto_refill_enabled BOOLEAN DEFAULT FALSE;
ALTER TABLE token_balances ADD COLUMN IF NOT EXISTS auto_refill_package VARCHAR(32);
ALTER TABLE token_balances ADD COLUMN IF NOT EXISTS auto_refill_threshold INTEGER DEFAULT 0;
ALTER TABLE token_balances ADD COLUMN IF NOT EXISTS stripe_customer_id VARCHAR(128);
ALTER TABLE token_balances ADD COLUMN IF NOT EXISTS stripe_payment_method_id VARCHAR(128);
ALTER TABLE token_balances ADD COLUMN IF NOT EXISTS auto_refill_failed BOOLEAN DEFAULT FALSE;
