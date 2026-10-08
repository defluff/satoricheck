---
name: monthly-security-audit
description: Comprehensive monthly security and compliance audit for Authenix. Covers CVE scanning, Dependabot alerts, secret exposure and key rotation, route authorization, frontend supply chain, live security headers, container and Cloud Run hardening, Cloud Scheduler data-retention jobs, and GDPR compliance. Use monthly or before major releases.
---

# Monthly Security Audit Protocol — Authenix

Run monthly or before major production releases. Work through the phases in order and finish
by writing an audit report (Phase 7). Run all commands from the repository root.

Principle: anything expressible as an assertion belongs in the pytest suite, not in this
document. Phases below call those tests and then cover what only a human with cloud access can check.

---

## Phase 1 — Dependencies, CVEs & Supply Chain

### 1a. Python CVE scan
```bash
.venv/bin/python -m pip_audit -r satoricheck/requirements.txt
```
Pass: zero vulnerabilities. On failure, triage Critical/High immediately (OSV.dev / NVD).

### 1b. Outdated packages
```bash
.venv/bin/python -m pip list --outdated
```
Priority: `cryptography`, `Authlib`, `PyJWT` (auth) → `Flask`, `Werkzeug`, `gunicorn` (server) →
`requests`, `aiohttp`, `urllib3` (HTTP/SSRF) → `stripe`, `google-genai` (payments/AI).

### 1c. Frontend supply chain
```bash
.venv/bin/pytest satoricheck/tests/test_frontend_supply_chain.py -q
```
Asserts: every third-party `<script>` has SRI + `crossorigin="anonymous"`, no JS loads code from a CDN at
runtime, vendored libraries (`frontend/vendor/`) match their recorded SHA-384.
Manually check CDN-pinned libraries (DOMPurify, html-to-image) and the vendored PDF.js for new
security releases; to upgrade, follow `frontend/vendor/pdfjs-*/README.md` and recompute SRI hashes with
`curl -sL <url> | openssl dgst -sha384 -binary | openssl base64 -A`.

### 1d. Dependabot & GitHub security alerts
1. GitHub → Security → Dependabot: review and merge security PRs.
2. Re-run the suite after merging: `.venv/bin/pytest satoricheck/tests/ --ignore=satoricheck/tests/e2e`.

---

## Phase 2 — Secret Exposure & Key Rotation

### 2a. Secret scan
Use the same step as the pre-release workflow (`/pre-release-security`, step 1): `gitleaks` when
installed, regex fallback otherwise. Also review GitHub → Security → Secret scanning alerts.

### 2b. Rotation tracker (quarterly)
Check each secret's last rotation date in Secret Manager (`gcloud secrets versions list <name>`).

| Secret | Rotation |
|---|---|
| `STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET` | Stripe Dashboard → new key → Secret Manager → revoke old |
| `GEMINI_API_KEY` | AI Studio → new key → Secret Manager → revoke old |
| `GROK_API_KEY` | xAI Console → new key → Secret Manager → revoke old |
| `GOOGLE_CLIENT_SECRET` | Cloud Console → rotate → Secret Manager |
| `SCHEDULER_SECRET` | `.venv/bin/python -c "import secrets; print(secrets.token_hex(32))"` → Secret Manager/Cloud Run → `gcloud scheduler jobs update http` on **both** jobs |

---

## Phase 3 — Authorization & Access Control

### 3a. Route authorization, scheduler and webhook contracts
```bash
.venv/bin/pytest satoricheck/tests/test_route_auth_coverage.py satoricheck/tests/test_scheduler_auth.py -q
```
Fail-closed: every registered route is either user-protected (401/403 unauthenticated) or in an
explicit PUBLIC / SCHEDULER / WEBHOOK allowlist.
**Review the allowlists themselves**: `git log -p -- satoricheck/tests/test_route_auth_coverage.py`
for the last month. Every addition must have a justification.

### 3b. IDOR scoping
```bash
grep -rn "filter.*user_id" satoricheck/backend/routes/ satoricheck/backend/services/ \
  | grep -v "current_user\.id\|user\.id\|user_id=user_id"
```
Every line printed must be explained (e.g. admin/cron paths). Also sample two new endpoints from the
month's `git log` and confirm they scope queries by `request.current_user.id`.

