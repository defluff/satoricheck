---
description: Start local server and extension for manual and automated testing
---

# Local Testing & Development Guide

Use `TEST_MODE=true` to run the application locally on `http://127.0.0.1:8000`. This bypasses external OAuth and automatically authenticates as `test@authenix.ai`.

---

## 1. Environment & Prerequisites

- **Python Version**: Python 3.12+ (matches production Docker runtime).
- **Virtual Environment**: Pre-configured at the **repository root**: `.venv/` (do not use macOS system Python).

```bash
# Verify environment from repo root
.venv/bin/python --version
```

---

## 2. Start the Local Server

Execute from repository root:

```bash
source .venv/bin/activate
cd satoricheck
TEST_MODE=true python -m backend.server
```

App will be available at: `http://127.0.0.1:8000`

---

## 3. Extension Testing (Side Panel)

To test the Chrome/Brave extension against localhost:

1. **Set API Base URL** in `satoricheck/extension/lib/api.js`:
   ```javascript
   const API_BASE = 'http://127.0.0.1:8000/api';
   // const API_BASE = 'https://authenix.ai/api';
   ```
2. **Reload Extension**: Open `chrome://extensions` or `brave://extensions` and click the reload icon on Authenix.
3. **Auto-Session Detection**: Because `TEST_MODE=true` creates an active cookie session on localhost, the extension sidebar syncs automatically.
4. **Revert before commit**: Ensure `API_BASE` is restored to `https://authenix.ai/api`.

---

## 4. Test User & Token Management

`test@authenix.ai` is automatically created on first request in `TEST_MODE`. To top up test credits:

```bash
source .venv/bin/activate
cd satoricheck
python manage.py add-tokens test@authenix.ai 10000
```

---

## 5. Automated Test Suites

From the `satoricheck/` directory:

```bash
# 1. Fast Unit & Integration Suite (No browser required)
../.venv/bin/pytest tests/ --ignore=tests/e2e

# 2. Playwright E2E Suite (Requires server running on port 8000)
../.venv/bin/pytest tests/e2e/
```

---

## 6. Test Identity Reference

| Email | Context | Domain Rules |
|---|---|---|
| `test@authenix.ai` | `TEST_MODE=true` local development only | `@authenix.ai` (owned domain) |
| `test@example.com` | Backend pytest unit fixtures (`:memory:` SQLite) | `@example.com` ([RFC 2606](https://www.rfc-editor.org/rfc/rfc2606) reserved) |
| `e2e-test@example.com` | Playwright E2E browser runs | `@example.com` ([RFC 2606](https://www.rfc-editor.org/rfc/rfc2606) reserved) |

> ⚠️ **Never use `@test.com`, `@fake.com`, or unreserved domains in tests.**
