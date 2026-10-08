---
name: security-review
description: On-demand security checklist and review protocol for Authenix PRs, new endpoints, and sensitive code changes.
---

# Security Review Checklist — Authenix

Apply this checklist when auditing pull requests, authentication handlers, payment flows, browser extension updates, or external integrations.

---

## 1. Authentication & Route Scoping
- [ ] Every data or mutation endpoint is protected with `@login_required`.
- [ ] Any edit to the `PUBLIC` / `SCHEDULER` / `WEBHOOK` allowlists in `tests/test_route_auth_coverage.py` is justified in the PR description (these are the only auth exemptions).
- [ ] New cron endpoints use `@scheduler_secret_required` (constant-time `X-Scheduler-Secret` check), are `POST`, and are documented in the README "Cloud Scheduler jobs" table plus a Cloud Scheduler job.
- [ ] Webhooks authenticate by signature (`stripe.Webhook.construct_event`), never by a shared header alone.
- [ ] All database queries filter by `request.current_user.id` (zero trust for client-provided IDs).
- [ ] JWT decode calls explicitly enforce `algorithms=['HS256']` (never read algorithm from token header).
- [ ] Extension auth uses encrypted storage via `lib/storage.js` (AES-GCM-256).

## 2. Token Billing & Cost Protection
- [ ] Any endpoint triggering Gemini or Grok models verifies sufficient balance in `TokenBalance` before calling the API.
- [ ] Zero-balance accounts are blocked (HTTP 402/403) from triggering free model executions.
- [ ] Payload size caps are enforced (e.g. `MAX_TEXT_LENGTH = 50000`, 50MB file size limit).

## 3. Rate Limiting
- [ ] Expensive AI and analysis routes have explicit `@limiter.limit(...)` annotations (e.g. `30 per minute` on factcheck, `60 per minute` on audio chunks).
- [ ] Auth endpoints (signup, login) maintain aggressive brute-force rate limits.

## 4. Input Sanitization & XSS
- [ ] JSON body extraction safely handles empty or non-JSON payloads (`request.get_json() or {}`).
- [ ] String inputs are bounded and `.strip()`-ed before processing.
- [ ] Frontend uses `textContent` by default; any `innerHTML` assignment is sanitized with `DOMPurify.sanitize()`.
- [ ] No `eval()`, `new Function()`, or `document.write()` anywhere in frontend or extension code.
- [ ] Third-party CDN scripts in `frontend/index.html` include Subresource Integrity (`integrity="sha384-..." crossorigin="anonymous"`).
- [ ] No JS loads scripts/workers from a CDN at runtime (SRI cannot cover dynamic loads or Web Workers). Libraries needing workers are vendored under `frontend/vendor/<name>-<version>/` with a provenance README.
- [ ] CSP changes only narrow privileges, or are justified; `unsafe-inline`/`unsafe-eval` are never added.

## 5. Network & SSRF Guardrails
- [ ] Outbound URL-fetching functions resolve destination IPs via DNS and reject `is_private`, `is_loopback`, or `is_link_local`.
- [ ] Cloud metadata endpoints (`169.254.169.254` and `metadata.google.internal`) are blocked.
- [ ] Strict network timeouts are set on all external requests (`requests.get(..., timeout=5)`).

## 6. Stripe Payments & Webhooks
- [ ] Webhook handler uses `stripe.Webhook.construct_event()` with `STRIPE_WEBHOOK_SECRET`.
- [ ] Duplicate transaction check on `stripe_session_id` before crediting tokens.
- [ ] `/api/billing/success` performs UI redirect only (tokens are credited exclusively via verified webhook).

## 7. Chrome Extension Security
- [ ] `manifest.json` uses Manifest V3 and restricts host permissions to required endpoints (no `<all_urls>` wildcards).
- [ ] Extension tokens at rest are encrypted with AES-GCM-256 (`lib/storage.js`).
- [ ] Sensitive API keys are never bundled inside client extension code.

## 8. Error Handling & Secrets
- [ ] No API keys, passwords, or tokens hardcoded in application code or client-facing JS.
- [ ] Error handlers in `error_handlers.py` return generic error JSON (no internal stack traces or raw SQL leaked).
- [ ] `cleanup_db()` is called on `@app.teardown_appcontext` to avoid SQLite locks and Cloud SQL connection pool exhaustion.
