# SatoriCheck

Fully vibecoded, AI-powered fact-checking app with live audio transcription, Meta Analysis for quote claims, Pitch Deck fact verification, media ai/authenticity analysis  and Stripe billing, using antigravity IDE.

**Status:** In Production, Active Users.
**Mission:** Provide instant, credible verification of claims using advanced AI and source grounding.

## Features

- Live audio with Standard (browser-based speech recognition) or Pro (Deepgram transcription)
- Pitch Deck Check — Verify claims in startup pitch decks (Gemini)
- AI Fact-Checking — Google Gemini with web search grounding and Grok.
- Media Authenticity — Analysis of images and videos for AI generation and manipulation
- Smart Agent — Auto-separates multiple claims for individual verification and meta level analysis.
- Social Sharing — Generate shareable cards for verified claims for X & Linkedin (Privacy-first)
- Streak System — Daily login rewards with CP bonuses
- Token Billing — Stripe integration for Check Points (CP)
- Google OAuth — One-click sign-in.
- Account Deletion — GDPR-compliant data erasure with anti-abuse protection.
- Browser Extension — Universal highlighting and fact-checking integration for Chrome via API tokens.


## Quick Start

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

## Configuration

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

## Architecture

```mermaid
flowchart TB
 subgraph Client["Frontend Client"]
        Browser["User Browser<br>Vanilla JS / HTML5"]
        Extension["Browser Extension<br>Manifest V3"]
  end
 subgraph API["API Routes"]
        AuthBP["Auth BP"]
        BillingBP["Billing BP"]
        FactBP["FactCheck BP"]
        LiveBP["Live Pro BP<br>WebSocket"]
        PitchBP["PitchDeck BP"]
        MediaBP["Media BP"]
  end
 subgraph Logic["Business Logic Layer"]
    direction TB
        GrokSvc["Grok Service"]
        DeepgramSvc["Deepgram Service"]
        PitchSvc["PitchDeck Service"]
        GeminiSvc["Gemini Service<br/>(Agentic Loop)"]
        AgentLoop["🤖 Agent Decision:<br/>Tool Selection<br/>Multi-turn Reasoning"]
        StreakSvc["Streak Service"]
        GeminiSvc -.-> AgentLoop
        AgentLoop -.-> GeminiSvc
  end
 subgraph CloudRun["Cloud Run Container"]
        API
        Logic
        ORM["SQLAlchemy ORM"]
  end
 subgraph Data["Storage"]
        DB[("Cloud SQL<br>PostgreSQL")]
  end
 subgraph GCP["Google Cloud Platform"]
        CloudRun
        Data
  end
 subgraph ExternalServices["External Integrations"]
        OAuth["Google OAuth"]
        StripeAPI["Stripe Payments"]
        GrokAPI["xAI Grok API"]
        DeepgramAPI["Deepgram API"]
        GeminiAPI["Google Gemini API"]
  end
    Browser -- HTTP/REST --> AuthBP & BillingBP & FactBP & PitchBP & MediaBP
    Browser -- WS / WebSocket --> LiveBP
    Extension -- HTTP/REST --> AuthBP & BillingBP & FactBP & MediaBP
    FactBP --> GeminiSvc
    PitchBP --> PitchSvc
    MediaBP --> GeminiSvc
    LiveBP --> DeepgramSvc & GeminiSvc
    BillingBP --> StreakSvc
    PitchSvc -- Verify Claims --> GeminiSvc
    GeminiSvc -. "Agent decides:<br/>Social Context" .-> GrokSvc
    AuthBP -- Verify Token --> OAuth
    BillingBP -- Webhooks --> StripeAPI
    PitchSvc -- "1. Vision + Thinking<br>(Multimodal)" --> GeminiAPI
    MediaBP -- "Forensic Analysis" --> GeminiAPI
    GeminiSvc -- "2. Agentic Verification<br>(Thinking Loop)" --> GeminiAPI
    GrokSvc -- Social Context --> GrokAPI
    DeepgramSvc -- Audio Stream --> DeepgramAPI
    API --> ORM
    Logic --> ORM
    ORM <--> DB

     Browser:::client
     Extension:::client
     AuthBP:::route
     BillingBP:::route
     FactBP:::route
     LiveBP:::route
     PitchBP:::route
     MediaBP:::route
     GrokSvc:::service
     DeepgramSvc:::service
     PitchSvc:::service
     GeminiSvc:::service
     StreakSvc:::service
     ORM:::route
     DB:::db
     OAuth:::external
     StripeAPI:::external
     GrokAPI:::external
     DeepgramAPI:::external
     GeminiAPI:::external
    classDef client fill:#e0f2fe,stroke:#0284c7,stroke-width:2px
    classDef container fill:#f8fafc,stroke:#64748b,stroke-width:2px
    classDef route fill:#ffedd5,stroke:#f97316,stroke-width:2px
    classDef service fill:#ffe4e6,stroke:#e11d48,stroke-width:2px
    classDef db fill:#dcfce7,stroke:#16a34a,stroke-width:2px
    classDef external fill:#f3e8ff,stroke:#9333ea,stroke-width:2px,stroke-dasharray: 5 5
```


## Deployment

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

## Legal

- Privacy Policy: [View on GitHub Gist](https://gist.github.com/defluff/bccc4d328f850de6eec1521ba4c2be22)
- Terms of Service: [View on GitHub Gist](https://gist.github.com/defluff/bccc4d328f850de6eec1521ba4c2be22)

Data handling:
- User data stored in PostgreSQL
- Audio processed by Deepgram (not stored)
- Text and Media processed by Google Gemini
- Payments via Stripe (PCI-DSS compliant)
- Account deletion removes all PII; SHA256 hash retained for anti-abuse

## License

**All Rights Reserved.**

This project is source-available for educational and portfolio purposes only.
See [LICENSE](LICENSE) for details.

---

Built with ☕️ in Switzerland by Andreas
