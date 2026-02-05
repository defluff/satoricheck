# SatoriCheck

Fully vibecoded, AI-powered fact-checking app with live audio transcription, Meta Analysis for quote claims, Pitch Deck verification, and Stripe billing, using antigravity IDE.

**Status:** In Production, Active Users.
**Mission:** Provide instant, credible verification of claims using advanced AI and source grounding.

## ✨ Features

- 🎤 **Standard Mode** — Free browser-based speech recognition
- 🎙️ **Live Pro** — Premium Deepgram transcription (1 CP/minute)
- 📊 **Pitch Deck Check** — Verify claims in startup pitch decks (Gemini 3 Pro)
- 🤖 **AI Fact-Checking** — Google Gemini with web search grounding
- 🧠 **Smart Agent** — Auto-separates multiple claims for individual verification
- 🔍 **Meta Analysis** — Distinguishes between "X said Y" (quote) and whether Y is actually true
- 🃏 **Social Sharing** — Generate shareable cards for verified claims for X & Linkedin (Privacy-first)
- 🔥 **Streak System** — Daily login rewards with CP bonuses
- ⚡ **Token Billing** — Stripe integration for Check Points (CP)
- 🔐 **Google OAuth** — One-click sign-in
- 🗑️ **Account Deletion** — GDPR-compliant data erasure with anti-abuse protection

## 🚀 Quick Start

```bash
# 1. Enter project directory
cd satoricheck

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
# Edit .env with your API keys

# 4. Run server
python3 -m backend.server
```

Visit **http://127.0.0.1:8000**

## ⚙️ Configuration

### Required Environment Variables

| Variable | Description | Get From |
|----------|-------------|----------|
| `GEMINI_API_KEY` | Google Gemini API key | [Google AI Studio](https://makersuite.google.com/app/apikey) |
| `FLASK_SECRET_KEY` | Random secure string for sessions | Generate with `python -c "import secrets; print(secrets.token_hex(32))"` |
| `DATABASE_URL` | Database connection string | SQLite for dev, PostgreSQL for prod |

### Optional (Production)

Check `.env.example` for full configuration options including Stripe, Deepgram, and Google OAuth.



### Test Mode

Set `TEST_MODE=true` to skip API key validation and use a test user.

## 🏗️ Architecture

<img width="4741" height="6516" alt="Gemini API Services Flow-2026-02-04-235943" src="https://github.com/user-attachments/assets/ad3fe0dc-0828-464d-b367-4ee54d23ab8b" />


## 🚢 Deployment

### Cloud Run / Heroku

```bash
# Procfile is included for container deployment
web: gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 0 backend.server:app
```

### Production `.env`

```env
TEST_MODE=false
ENV=production
DATABASE_URL=postgresql://user:pass@host:5432/satoricheck
```

### Stripe Webhook

Configure webhook endpoint in your Stripe Dashboard to point to your deployed backend.

Events to listen for:
- `checkout.session.completed`
- `invoice.payment_succeeded`

## 📜 Legal

- Privacy Policy: [View on GitHub Gist](https://gist.github.com/defluff/bccc4d328f850de6eec1521ba4c2be22)
- Terms of Service: [View on GitHub Gist](https://gist.github.com/defluff/bccc4d328f850de6eec1521ba4c2be22)

Data handling:
- User data stored in PostgreSQL
- Audio processed by Deepgram (not stored)
- Text processed by Google Gemini
- Payments via Stripe (PCI-DSS compliant)
- Account deletion removes all PII; SHA256 hash retained for anti-abuse

## 📝 License

**All Rights Reserved.**

This project is source-available for educational and portfolio purposes only.
See [LICENSE](LICENSE) for details.

---

Built with ❤️ in Switzerland by Andreas, 100% vibecoded using antigravity IDE
