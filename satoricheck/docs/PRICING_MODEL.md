# SatoriCheck Token Economics & Pricing Model

**Document Version:** 1.0  
**Last Updated:** 2025-12-19  
**Classification:** Internal Business Documentation

---

## Executive Summary

SatoriCheck operates on a **Check Point (CP)** token economy. Users purchase CP which are consumed when using the app's core features: fact-checking transcribed content and Live Pro transcription.

**Key Metrics:**
- Text Mode: 1 CP = 1,250 words fact-checked
- Live Pro Mode: 1 CP = 1 minute of premium transcription
- Gross margins: 83-98% depending on usage pattern

---

## 1. Token Package Pricing

| Package | CP Amount | Price (CHF) | CP per CHF | Target Segment |
|---------|-----------|-------------|------------|----------------|
| **Small Battery** | 86 | CHF 4.50 | 19.1 | Trial users |
| **Medium Battery** | 486 | CHF 24.00 | 20.3 | Regular users |
| **Large Battery** | 2,222 | CHF 99.00 | 22.4 | Power users |
| **Wizard Plantation** | 60,000 (lifetime) | CHF 890 | 67.4 | Enterprise |

*Wizard: One-time payment, 1,000 CP/month × 60 months (5 years)*

---

## 2. CP Consumption Rates

### Text Mode (Manual/Browser Transcription)
| Metric | Value |
|--------|-------|
| Words per CP | 1,250 |
| Approximate speech time per CP | ~8 minutes |
| Fact-check cost | ~1 CP per 8-10 min audio |

### Live Pro Mode (Deepgram Premium)
| Metric | Value |
|--------|-------|
| Duration per CP | 1 minute |
| Audio sources | Any device (virtual cables, external mics) |
| Languages | 40+ supported |

---

## 3. Cost Structure Analysis

### API Costs (Per Unit)

| Service | Cost per Unit | Unit |
|---------|---------------|------|
| **Gemini API** | ~CHF 0.001 | Per 1,250 words |
| **Deepgram Nova-2** | ~CHF 0.0077 | Per minute |
| **Combined (Live Pro + Check)** | ~CHF 0.0087 | Per minute |

### Revenue per CP

| Package | Price | CP | Revenue/CP |
|---------|-------|----|-----------:|
| Small | CHF 4.50 | 86 | CHF 0.052 |
| Medium | CHF 24.00 | 486 | CHF 0.049 |
| Large | CHF 99.00 | 2,222 | CHF 0.045 |
| Wizard | CHF 890 | 60,000 | CHF 0.015 |

---

## 4. User Scenario Margin Analysis

### Scenario A: Manual Checker (Text Only)
**Profile:** User types/pastes text, uses manual "Check Selection" tool

| Metric | Value |
|--------|-------|
| Transcription Cost | CHF 0 (free browser/manual input) |
| Gemini Cost per 1,250 words | CHF 0.001 |
| Revenue per CP | CHF 0.052 |
| **Gross Margin** | **~98%** |

**Example Usage:**
- Medium Package (486 CP, CHF 24)
- Words fact-checked: 486 × 1,250 = 607,500 words
- Our cost: 607,500 × (0.001/1250) = CHF 0.49
- **Profit: CHF 23.51**

---

### Scenario B: Audio User with Smart Agent
**Profile:** Uses browser transcription (free), auto-check with Smart Agent enabled

| Metric | Value |
|--------|-------|
| Transcription Cost | CHF 0 (browser SpeechRecognition) |
| Gemini Cost per 1,250 words | CHF 0.001 |
| Auto-check frequency | Every ~250-300 words |
| Revenue per CP | CHF 0.052 |
| **Gross Margin** | **~98%** |

**Example Usage:**
- Medium Package (486 CP, CHF 24)
- ~486 automatic checks triggered
- Our cost: CHF 0.49
- **Profit: CHF 23.51**

