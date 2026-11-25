---
trigger: always_on
---

# Authenix — Project Invariants

## 1. Context, Domains & Runtime
- **Naming**: User-facing UI/assets use **Authenix**; backend routes, Cloud Run service (`satoricheck`), and DB (`satoricheck.db`) retain `satoricheck`.
- **Domains**:
  - `https://authenix.ai` — external Framer marketing/legal site only (never serves Flask/API).
  - `https://satoricheck-829698588154.europe-west6.run.app` (prod) and `http://localhost:8000` (local) — live app, API, OAuth callbacks, Cloud Scheduler, and Chrome extension target. Never point API/OAuth/extension URLs to `authenix.ai`.
- **Runtime**: Python 3.12+ (Cloud Run Docker in prod; `.venv/` at repo root locally).
- **Scope**: Keep single-layer audits/edits within that layer; log cross-layer findings to the active TODO doc.

## 2. Tech Stack & Invariants
- **Backend**: Flask 3.x + SQLAlchemy (SQLite WAL dev, Cloud SQL PostgreSQL prod), `ProxyFix` for Cloud Run headers. Always call `db_session.remove()` in teardown/`finally`.
- **AI Models & Billing**:
  - Use explicit model IDs (never `-latest` aliases): `Config.GEMINI_MODEL` (`gemini-3.8-flash`) for text/audio-only (`audio/*`), `Config.GEMINI_MEDIA_MODEL` (`gemini-4-argon`) for visual/video (`image/*`, `video/*`) and pitch decks, and `Config.GROK_MODEL` (`grok-4.7`) for conditional social context. `thinking_level='minimal'` is unsupported. Validate model swaps with `tests/test_gemini_model_smoke.py`.
  - Reserve Check Points (CP) atomically before metered AI calls; clean up Gemini Files API uploads (`>14 MB` media) in `finally`.
- **Frontend**: Vanilla ES6+ JS, HTML5, CSS3 (`core.css` + `app.css`), mobile-first. No React/Vue/Next.js/Tailwind unless explicitly requested.

## 3. Security & Auth
- **Auth & IDOR**: Protect non-public routes with `@login_required`; scope all user data queries to `request.current_user.id`.
- **Config**: Load secrets via `Config` from env vars (`Config.validate()` at startup). `TEST_MODE=true` is blocked when `FLASK_ENV=production`.
- **SSRF**: Validate outbound URL fetches against private/loopback/link-local IPs.
- **Stripe**: Fulfill only via `stripe.Webhook.construct_event()` with `stripe_session_id` deduplication — never credit tokens on `/success`.
- **Extension**: Store tokens at rest with AES-GCM-256 (`lib/storage.js`).

## 4. Testing (`tests/conftest.py` & `tests/test_suite_integrity.py`)
- **Env & Command**: Use repo-root `.venv/` (run `../.venv/bin/pytest tests/ --ignore=tests/e2e` from `satoricheck/`).
- **Personas**: `test@authenix.ai` for `TEST_MODE=true` only; `@example.com` (RFC 2606) for unit/E2E tests.
- **Integrity Rules**:
  - **Wire-only mocking**: Mock only external SDK/HTTP boundaries (`generate_content`, `stripe.checkout.Session.create`, `requests.post` to xAI) — never DB sessions, route helpers, or local crypto/SSRF/webhook validators.
  - **Exact & triple-state assertions**: Assert exact HTTP status codes and payloads (no `status_code in [...]`, `assert True`, or `a or b`). For mutating tests, assert HTTP response, persisted `db_session` state, and external SDK call args.
  - **Break-to-verify**: Temporarily invert guarded logic to confirm new/updated tests fail with `AssertionError`, then restore and verify a clean `git diff`.