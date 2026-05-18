"""
analyze_claude_round1.py
Claude API – Round 1: Identify Top 5 investment candidates.

Uses claude-haiku (CLAUDE_MODEL_R1) – sufficient for screening and
~20× cheaper than Sonnet. Sonnet is reserved for Round 2 (precise
option selection where nuance matters more).

Improvements:
  - CLAUDE_MODEL_R1 (Haiku) instead of Sonnet
  - Multi-quarter build signals included in prompt
  - Price-action staleness flags surfaced to Claude
  - Exponential backoff retry
  - Robust JSON extraction
"""

import json
import os
import re
import time
from datetime import date

import anthropic

from config import (
    CLAUDE_MAX_TOKENS, CLAUDE_MODEL_R1, CLAUDE_RETRY_COUNT,
    CLAUDE_RETRY_DELAY, DATA_DIR,
)


def load_scores(today_str: str) -> dict:
    path = DATA_DIR / f"{today_str}_scores.json"
    if not path.exists():
        raise FileNotFoundError(f"Scores not found: {path}")
    with open(path) as f:
        return json.load(f)


def normalize_ticker(ticker: str) -> str:
    return ticker.strip().upper().replace(".", "/")


def build_prompt(scores: dict) -> str:
    today_str  = scores["date"]
    top20      = scores["top20"]
    clusters   = scores["clusters"]
    mq_signals = scores.get("mq_signals", {})

    positions_text = []
    for i, agg in enumerate(top20, 1):
        filer_summary = "; ".join(
            f"{f['filer']} ({f['delta_type']}, "
            f"Δ{f['delta_pct'] if f['delta_pct'] is not None else 'N/A'}%, "
            f"port_wt={f['port_weight_pct']}%)"
            for f in agg["filers"]
        )

        # Price-action context
        price_chg = agg.get("price_change_since_filing_pct")
        price_note = ""
        if price_chg is not None:
            if "PRICE_ACTION_STALE" in agg.get("flags", []):
                price_note = f" ⚠️ ALREADY +{price_chg:.0f}% SINCE FILING – thesis may be priced in"
            elif "PRICE_ACTION_WARNING" in agg.get("flags", []):
                price_note = f" ⚡ +{price_chg:.0f}% since filing"
            else:
                price_note = f" (+{price_chg:.0f}% since filing)" if price_chg > 0 else f" ({price_chg:.0f}% since filing)"

        # Multi-quarter context
        mq_note = ""
        mq = mq_signals.get(agg["ticker"])
        if mq and mq["build_quarters"] >= 2:
            mq_note = (
                f"\n   Multi-Quarter: {mq['build_quarters']} quarters of building"
                f" | avg delta {mq['avg_delta_pct']}%"
                f" | flags: {', '.join(mq['flags']) or 'none'}"
            )

        positions_text.append(
            f"{i}. {agg['ticker']} ({agg['name']}){price_note}\n"
            f"   Score: {agg['conviction_score']}/100 | Filers: {agg['filer_count']} | "
            f"Flags: {', '.join(agg['flags']) or 'none'}\n"
            f"   Cluster: {'YES – ' + str(agg['cluster_count']) + ' funds' if agg['cluster_count'] >= 3 else 'no'}\n"
            f"   Details: {filer_summary}{mq_note}\n"
        )

    cluster_text = ""
    if clusters:
        cluster_text = "\nCLUSTER SIGNALS (3+ funds buying same ticker):\n"
        for ticker, filers in clusters.items():
            cluster_text += f"  {ticker}: {', '.join(filers)}\n"

    return f"""You are an expert quantitative analyst specializing in 13F filing analysis and institutional investor tracking.

ANALYSIS DATE: {today_str}
DATA SOURCE: SEC 13F filings (latest available, up to 45-day lag)

IMPORTANT DISCLAIMER: 13F data reflects only US long equity positions >$200K.
Portfolio weights use long-only AUM (cash/shorts/bonds excluded → weights are systematically overstated).
Stock splits have been adjusted. Treat this as an idea generator, not a buy signal.

IMPORTANT – PRICE ACTION: Positions marked ⚠️ ALREADY +X% SINCE FILING have potentially
already played out. Strong preference for fresh ideas that have NOT run significantly yet.

TOP 20 CONVICTION SCORES (normalized 0-100):
{''.join(positions_text)}
{cluster_text}

YOUR TASK:
Analyze the above data and identify the TOP 5 stocks with the strongest institutional conviction signals
that have NOT already fully played out in price.

Consider in priority order:
1. Multi-quarter building (3+ quarters of consistent accumulation = strongest signal)
2. Cluster signals (multiple top funds buying simultaneously)
3. Fresh entries that haven't run >15% since the filing date
4. Conviction Score magnitude
5. Quality of the buying funds (university endowments > hedge funds for long-term thesis)
6. Position type flags: HIGH_CONVICTION (>3% of portfolio), NEW_POSITION, AGGRESSIVE_ADD

Explicitly DOWNWEIGHT stocks with PRICE_ACTION_STALE flag – the thesis is likely priced in.

OUTPUT FORMAT (respond ONLY with valid JSON, no markdown, no explanation):
{{
  "analysis_date": "{today_str}",
  "market_context": "2-3 sentence summary of current market environment relevant to these picks",
  "top5": [
    {{
      "rank": 1,
      "ticker": "NORMALIZED_TICKER",
      "company_name": "Full Company Name",
      "conviction_score": 87.5,
      "thesis": "2-3 sentence investment thesis explaining WHY this is a strong signal",
      "key_buyers": ["Fund Name 1", "Fund Name 2"],
      "cluster_signal": true,
      "multi_quarter_build": false,
      "primary_flag": "HIGH_CONVICTION|NEW_POSITION|AGGRESSIVE_ADD|CLUSTER|MULTI_QUARTER_BUILD",
      "risk_factors": "1-2 sentence note on key risks or the 45-day data lag impact",
      "direction": "BULLISH"
    }}
  ],
  "disclaimer": "This analysis is based on delayed 13F data and is for informational purposes only, not investment advice."
}}"""