*Note: Browser transcription is FREE (runs locally in user's browser)*

---

### Scenario C: Live Pro + Manual Checking
**Profile:** Uses Live Pro for transcription, manually selects text to verify

| Metric | Value |
|--------|-------|
| Deepgram Cost | CHF 0.0077/min |
| Gemini Cost (occasional) | CHF 0.001/check |
| Revenue per CP | CHF 0.052 |
| Live Pro rate | 1 CP/minute |
| **Gross Margin** | **~83-85%** |

**Example Usage:**
- Large Package (2,222 CP, CHF 99)
- Usage: 2,000 CP on Live Pro (33.3 hours) + 222 CP on text checks
- Deepgram cost: 2,000 × CHF 0.0077 = CHF 15.40
- Gemini cost: 222 × CHF 0.001 = CHF 0.22
- Total cost: CHF 15.62
- **Profit: CHF 83.38 (84% margin)**

---

### Scenario D: Power User (Live Pro + Auto-Check)
**Profile:** Runs Live Pro continuously with Smart Agent auto-checking everything

| Metric | Value |
|--------|-------|
| Deepgram Cost | CHF 0.0077/min |
| Gemini Cost | CHF 0.001/check (frequent) |
| Revenue per CP | CHF 0.052 |
| Combined CP burn rate | ~1.5-2 CP/minute (Live Pro + text checks) |
| **Gross Margin** | **~78-83%** |

**Example Usage:**
- Large Package (2,222 CP, CHF 99)
- Heavy user: 1,500 CP on Live Pro + 722 CP on auto-checks
- Deepgram cost: 1,500 × CHF 0.0077 = CHF 11.55
- Gemini cost: 722 × 0.001 = CHF 0.72
- Total cost: CHF 12.27
- **Profit: CHF 86.73 (88% margin)**

*Note: Even heavy users remain highly profitable*

---

## 5. Margin Summary by User Type

| User Type | Primary Features Used | Margin | Annual Revenue per Active User (est.) |
|-----------|----------------------|--------|--------------------------------------|
| **Manual Checker** | Text input, Verify Selection | 98% | CHF 24-99 |
| **Audio User** | Browser transcription, Smart Agent | 98% | CHF 24-99 |
| **Live Pro Manual** | Live Pro + Manual checking | 83-85% | CHF 99-890 |
| **Power User** | Live Pro + Auto-check | 78-83% | CHF 99-890 |

---

## 6. Package Duration Estimates

### Text Mode Only (1 CP = 1,250 words ≈ 8 min speech)

| Package | CP | Estimated Duration |
|---------|----|--------------------|
| Small | 86 | ~11 hours of content |
| Medium | 486 | ~65 hours of content |
| Large | 2,222 | ~296 hours of content |
| Wizard | 1,000/month | ~133 hours/month |

### Live Pro Mode (1 CP = 1 minute)

| Package | CP | Duration |
|---------|----|---------:|
| Small | 86 | 1.4 hours |
| Medium | 486 | 8.1 hours |
| Large | 2,222 | 37 hours |
| Wizard | 1,000/month | 16.7 hours/month |

---

## 7. Break-Even Analysis

**Minimum CP purchase to cover API costs:**

| Mode | Cost per CP | Revenue per CP (Small) | Break-even |
|------|-------------|------------------------|------------|
| Text | CHF 0.0008 | CHF 0.052 | 65x margin |
| Live Pro | CHF 0.0087 | CHF 0.052 | 6x margin |

**Conclusion:** Even at lowest-margin usage (100% Live Pro), we maintain 6x our costs in revenue.

---

## 8. Competitive Advantages

### Live Pro Value Proposition
| Feature | Standard (Free) | Live Pro (1 CP/min) |
|---------|-----------------|---------------------|
| Audio Sources | System default only | Any device, virtual cables |
| Accuracy | Basic browser | Deepgram Nova-2 premium |
| Languages | ~5-10 | 40+ |
| Latency | Variable | Optimized streaming |
| Reliability | Browser-dependent | Cloud-based |

### vs. Competitors
| Service | Price | What You Get |
|---------|-------|--------------|
| Otter.ai | $8.33/mo | 1,200 min transcription only |
| SatoriCheck | CHF 4.50 (one-time) | 86 CP + fact-checking + AI analysis |

---

## 9. Key Business Metrics

| Metric | Value | Notes |
|--------|-------|-------|
| Average Margin | ~90% | Blended across use cases |
| Lowest Margin | ~78% | Power user (Live Pro + Auto-check) |
| Highest Margin | ~98% | Text-only user |
| Free Tier | 5 CP signup bonus | Acquisition cost |
| Wizard LTV | CHF 890 | 5-year commitment |

---

## 10. Growth Trajectory & Revenue Projections
### Revenue Estimates (One-Time Acquisition Model)
*Baseline assumption: Users purchase a single package (blended average CHF 15.00) and churn. No recurring revenue is modeled for the initial launch phase.*

| Total Users | Total Revenue | Est. Gross Profit (90%) |
|-------------|---------------|-------------------------|
| 1,000       | CHF 15,000    | CHF 13,500              |
| 10,000      | CHF 150,000   | CHF 135,000             |
| 50,000      | CHF 750,000   | CHF 675,000             |
| 100,000     | CHF 1,500,000 | CHF 1,350,000           |

### Retention & Expansion Strategy
To transition from a "Try and Churn" model to sustainable growth, the following roadmap is planned:
- **Professional Subscription (Monthly):** CHF 39/month for unlimited Text Mode + 500 Live Pro minutes. Designed for journalists and researchers who require predictable monthly overhead and high-volume verification.
- **Business License (Team Tier):** CHF 149/month for up to 5 seats with a shared pool of 3,000 CP. Includes centralized management, team collaboration tools, and audit logs for legal and corporate compliance.
- **Seamless Integration (Chrome Extension):** Moving SatoriCheck from a destination site to a background utility. By integrating directly into browsers, we reduce friction and increase the adoption of the "Smart Refill" model, effectively converting one-time users into recurring customers.

### Margin Scenarios
1.  **High Margin (98%):** Browser-based users and manual text checkers. Minimal API overhead.
2.  **Target Margin (85-90%):** Standard mix of Live Pro and browser transcription.
3.  **Low Margin (78%):** Power users utilizing continuous Live Pro + Smart Agent auto-checking.

### Churn Risk & Mitigation: "Smart Refill"
**Risk:** Pure pay-per-usage models face high churn at the "empty battery" state due to manual checkout friction.

**Mitigation (Optional Recurring Model):**
To stabilize MRR (Monthly Recurring Revenue) and reduce churn, we offer **Smart Refill**:
- **Trigger:** When CP balance drops below 20 CP.
- **Action:** Automatically purchases a **Medium Package** (486 CP / CHF 24).
- **Benefit:** Combines the flexibility of usage-based pricing with the seamless experience of a subscription, ensuring the user is never interrupted during a live session.

---

## Appendix: Technical Implementation

### Token Deduction Logic

```
Text Mode:
- Words accumulated in unbilled_words counter
- Every 1,250 words → 1 CP deducted

Live Pro Mode:
- Heartbeat sent every 10 seconds
- CP deducted every 30 seconds of active session
- System handles abandoned sessions (60s timeout)
```

### Configuration (config.py)
```python
WORDS_PER_CP = 1250
LIVE_PRO_CP_PER_MINUTE = 1
WIZARD_REFILL_AMOUNT = 1000
```

---

*Document prepared for internal business planning and investor communications.*
