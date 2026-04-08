"""
analyze_claude_round1.py
Claude API – Round 1: Identify Top 5 investment candidates.

Fixes from architecture audit:
  R-06  Structured JSON output enforced via system prompt schema
  R-13  Retry with exponential backoff on API failures
  R-12  Ticker normalization before passing to Claude
"""

import json
import os
import re
import time
from datetime import date
from pathlib import Path

import anthropic

from config import (
    CLAUDE_MAX_TOKENS, CLAUDE_MODEL, CLAUDE_RETRY_COUNT,
    CLAUDE_RETRY_DELAY, DATA_DIR,
)


def load_scores(today_str: str) -> dict:
    path = DATA_DIR / f"{today_str}_scores.json"
    if not path.exists():
        raise FileNotFoundError(f"Scores not found: {path}")
    with open(path) as f:
        return json.load(f)


def normalize_ticker(ticker: str) -> str:
    """
    R-12 Fix: Normalize ticker formats.
    BRK.B → BRK/B (Tradier format)
    Handles common edge cases before they reach downstream APIs.
    """
    return ticker.strip().upper().replace(".", "/")


def build_prompt(scores: dict) -> str:
    today_str = scores["date"]
    top20     = scores["top20"]
    clusters  = scores["clusters"]

    # Format top 20 compactly
    positions_text = []
    for i, agg in enumerate(top20, 1):
        filer_summary = "; ".join(
            f"{f['filer']} ({f['delta_type']}, Δ{f['delta_pct']}%, "
            f"port_wt={f['port_weight_pct']}%)"
            for f in agg["filers"]
        )
        positions_text.append(
            f"{i}. {agg['ticker']} ({agg['name']})\n"
            f"   Score: {agg['conviction_score']}/100 | Filers: {agg['filer_count']} | "
            f"Flags: {', '.join(agg['flags']) or 'none'}\n"
            f"   Cluster: {'YES – ' + str(agg['cluster_count']) + ' funds' if agg['cluster_count'] >= 3 else 'no'}\n"
            f"   Details: {filer_summary}\n"
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

TOP 20 CONVICTION SCORES (normalized 0-100):
{''.join(positions_text)}
{cluster_text}

YOUR TASK:
Analyze the above data and identify the TOP 5 stocks that represent the strongest institutional conviction signals.

Consider:
1. Conviction Score magnitude (higher = stronger signal)
2. Cluster signals (multiple top funds buying simultaneously = strongest signal)
3. Quality of the buying funds (university endowments vs. hedge funds = different risk profiles)
4. Position type flags: HIGH_CONVICTION (>3% of portfolio), NEW_POSITION, AGGRESSIVE_ADD
5. Whether the buying pattern suggests a coherent investment thesis

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
      "primary_flag": "HIGH_CONVICTION|NEW_POSITION|AGGRESSIVE_ADD|CLUSTER",
      "risk_factors": "1-2 sentence note on key risks or the 45-day data lag impact",
      "direction": "BULLISH"
    }}
  ],
  "disclaimer": "This analysis is based on delayed 13F data and is for informational purposes only, not investment advice."
}}"""


def call_claude_with_retry(prompt: str) -> str:
    """
    R-13 Fix: Exponential backoff retry on API failures.
    """
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    for attempt in range(1, CLAUDE_RETRY_COUNT + 1):
        try:
            response = client.messages.create(
                model=CLAUDE_MODEL,
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
    """
    R-06 Fix: Robust JSON extraction even if Claude includes extra whitespace.
    Strips any accidental markdown fences.
    """
    # Remove accidental code fences
    cleaned = re.sub(r"```(?:json)?", "", raw).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        # Try to extract JSON object from response
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            return json.loads(match.group())
        raise ValueError(f"Could not parse Claude response as JSON: {e}\n\nRaw: {raw[:500]}")


def run():
    today_str = date.today().isoformat()

    print(f"\n{'='*60}")
    print(f"Claude Analysis Round 1 – {today_str}")
    print(f"{'='*60}")

    scores = load_scores(today_str)
    prompt = build_prompt(scores)

    print(f"📤 Sending top {len(scores['top20'])} scored positions to Claude...")

    raw_response = call_claude_with_retry(prompt)
    result       = parse_claude_response(raw_response)

    # Normalize tickers in result (R-12)
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
