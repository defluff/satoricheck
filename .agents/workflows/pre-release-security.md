---
description: Automated security and stability gate before production deployment
---

# Pre-Release Security & Stability Gate

Run this checklist **before merging to main, building Docker images, or deploying to Cloud Run**.
Checks that can be expressed as assertions live in the pytest suite (so they also run in every
normal test run); this workflow only adds what pytest cannot cover: secrets, dependency CVEs,
container hygiene, and deploy prerequisites. Do not paste ad-hoc verification scripts here:
add a test instead.

---

## 1. Secret Scan

// turbo

```bash
cd satoricheck
echo "=== Secret scan ==="

# Preferred: entropy + provider-aware scanner (brew install gitleaks)
if command -v gitleaks >/dev/null 2>&1; then
  gitleaks detect --source .. --no-banner --redact || { echo "❌ gitleaks found secrets"; exit 1; }
else
  echo "⚠️ gitleaks not installed; falling back to regex scan (weaker)"
fi

# Regex fallback for application code (tests use fake keys by design)
MATCHES=$(grep -rn --include="*.py" --include="*.js" --include="*.html" \
  --exclude-dir="tests" --exclude-dir=".venv" --exclude-dir=".git" --exclude-dir="vendor" \
  -E "(sk_live_[0-9a-zA-Z]{24}|rk_live_[0-9a-zA-Z]{24}|whsec_[0-9a-zA-Z]{32}|AIza[0-9A-Za-z_-]{35}|GOCSPX-[0-9A-Za-z_-]{28}|ghp_[0-9A-Za-z]{36}|AKIA[0-9A-Z]{16}|xai-[0-9a-zA-Z]{30,}|BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY)" .)
if [ -n "$MATCHES" ]; then echo "❌ Hardcoded secrets:"; echo "$MATCHES"; exit 1; fi
echo "✓ No hardcoded secrets in application code"

git ls-files | grep -E "(^|/)\.env$" && { echo "❌ .env is tracked in git"; exit 1; } || echo "✓ No .env tracked"
```

---

## 2. Dependency Vulnerability Check

// turbo

```bash
cd satoricheck
../.venv/bin/python -m pip_audit -r requirements.txt
```

---

## 3. Security Regression Tests (fast gate)

Covers, with real assertions: route authorization (fail-closed), emitted security headers,
SRI / no runtime CDN loads / vendored-file hashes, scheduler-secret auth + production startup
guard, TEST_MODE production guard, IDOR/SSRF fixes, Stripe redirect-only fulfilment, extension
token handling.

// turbo

```bash
cd satoricheck
../.venv/bin/pytest -q \
  tests/test_route_auth_coverage.py \
  tests/test_security_headers.py \
  tests/test_frontend_supply_chain.py \
  tests/test_scheduler_auth.py \
  tests/test_security_fixes.py \
  tests/test_extension_security.py
```

---

## 4. Frontend Anti-Patterns

// turbo

```bash
cd satoricheck
grep -rn "eval(\|new Function(\|document\.write" frontend/js/ && { echo "❌ Dangerous JS sink found"; exit 1; } || echo "✓ No eval/new Function/document.write"
echo "--- innerHTML assignments (each must be DOMPurify-sanitized or a static string) ---"
grep -rn "innerHTML\s*=" frontend/js/ | grep -v "DOMPurify\|sanitize\|= ''\|= \"\"" || echo "✓ None unsanitized"
```

Review every line printed by the `innerHTML` check by hand; the grep cannot prove safety.

---

## 5. Dockerfile Hardening

// turbo

```bash
echo "--- Base image (pin by @sha256 digest when feasible) ---"
head -n 1 Dockerfile
grep -E "^USER " Dockerfile || { echo "❌ No non-root USER"; exit 1; }
grep -iE "^ENV.*(KEY|SECRET|PASSWORD|TOKEN)" Dockerfile && { echo "❌ Secret baked into image"; exit 1; } || echo "✓ No secrets in image layers"
```

---

## 6. Deploy Prerequisites (manual, requires `gcloud`)

Not turbo: these touch production configuration.

```bash
export REGION=<cloud-run-region>

# 6a. Cloud Run must define SCHEDULER_SECRET (startup fails in production without it)
gcloud run services describe <service> --region "$REGION" \
  --format='value(spec.template.spec.containers[0].env)' | tr ';' '\n' | grep -c SCHEDULER_SECRET

# 6b. Both cron jobs exist and last ran successfully (retention depends on the first one)
gcloud scheduler jobs describe authenix-cleanup-expired --location "$REGION" \
  --format='value(state,lastAttemptTime,status.code)'
gcloud scheduler jobs describe authenix-wizard-refill --location "$REGION" \
  --format='value(state,lastAttemptTime,status.code)'
```

Expected: state `ENABLED`, no error `status.code`. Job definitions and creation commands: README,
"Cloud Scheduler jobs". If a job is missing, **do not deploy**: expired user data would never be purged.

---

## 7. Full Regression Suite

// turbo

```bash
cd satoricheck
../.venv/bin/pytest tests/ --ignore=tests/e2e
```

---

## Verdict

- **Pass (✓):** every automated step passes, step 6 confirms prerequisites, zero test failures.
- **Fail (❌):** any secret finding, CVE, failing security test, missing cron job/secret, or
  unsanitised `innerHTML` **blocks deployment**.
