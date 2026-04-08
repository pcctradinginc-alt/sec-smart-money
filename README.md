# SEC 13F Smart Money Analyzer

Automated quarterly analysis of 13F filings from 13 top institutional investors.
Runs automatically on **16 May / 16 Aug / 16 Nov / 16 Feb** (next business day if weekend).
Delivers a **Top 5 stock picks + specific option trades** report via Gmail.

---

## Monitored Institutions

| Institution | CIK |
|---|---|
| Situational Awareness LP | 0002045724 |
| Yale University | 0000938582 |
| Gates Foundation Trust | 0001166559 |
| Harvard Management Co | 0001082621 |
| Brown University | 0001664741 |
| Duke University | 0001439873 |
| TCI Fund (Chris Hohn) | 0001649339 |
| Pershing Square (Ackman) | 0001336528 |
| Tiger Global (Coleman) | 0001167483 |
| Coatue (Laffont) | 0001766502 |
| D1 Capital (Sundheim) | 0001747057 |
| Viking Global (Halvorsen) | 0001103804 |
| AQR Capital (Asness) | 0001167557 |

---

## Pipeline

```
GitHub Actions Trigger (14:00 UTC, 14th-20th of target months)
    │
    ├─ date_check.py        → Is today the right run date?
    ├─ fetch_filings.py     → SEC EDGAR: fetch 13F XMLs for all 13 CIKs
    │                          + CUSIP→Ticker mapping (OpenFIGI)
    │                          + Stock split detection (yfinance)
    ├─ parse_13f.py         → Delta vs. prior quarter, portfolio weights
    ├─ scoring.py           → Conviction scores, clustering detection
    ├─ analyze_claude_round1.py → Claude: Top 5 stocks + investment theses
    ├─ options_lookup.py    → Tradier: Real option chains for Top 5
    ├─ analyze_claude_round2.py → Claude: Select best specific option per stock
    └─ send_report.py       → HTML report → Gmail
```

---

## Setup

### 1. Fork / Clone this repo

```bash
git clone https://github.com/YOUR_USERNAME/sec-smart-money.git
cd sec-smart-money
```

### 2. Add GitHub Secrets

Go to **Settings → Secrets and variables → Actions → New repository secret**:

| Secret Name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic API key |
| `TRADIER_API_KEY` | Your Tradier API key (live or sandbox) |
| `GMAIL_ADDRESS` | Your Gmail address (e.g. `you@gmail.com`) |
| `GMAIL_APP_PASSWORD` | Gmail App Password (not your login password) |

**How to create a Gmail App Password:**
1. Go to [myaccount.google.com/security](https://myaccount.google.com/security)
2. Enable 2-Factor Authentication if not already
3. Search for "App passwords"
4. Create a new app password → select "Mail" and "Other (custom)"
5. Copy the 16-character password into the `GMAIL_APP_PASSWORD` secret

**Tradier API:**
- Live account: use `https://api.tradier.com/v1` (default in config.py)
- Paper account: change `TRADIER_BASE_URL` in `src/config.py` to `https://sandbox.tradier.com/v1`

### 3. Configure Tradier Account Type

Edit `src/config.py`:
```python
TRADIER_BASE_URL = "https://api.tradier.com/v1"   # Live (default)
# TRADIER_BASE_URL = "https://sandbox.tradier.com/v1"  # Paper
```

### 4. Test a Manual Run

Go to **Actions → SEC 13F Quarterly Analysis → Run workflow**
Set `force_run = true` to bypass the date check.

---

## Local Development

```bash
pip install -r requirements.txt

# Set environment variables
export ANTHROPIC_API_KEY="your_key"
export TRADIER_API_KEY="your_key"
export GMAIL_ADDRESS="you@gmail.com"
export GMAIL_APP_PASSWORD="your_app_password"

# Run individual steps
python src/fetch_filings.py
python src/parse_13f.py
python src/scoring.py
python src/analyze_claude_round1.py
python src/options_lookup.py
python src/analyze_claude_round2.py
python src/send_report.py
```

---

## Data Architecture

```
data/holdings/
  YYYY-MM-DD_raw_holdings.json      ← Raw SEC XML data + CUSIP mapping
  YYYY-MM-DD_holdings_parsed.json   ← Delta-enriched positions
  YYYY-MM-DD_scores.json            ← Conviction scores
  YYYY-MM-DD_claude_round1.json     ← Top 5 stocks from Claude
  YYYY-MM-DD_options.json           ← Tradier options data
  YYYY-MM-DD_final_analysis.json    ← Combined analysis
reports/
  YYYY-MM-DD_report.html            ← Final HTML report (committed to repo)
```

---

## Known Limitations (by design)

| Limitation | Impact | Mitigation |
|---|---|---|
| 13F data is 45 days old | Positions may have changed | Use as idea generator only |
| Long-only AUM denominator | Portfolio weights overstated | Disclaimer in report |
| No shorts/hedges visible | Incomplete picture | Noted in report |
| Stock splits adjusted | High accuracy, not perfect | yfinance split check |
| Options prices at analysis time | Change constantly | Always verify before trading |

---

## Conviction Score Formula

```
Raw Score = (0.40 × portfolio_weight%) + (0.30 × |delta%|)
Cluster Bonus: ×1.5 if 3+ funds buy same ticker
Normalized: min-max scaled to [0, 100]
```

**Flags:**
- `HIGH_CONVICTION`: New position ≥3% of portfolio
- `NEW_POSITION`: Not in prior quarter's filing
- `AGGRESSIVE_ADD`: Position increased >20% by share count
- `CLUSTER`: 3+ monitored funds buying same ticker simultaneously
- `TOP10_ENTRY`: New position entered directly in top 10 holdings

---

## Disclaimer

This tool is for educational and informational purposes only. 13F data is publicly
available but delayed. Nothing in this repository constitutes investment advice.
Options trading involves significant risk of loss. Always conduct your own research
before making any investment decisions.
