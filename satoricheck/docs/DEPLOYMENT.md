# SatoriCheck Deployment Guide

Deploy SatoriCheck to **Google Cloud Run** with **Cloud SQL (PostgreSQL)**.

---

## Prerequisites

- Google Cloud account with billing enabled
- Stripe account (live mode)
- Domain: `satoricheck.com` with DNS access
- GitHub repo: `https://github.com/defluff/satoricheck.git`

---

## Step 0: Push to GitHub

```bash
# Initialize git (if not already done)
git init
git add .
git commit -m "Initial release"

# Link to GitHub
git remote add origin https://github.com/defluff/satoricheck.git
git branch -M main
git push -u origin main
```

---

## Step 1: Create Dockerfile

Add this to the project root:

```dockerfile
FROM python:3.12-slim

RUN apt-get update && apt-get install -y gcc libpq-dev && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV PORT=8080
CMD exec gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 0 backend.server:app
```

---

## Step 2: Setup GCP Project

```bash
# Login
gcloud auth login
gcloud config set project [YOUR_PROJECT_ID]

# Enable APIs
gcloud services enable run.googleapis.com sqladmin.googleapis.com cloudbuild.googleapis.com
```

---

## Step 3: Create Cloud SQL Database

```bash
# Create instance (takes ~5 min)
gcloud sql instances create satoricheck-db \
  --database-version=POSTGRES_15 \
  --region=europe-west6 \
  --tier=db-f1-micro

# Create database
gcloud sql databases create satoricheck --instance=satoricheck-db

# Set password
gcloud sql users set-password postgres \
  --instance=satoricheck-db \
  --password=[SECURE_PASSWORD]
```

**Connection String Format:**
```
postgresql://postgres:[PASSWORD]@/satoricheck?host=/cloudsql/[PROJECT_ID]:europe-west6:satoricheck-db
```

---

## Step 4: Build & Deploy

```bash
# Build container
gcloud builds submit --tag gcr.io/[PROJECT_ID]/satoricheck

# Deploy to Cloud Run
gcloud run deploy satoricheck \
  --image gcr.io/[PROJECT_ID]/satoricheck \
  --platform managed \
  --region europe-west6 \
  --allow-unauthenticated \
  --add-cloudsql-instances [PROJECT_ID]:europe-west6:satoricheck-db \
  --set-env-vars "FLASK_ENV=production" \
  --set-env-vars "FLASK_SECRET_KEY=[GENERATE_WITH: python -c 'import secrets; print(secrets.token_hex(32))']" \
  --set-env-vars "DATABASE_URL=postgresql://postgres:[DB_PASSWORD]@/satoricheck?host=/cloudsql/[PROJECT_ID]:europe-west6:satoricheck-db" \
  --set-env-vars "GEMINI_API_KEY=[YOUR_KEY]" \
  --set-env-vars "GOOGLE_CLIENT_ID=[YOUR_OAUTH_CLIENT_ID]" \
  --set-env-vars "GOOGLE_CLIENT_SECRET=[YOUR_OAUTH_CLIENT_SECRET]" \
  --set-env-vars "STRIPE_SECRET_KEY=[LIVE_KEY]" \
  --set-env-vars "STRIPE_PUBLISHABLE_KEY=[LIVE_PUBLISHABLE_KEY]" \
  --set-env-vars "STRIPE_WEBHOOK_SECRET=[LIVE_WEBHOOK_SECRET]" \
  --set-env-vars "DEEPGRAM_API_KEY=[YOUR_KEY]"
```

Cloud Run will give you a URL like: `https://satoricheck-xyz-ew.a.run.app`

---

## Step 5: Map Custom Domain

1. Go to Cloud Run → **Manage Custom Domains**
2. Map `app.satoricheck.com` to the service
3. Update DNS with the provided records (usually an A record)

---

## Step 6: Configure OAuth & Stripe

### Google OAuth
1. **Google Cloud Console** → **APIs & Services** → **Credentials**
2. Edit OAuth 2.0 Client
3. Add redirect URI: `https://app.satoricheck.com/api/auth/callback/google`

### Stripe Webhook
1. **Stripe Dashboard** → **Developers** → **Webhooks**
2. Add endpoint: `https://app.satoricheck.com/api/billing/webhook`
3. Select events: `checkout.session.completed`, `invoice.payment_succeeded`
4. Copy the webhook signing secret and redeploy with updated `STRIPE_WEBHOOK_SECRET`

---

## Step 7: Update Landing Page

On `satoricheck.com`, update button links to:
- **Log In** → `https://app.satoricheck.com`
- **Get Started** → `https://app.satoricheck.com`

---

## Verification Checklist

- [ ] App loads at `https://app.satoricheck.com`
- [ ] Google login works
- [ ] Test Stripe checkout (use test mode first)
- [ ] Webhook fires and updates balance
- [ ] Fact-check works
- [ ] AI Detection works (manual, selection, auto-check)
- [ ] Live Pro transcription connects

**Check logs:**
```bash
gcloud logs read --service=satoricheck --limit=50
```

---

## Updates

To deploy changes:
```bash
git push
gcloud builds submit --tag gcr.io/[PROJECT_ID]/satoricheck
gcloud run deploy satoricheck --image gcr.io/[PROJECT_ID]/satoricheck
```


---

## 🛠️ Manual Administration (Adding CP manually)

If you need to manually add tokens (CP) to a user without Stripe:

### Option A: Google Cloud Console (Recommended for Cloud)
The easiest way to influence the Production DB directly is **Cloud SQL Studio**.

1. Go to **Google Cloud Console** → **SQL**
2. Select your instance (`satoricheck-db`)
3. Click **Cloud SQL Studio** in the left menu.
4. Sign in with user `postgres` and your password.
5. Run SQL queries directly:

```sql
-- Check a user's ID
SELECT id, email FROM users WHERE email = 'andy@kiniroo.com';

-- Update their balance
UPDATE token_balances 
SET balance = 5000 
WHERE user_id = (SELECT id FROM users WHERE email = 'andy@kiniroo.com');

-- (Optional) Log the transaction so it appears in history
INSERT INTO transactions (user_id, type, amount, description, timestamp)
VALUES (
    (SELECT id FROM users WHERE email = 'andy@kiniroo.com'), 
    'admin_grant', 
    5000, 
    'Manual Admin Gift', 
    NOW()
);
```

### Option B: Using `manage.py` (Local Only)
We have included a `manage.py` script in the project root. This is great for your **local** testing.

```bash
# List all users
python manage.py list-users

# Set a specific balance
python manage.py set-balance user@example.com 5000

# Add tokens to existing balance
python manage.py add-tokens user@example.com 100
```


