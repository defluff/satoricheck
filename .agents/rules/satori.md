---
trigger: always_on
---

# authenix — Architecture & Project Invariants

## 1. Project Context & Branding
- **Product Name**: **Authenix** (live AI verification platform for text, audio streams, and media forensics).
- **Codebase Naming**: Frontend and user-facing assets use **Authenix**. Backend routes, Cloud Run service name (`satoricheck`), and database filenames (`satoricheck.db`) retain `satoricheck` until a dedicated Authenix Cloud Run service / custom app domain is provisioned.
- **Domains & Deployment Reality**:
  - **Marketing & Legal Site (Framer, external):** `https://authenix.ai` (hosts landing pages and `https://authenix.ai/privacy_and_terms`). It does **not** serve the Flask app or API.
  - **Live Web App & API (Google Cloud Run, `europe-west6`, service `satoricheck`):** `https://satoricheck-829698588154.europe-west6.run.app`. All live endpoints, OAuth callbacks (`/api/auth/google/callback`, `/api/storage/google_drive/callback`, `/api/storage/onedrive/callback`, `/api/storage/dropbox/callback`), Cloud Scheduler jobs, and the Chrome Extension target this origin.
  - **Allowed JS / OAuth Origins:** `https://satoricheck-829698588154.europe-west6.run.app` (production) and `http://localhost:8000` (local testing). Do **not** point API/OAuth/extension URLs to `https://authenix.ai` until a dedicated Authenix app domain is deployed on Cloud Run.
- **Runtime Environment**: Python 3.12+ in Docker / Cloud Run (production). Local dev uses `.venv/` at repository root.
- **Scope Discipline**: When auditing or implementing a single-layer spec (e.g., backend billing), keep code edits strictly within that layer; log cross-layer findings (frontend, extension, stale copy, legacy cleanup) to the active TODO doc rather than editing them inline.

---

## 2. Tech Stack & Architectural Invariants

### Backend (Flask 3.x + SQLAlchemy)
- **Blueprints**: Modular routing in `backend/routes/` (`auth.py`, `billing.py`, `factcheck.py`, `tokens.py`, `export.py`).
- **Database**: SQLAlchemy ORM.
  - *Dev*: SQLite (`satoricheck.db` / WAL mode).
  - *Prod*: Cloud SQL PostgreSQL via Google Cloud Run (`DATABASE_URL`).
- **DB Stability & Session Lifecycle**: Always invoke `db_session.remove()` in teardown/`finally` blocks to prevent SQLite database locks and Cloud SQL connection pool exhaustion.
- **Proxy & Headers**: Uses `ProxyFix(app.wsgi_app, x_proto=1, x_host=1)` to ensure correct IP resolution and HTTPS scheme behind Cloud Run load balancers.
- **AI Integrations**: Server-side client in `backend/services/gemini/client.py`. Module-boundary hybrid split: `Config.GEMINI_MODEL` (default `gemini-3.8-flash`, explicit ID — never a `-latest` alias) handles all text extraction, triage, verification, Flash-pinned context caches, and audio-only (`audio/*`) forensics (`thinking_level` `'low'` for extraction/triage/batch, `'high'` for single-claim and audio forensics; `'minimal'` is unsupported), while `Config.GEMINI_MEDIA_MODEL` (default `gemini-4-argon`) handles visual/video media forensics (`image/*`, `video/*`) and pitch deck visual extraction. Social context: xAI Grok (`backend/services/grok_service.py`) via `Config.GROK_MODEL` (default `grok-4.7`, explicit ID — never a `-latest` alias, capturing EU AI Act `x-watermark` header provenance), conditionally attached only on social triggers (`@`, `#`, quotes, breaking keywords) inside the single master verification loop (`_run_verification_loop`). Validate model swaps with `tests/test_gemini_model_smoke.py`.
- **Claim Extraction & Lean Fact-Checking Flow**: Claim extraction (`/api/factcheck/identify-claims`) is unmetered (`0 CP`, requires `balance >= 1` guard -> `403`) and uses `thinking_level='low'`; if no empirical claims are detected (conversational banter, pure opinion), processing halts immediately without invoking verification APIs or billing tokens. Discrete text endpoints enforce atomic flat Check Point (CP) reservation before external AI calls: `/api/factcheck/analyze` charges `1 CP` flat (`thinking_level='high'`, multi-turn `_run_verification_loop` with `max_turns=4` + closing turn, native Google Search grounding, and conditional Grok `search_social` tool calls); `/api/factcheck/analyze-batch` charges `1 CP` per micro-batch of up to `8` uncached claims (`0 CP` on DB cache hit, `thinking_level='low'` triage & `_run_verification_loop` with `max_turns=3` + closing turn, native Google Search grounding, and conditional Grok `search_social`); `/api/factcheck/analyze-ai` charges `1 CP` flat (`402` on insufficient balance when `TokenBalance` exists, `thinking_level='low'`, no search grounding).
- **Audio & Media Forensics**: Speech transcription runs locally in the browser via Web Speech API (`audio.js`) at zero API cost. Visual/video media forensics analyzes multimodal payloads via `Config.GEMINI_MEDIA_MODEL` (`thinking_level='high'`) with file-size tiered video sampling and pricing (`<= 5 MB` @ `4.0` FPS = 15 CP, `5–14 MB` @ `2.0` FPS = 25 CP, `> 14 MB` or YouTube URI @ `2.0` FPS = 40 CP), while audio-only (`audio/*`) routes to `Config.GEMINI_MODEL` (`gemini-3.8-flash`, `thinking_level='high'`, 5 CP), with a 30s video ingestion window (longer clips analyze their first 30s), inline transport `<= 14 MB`, and automated Files API upload/delete cleanup for payloads `> 14 MB`.

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
- **Test Integrity Invariants** (enforced mechanically by `tests/conftest.py` and `tests/test_suite_integrity.py`):
  1. **Wire-Only Mocking**: Mock only public external SDK/network boundaries (`client.models.generate_content`, `stripe.checkout.Session.create`, `requests.post` to xAI). Never mock DB sessions, route helpers, or local crypto/SSRF validators (`stripe.Webhook.construct_event`, JWT decode, `_is_private_ip`).
  2. **Exact Assertions**: Assert exact HTTP status codes and payload fields. Never use `status_code in [...]`, `assert True`, `or True`, or permissive `assert a or b`.
  3. **Triple State Verification**: For any mutating endpoint/service test, assert (a) exact HTTP status and payload, (b) persisted `db_session` state (`TokenBalance`, created/deleted rows), and (c) external SDK call arguments.
  4. **Break-to-Verify**: Prove mutation resistance by temporarily inverting the guarded business logic to confirm the test fails with `AssertionError`, then restore and verify a clean working tree via `git diff`.
