# Vendored PDF.js 3.11.174

Self-hosted copy of [`pdfjs-dist@3.11.174`](https://www.npmjs.com/package/pdfjs-dist/v/3.11.174)
(Apache-2.0, see `LICENSE`).

## Why vendored

Web Workers cannot be loaded with Subresource Integrity (SRI), so `pdf.worker.min.js`
could not be integrity-pinned on a CDN. Serving both files from our own origin removes the
third-party trust dependency and lets CSP `worker-src` stay `'self' blob:`.

## Provenance

Downloaded unmodified from `https://cdn.jsdelivr.net/npm/pdfjs-dist@3.11.174/`:

| File | SHA-384 (base64) |
|---|---|
| `pdf.min.js` | `/1qUCSGwTur9vjf/z9lmu/eCUYbpOTgSjmpbMQZ1/CtX2v/WcAIKqRv+U1DUCG6e` |
| `pdf.worker.min.js` | `SnzOobpRMLXZ52iJvZm/C0fYw0OQemTXzTjIsdsfMcrCtCEe9qgzxTd3RSklO5x2` |

Verify: `openssl dgst -sha384 -binary <file> | openssl base64 -A`

## Upgrading

1. Create a new `pdfjs-<version>/` directory (the version in the path doubles as a cache-buster).
2. Update the two paths in `frontend/js/pitchdeck.js` and `index.html`.
3. Update the hashes above and run `pytest tests/test_frontend_supply_chain.py`.
