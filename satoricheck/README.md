# SatoriCheck

AI-powered fact-checking app with live audio transcription, gamification, and Stripe billing.

## Features

- 🎙️ **Live Audio** - Real-time speech-to-text transcription
- 🤖 **AI Fact-Checking** - Google Gemini with web search grounding
- ⚡ **Smart Agent** - Auto-separates multiple claims for individual verification
- 🔥 **Streak System** - Daily login rewards with CP bonuses
- � **Token Billing** - Stripe integration for Check Points (CP)
- 🔐 **Google OAuth** - One-click sign-in or email/password

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Edit .env with your API keys

# 3. Run server
python3 -m backend.server
```

Visit **http://127.0.0.1:8000**

## Configuration

Required in `.env`:
- `GEMINI_API_KEY` - Get from [Google AI Studio](https://makersuite.google.com/app/apikey)
- `STRIPE_SECRET_KEY` / `STRIPE_PUBLISHABLE_KEY` - From [Stripe Dashboard](https://dashboard.stripe.com/apikeys)
- `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` - For OAuth
- `FLASK_SECRET_KEY` - Random secure string
- `DATABASE_URL` - PostgreSQL or SQLite connection string

## Tech Stack

**Backend:** Flask, SQLAlchemy, PostgreSQL, Stripe, Google Gemini  
**Frontend:** Vanilla JS, Web Speech API, Modern CSS  
**Auth:** JWT cookies (web) + Bearer tokens (Chrome extension)



## Deployment

Set in production `.env`:
```env
TEST_MODE=false
DATABASE_URL=postgresql://...
ENV=production
```

Configure Stripe webhook: `/api/billing/webhook`

## License

Proprietary - Built in Switzerland 🇨🇭

---

Built with ❤️ by Andreas 
