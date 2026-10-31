---
trigger: always_on
---

# authenix — Architecture & Project Invariants

## 1. Project Context & Branding
- **Product Name**: **Authenix** (live AI verification platform for text, audio streams, and media forensics).
- **Codebase Naming**: Frontend and user-facing assets use **Authenix**. Backend routes and database filenames retain `satoricheck` / `satoricheck.db` to maintain backwards compatibility in production.
- **Runtime Environment**: Python 3.12+ in Docker / Cloud Run (production). Local dev uses `.venv/` at repository root.

---

## 2. Tech Stack & Architectural Invariants

### Backend (Flask 3.x + SQLAlchemy)
- **Blueprints**: Modular routing in `backend/routes/` (`auth.py`, `billing.py`, `factcheck.py`, `tokens.py`, `export.py`).
- **Database**: SQLAlchemy ORM.
  - *Dev*: SQLite (`satoricheck.db` / WAL mode).
  - *Prod*: Cloud SQL PostgreSQL via Google Cloud Run (`DATABASE_URL`).
- **DB Stability & Session Lifecycle**: Always invoke `db_session.remove()` in teardown/`finally` blocks to prevent SQLite database locks and Cloud SQL connection pool exhaustion.
- **Proxy & Headers**: Uses `ProxyFix(app.wsgi_app, x_proto=1, x_host=1)` to ensure correct IP resolution and HTTPS scheme behind Cloud Run load balancers.
- **AI Integrations**: Server-side client in `backend/services/gemini/client.py`. One model for all Gemini calls via `Config.GEMINI_MODEL` (default `gemini-3.8-flash`, explicit ID — never a `-latest` alias), depth set per call with `thinking_level` (`low` for extraction/triage, `high` for verification/forensics; `minimal` is unsupported on current models). Embeddings: `gemini-embedding-2-preview`. Social context: xAI Grok (`backend/services/grok_service.py`), conditionally attached only on social triggers (`@`, `#`, quotes, breaking keywords). Validate model swaps with `tests/test_gemini_model_smoke.py`.
- **Claim Extraction & Lean Fact-Checking Flow**: Claim extraction (`/api/factcheck/identify-claims`) uses `thinking_level='low'`. If no empirical claims are detected (conversational banter, pure opinion), processing halts immediately without invoking verification APIs or billing tokens. Verification runs via single-turn native Google Search grounding (`thinking_level='high'`).
- **Audio & Media Forensics**: Speech transcription runs locally in the browser via Web Speech API (`audio.js`) at zero API cost. Media forensics analyzes multimodal video/image uploads via `Config.GEMINI_MODEL` (`thinking_level='high'`) with automated Files API upload/delete cleanup.

### Frontend (Vanilla Stack)
- **Core**: Vanilla JavaScript (ES6+), HTML5, Vanilla CSS3 (Custom Properties in `core.css` + `app.css`).
- **Design Rules**: Mobile-First strict requirement. Dark aesthetic with vibrant gradients, glassmorphism, 0.2s–0.3s transitions, and deep shadows.
- **Framework Policy**: No React/Vue/Next.js/Tailwind unless explicitly requested.

---

## 3. Core Security & Auth Invariants

- **Auth Cascade**:
  1. `authenix_token` JWT Cookie (`HttpOnly`, `SameSite=Lax`, `Secure` in prod).
  2. `Authorization: Bearer <jwt_or_token>` for Chrome extension & API.
  3. Google OAuth & Cookie Session Sync.
  4. Flask Session (`user_id`) fallback.
- **Authorization & IDOR**: All protected routes require `@login_required`. All queries fetching user data must filter by `request.current_user.id`.
- **Secrets & Modes**: Load via `Config` from environment variables only. `Config.validate()` runs at startup. `TEST_MODE=true` is strictly blocked when `FLASK_ENV=production`.
- **SSRF Defense**: Validate all outbound URL fetches against private/loopback/link-local IP ranges.
- **Stripe Billing**: All fulfillments require `stripe.Webhook.construct_event()` signature verification and check for duplicate `stripe_session_id`. Never credit tokens via `/success` redirect.
- **Extension Security**: Store extension tokens at rest using AES-GCM-256 (`lib/storage.js`).

---

## 4. Testing & Verification Rules
- **Python Environment**: Always use the virtual environment at repo root (`.venv/bin/python`, `.venv/bin/pytest`).
- **Test Personas**: `test@authenix.ai` is reserved for local `TEST_MODE=true`. Unit tests use `@example.com` (RFC 2606).
- **Regression Testing**: Run `pytest tests/ --ignore=tests/e2e` before completing backend tasks.