### 3c. Billing abuse
Confirm every route calling Gemini/Grok checks `TokenBalance` first and enforces payload caps and a
`@limiter.limit(...)` (see `tests/test_security_fixes.py::TestWalletDrainProtections`).

---

## Phase 4 — Live Production Verification

### 4a. Live security headers
```bash
curl -sI https://authenix.ai | grep -iE "strict-transport|x-frame|x-content-type|content-security|referrer-policy"
```
| Header | Expected |
|---|---|
| `Strict-Transport-Security` | `max-age=31536000; includeSubDomains` |
| `X-Frame-Options` | `DENY` |
| `X-Content-Type-Options` | `nosniff` |
| `Referrer-Policy` | `strict-origin-when-cross-origin` |
| `Content-Security-Policy` | `default-src 'self'`, `frame-ancestors 'none'`, `worker-src 'self' blob:` |

The emitted-header contract is unit-tested (`tests/test_security_headers.py`); this check detects
drift introduced by the load balancer or domain configuration. Known weakness to track:
CSP `script-src` still allows `'unsafe-inline'`.

### 4b. TLS
Check the certificate expiry and grade (e.g. SSL Labs) for the production domain.

---

## Phase 5 — Container & Cloud Hardening

### 5a. Dockerfile
```bash
head -n 1 Dockerfile                               # prefer pinning by @sha256 digest
grep -E "^USER " Dockerfile || echo "❌ no non-root USER"
grep -iE "^ENV.*(KEY|SECRET|PASSWORD|TOKEN)" Dockerfile && echo "❌ secret in image" || echo "✓ clean"
```

### 5b. Cloud Run
Verify in the console or via `gcloud run services describe`:
- `FLASK_ENV=production`, `TEST_MODE=false`
- Secrets sourced from Secret Manager: `GEMINI_API_KEY`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`,
  `DATABASE_URL`, `SCHEDULER_SECRET`
- Service account has least privilege; ingress/auth settings are as intended.

---

## Phase 6 — Data Retention & GDPR

### 6a. Retention jobs are running (critical)
The 7-day purge only happens if Cloud Scheduler calls it (no in-process scheduler exists).
```bash
export REGION=<cloud-run-region>
gcloud scheduler jobs describe authenix-cleanup-expired --location "$REGION" \
  --format='value(state,lastAttemptTime,status.code)'
```
Expected: `ENABLED`, `lastAttemptTime` within 24h, no error code. If missing or failing, this is a
**High** finding: user data is being retained beyond policy. Spot-check production: no
`fact_checks` / `media_checks` row older than 8 days.

### 6b. Account deletion cascade
`delete_account()` in `backend/routes/auth.py` must purge `TokenBalance`, `Streak`, `Transaction`,
`FactCheck`, `MediaCheck`, and write only a SHA-256 `email_hash` tombstone (`DeletedUser`):
```bash
grep -n "DeletedUser\|email_hash" satoricheck/backend/models.py satoricheck/backend/routes/auth.py
```
Confirm any newly added user-owned table is included in the cascade.

### 6c. Third-party data processors
Privacy policy (README → Legal) must still match reality: Google Gemini (text/media/audio), xAI Grok
(social context), Stripe (payments).

---

## Phase 7 — Audit Report

Write `security_audit_YYYY-MM-DD.md` with:
1. **Status:** PASS / PASS WITH WARNINGS / FAIL
2. **Findings** by severity (Critical / High / Medium / Low), each with evidence and owner
3. **Action items** (including Dependabot merges and rotations performed)
4. **Checks not performed** and why (so gaps are visible, not silent)

---

## Emergency Playbooks

### API key compromise
1. Revoke in the provider console (AI Studio / Stripe / xAI) immediately.
2. Create a replacement and update Secret Manager.
3. Redeploy Cloud Run (new revision picks up the secret).
4. Review Cloud Run request logs for the exposure window.

### Stored XSS
1. Set `MAINTENANCE_MODE=true` in Cloud Run and redeploy.
2. Find the sink: `grep -rn "innerHTML" satoricheck/frontend/js/`.
3. Fix with `textContent` or `DOMPurify.sanitize()`, add a regression test, redeploy.
4. Purge affected records, then disable maintenance mode.
