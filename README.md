# SatoriCheck

Fully vibecoded, AI-powered fact-checking app with live audio transcription, Meta Analysis for quote claims, and Stripe billing, using antigravity IDE

## ✨ Features

- 🎤 **Standard Mode** — Free browser-based speech recognition
- 🎙️ **Live Pro** — Premium Deepgram transcription (1 CP/minute)
- 🤖 **AI Fact-Checking** — Google Gemini with web search grounding
- 🧠 **Smart Agent** — Auto-separates multiple claims for individual verification
- 🔍 **Meta Analysis** — Distinguishes between "X said Y" (quote) and whether Y is actually true
- � **Social Sharing** — Generate shareable cards for verified claims for X & Linkedin
- �🔥 **Streak System** — Daily login rewards with CP bonuses
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

| Variable | Description | Get From |
|----------|-------------|----------|
| `DEEPGRAM_API_KEY` | Live Pro transcription | [Deepgram Console](https://console.deepgram.com/) |
| `STRIPE_SECRET_KEY` | Payment processing | [Stripe Dashboard](https://dashboard.stripe.com/apikeys) |
| `STRIPE_PUBLISHABLE_KEY` | Frontend Stripe key | Stripe Dashboard |
| `STRIPE_WEBHOOK_SECRET` | Webhook verification | Stripe Dashboard → Webhooks |
| `GOOGLE_CLIENT_ID` | OAuth login | [Google Cloud Console](https://console.cloud.google.com/) |
| `GOOGLE_CLIENT_SECRET` | OAuth secret | Google Cloud Console |

### Test Mode

Set `TEST_MODE=true` to skip API key validation and use a test user.

## 🏗️ Architecture

```
satoricheck/
├── backend/
│   ├── server.py          # Flask app entry point
│   ├── config.py          # Environment config + pricing
│   ├── models.py          # SQLAlchemy models
│   ├── database.py        # DB connection
│   ├── jwt_utils.py       # Token authentication
│   ├── routes/
│   │   ├── auth.py        # Login, signup, OAuth, delete account
│   │   ├── billing.py     # Stripe checkout + webhooks
│   │   ├── factcheck.py   # AI analysis + Smart Agent
│   │   ├── live_pro.py    # Deepgram session management
│   │   ├── tokens.py      # Balance + streak
│   │   ├── export.py      # CSV export
│   │   └── analytics.py   # Social sharing stats
│   └── services/
│       ├── gemini_service.py    # Fact-checking AI
│       ├── deepgram_service.py  # Audio transcription
│       └── streak.py            # Streak logic
├── frontend/
│   ├── index.html         # Main app
│   ├── css/style.css      # Styling
│   └── js/
│       ├── app.js         # Main controller
│       ├── api.js         # Backend communication
│       ├── auth.js        # Authentication
│       ├── factcheck.js   # Fact-check logic
│       ├── livepro.js     # Live Pro WebSocket
│       ├── audio.js       # Browser speech
│       ├── selection.js   # Text selection
│       ├── share.js       # Social image generation
│       └── ui.js          # UI updates
```

## 💰 Token Economics

| Package | Price (CHF) | Check Points |
|---------|-------------|--------------|
| Small Battery | 5.90 | 86 CP |
| Medium Battery | 24.00 | 486 CP |
| Large Battery | 99.00 | 2,222 CP |
| Wizard | 890.00 | 1,000 CP/month × 5 years |

**Usage:**
- Text fact-checking: 1 CP per ~1,250 words
- Live Pro: 1 CP per minute

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

Configure webhook endpoint: `https://yourdomain.com/api/billing/webhook`

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

Built with ❤️ in Switzerland by Andreas
