---
name: ttp-security-audit
description: Lightweight TTP vulnerability hunter for Authenix routes, services, and frontend/extension JS. Use during monthly security audits, pre-release security gates, PR security reviews, or whenever auditing code changes for wallet/input bounds, IDOR, SSRF, XSS/CSV/header injection, or exception leaks.
---

# TTP Security Audit — Authenix

Hunts structural vulnerability patterns across `satoricheck/backend/`, `satoricheck/frontend/js/`, and `satoricheck/extension/` that `pytest` does not already enforce (`test_route_auth_coverage.py`, `test_security_headers.py`, `test_frontend_supply_chain.py`, `test_scheduler_auth.py`).

## Launch & Hunt

Run from the repository root (or from `satoricheck/` with `../` prefixes):

```bash
# Full repository scan
.venv/bin/python .agents/skills/ttp-security-audit/scripts/ttp_hunt.py

# Changed/untracked files only (PRs and pre-commit review)
.venv/bin/python .agents/skills/ttp-security-audit/scripts/ttp_hunt.py --diff

# Optional CI-strict exit (returns 1 if any leads are printed)
.venv/bin/python .agents/skills/ttp-security-audit/scripts/ttp_hunt.py --strict
```

## 5 Authenix TTP Hunting Lenses

Triage every surfaced lead (and manually inspect surrounding code) through these five lenses:

1. **Input & Wallet Bounds (`T1499` / `AML.T0034`)**: Secondary JSON fields concatenated into AI prompts without type/length caps, `base64.b64decode` before size/balance checks, pagination `min(...)` without `max(1, ...)`, unbounded `.all()` exports, missing `@limiter.limit`, or AI calls executed before `reserve_flat_tokens`.
2. **Tenant & Shared-Key Cache Isolation (`T1068`)**: ORM queries on user-owned tables (`FactCheck`, `MediaCheck`, `TokenBalance`, `Transaction`, `Streak`) missing `user_id == request.current_user.id`, or Gemini `cache_name` accepted without `hmac.compare_digest` ownership checks.
3. **SSRF Bypass (`T1210`)**: Outbound `requests.*` or `urllib.request.urlopen` calls bypassing `backend/services/safe_http.py`.
4. **Output & Header Sinks (`AML.T0054` / `T1059.007` / `T1204.002`)**: Unsanitized `innerHTML` / `eval` / `new Function` / `document.write`, unvalidated `href` schemes, unescaped CSV formula cells (`=`, `+`, `-`, `@`, `\t`, `\r`), or unvalidated request values interpolated into `Content-Disposition` / response headers.
5. **Exception & Resource Hygiene (`T1552` / `T1499`)**: Raw `APIError(str(e))` / `jsonify(...str(e)...)` leaks exposing upstream URLs, paths, or SDK errors; temp files missing `finally` cleanup; unclosed DB sessions.

## Regression Rule

When `ttp_hunt.py` or lens review confirms a real vulnerability, patch it and add a regression test in `satoricheck/tests/test_security_fixes.py`. Do **not** add unit tests for `ttp_hunt.py` itself.