def call_claude_with_retry(prompt: str) -> str:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    for attempt in range(1, CLAUDE_RETRY_COUNT + 1):
        try:
            response = client.messages.create(
                model=CLAUDE_MODEL_R1,
                max_tokens=CLAUDE_MAX_TOKENS,
                system=(
                    "You are a quantitative analyst. You respond ONLY with valid JSON. "
                    "Never include markdown code fences, preamble, or explanation. "
                    "Your entire response must be parseable by json.loads()."
                ),
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text

        except anthropic.RateLimitError:
            wait = CLAUDE_RETRY_DELAY * (2 ** (attempt - 1))
            print(f"  ⏳ Rate limit hit. Waiting {wait}s (attempt {attempt}/{CLAUDE_RETRY_COUNT})")
            time.sleep(wait)

        except anthropic.APIError as e:
            wait = CLAUDE_RETRY_DELAY * attempt
            print(f"  ⚠️  API error (attempt {attempt}): {e}. Retrying in {wait}s...")
            time.sleep(wait)

    raise RuntimeError(f"Claude API failed after {CLAUDE_RETRY_COUNT} attempts")


def parse_claude_response(raw: str) -> dict:
    cleaned = re.sub(r"```(?:json)?", "", raw).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            return json.loads(match.group())
        raise ValueError(f"Could not parse Claude response as JSON: {e}\n\nRaw: {raw[:500]}")


def run():
    today_str = date.today().isoformat()

    print(f"\n{'='*60}")
    print(f"Claude Round 1 (Haiku) – {today_str}")
    print(f"{'='*60}")
    print(f"  Model: {CLAUDE_MODEL_R1}")

    scores = load_scores(today_str)
    prompt = build_prompt(scores)

    print(f"📤 Sending top {len(scores['top20'])} scored positions to Claude Haiku...")

    raw_response = call_claude_with_retry(prompt)
    result       = parse_claude_response(raw_response)

    for stock in result.get("top5", []):
        stock["ticker"] = normalize_ticker(stock.get("ticker", ""))

    print(f"✅ Claude identified top 5:")
    for stock in result.get("top5", []):
        print(f"   {stock['rank']}. {stock['ticker']} – {stock['thesis'][:60]}...")

    output_path = DATA_DIR / f"{today_str}_claude_round1.json"
    with open(output_path, "w") as f:
        json.dump(result, f, indent=2)

    print(f"💾 Saved to {output_path}")


if __name__ == "__main__":
    run()
