"""
options_lookup.py
Fetches real options chains from Tradier for the Top 5 tickers.

Fixes from architecture audit:
  R-07  Greeks = null pre-market → fallback to expiry/volume/OI filter
  R-12  Ticker normalization (BRK.B → BRK/B for Tradier)
  R-14  Empty results handled with fallback logic
"""

import json
import os
from datetime import date, timedelta

import requests

from config import (
    DATA_DIR, OPTION_DELTA_MAX, OPTION_DELTA_MIN,
    OPTION_MAX_DAYS, OPTION_MIN_DAYS, OPTION_MIN_VOLUME,
    TRADIER_BASE_URL,
)


def get_headers() -> dict:
    api_key = os.environ.get("TRADIER_API_KEY", "")
    if not api_key:
        raise ValueError("TRADIER_API_KEY environment variable not set")
    return {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }


def normalize_ticker_for_tradier(ticker: str) -> str:
    """
    R-12 Fix: Tradier uses BRK/B format (slash), not BRK.B (dot).
    Also handles common edge cases.
    """
    return ticker.strip().upper().replace(".", "/")


def get_expiration_dates(ticker: str, headers: dict) -> list[str]:
    """
    Fetches available option expiration dates for a ticker.
    Filters to those within OPTION_MIN_DAYS to OPTION_MAX_DAYS from today.
    """
    url = f"{TRADIER_BASE_URL}/markets/options/expirations"
    params = {"symbol": ticker, "includeAllRoots": "true", "strikes": "false"}

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"    ⚠️  Expiration fetch failed for {ticker}: {e}")
        return []

    dates = data.get("expirations", {})
    if not dates:
        return []

    date_list = dates.get("date", [])
    if isinstance(date_list, str):
        date_list = [date_list]

    today    = date.today()
    min_date = today + timedelta(days=OPTION_MIN_DAYS)
    max_date = today + timedelta(days=OPTION_MAX_DAYS)

    valid = [
        d for d in date_list
        if min_date <= date.fromisoformat(d) <= max_date
    ]
    return valid


def get_option_chain(ticker: str, expiry: str, headers: dict) -> list[dict]:
    """
    Fetches the full option chain for a given ticker and expiry date.
    Includes greeks if available (market hours), falls back to volume/OI filter.
    """
    url = f"{TRADIER_BASE_URL}/markets/options/chains"
    params = {
        "symbol":     ticker,
        "expiration": expiry,
        "greeks":     "true",
    }

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"    ⚠️  Chain fetch failed for {ticker} {expiry}: {e}")
        return []

    options = data.get("options", {})
    if not options:
        return []

    chain = options.get("option", [])
    if isinstance(chain, dict):
        chain = [chain]

    return chain


def filter_options(chain: list[dict], direction: str = "BULLISH") -> list[dict]:
    """
    Filters the option chain to candidates matching our criteria.

    R-07 Fix: If greeks are unavailable (pre-market, None values),
    falls back to volume + open interest filter only.

    direction: "BULLISH" → CALLs, "BEARISH" → PUTs
    """
    option_type = "call" if direction == "BULLISH" else "put"

    candidates = []
    greeks_available = False

    for opt in chain:
        if opt.get("option_type", "").lower() != option_type:
            continue

        volume = opt.get("volume", 0) or 0
        oi     = opt.get("open_interest", 0) or 0

        if volume < OPTION_MIN_VOLUME and oi < 500:
            continue

        # Try to use greeks for delta filter
        greeks = opt.get("greeks") or {}
        delta  = greeks.get("delta")

        if delta is not None:
            greeks_available = True
            if not (OPTION_DELTA_MIN <= abs(float(delta)) <= OPTION_DELTA_MAX):
                continue

        candidates.append({
            "symbol":         opt.get("symbol"),
            "option_type":    opt.get("option_type"),
            "strike":         opt.get("strike"),
            "expiration_date":opt.get("expiration_date"),
            "bid":            opt.get("bid"),
            "ask":            opt.get("ask"),
            "last":           opt.get("last"),
            "volume":         volume,
            "open_interest":  oi,
            "implied_volatility": greeks.get("smv_vol") or opt.get("greeks", {}).get("mid_iv"),
            "delta":          delta,
            "gamma":          greeks.get("gamma"),
            "theta":          greeks.get("theta"),
            "greeks_available": greeks_available,
        })

    if not greeks_available:
        print(f"    ⚠️  Greeks unavailable (pre-market?). Using volume/OI filter only.")

    # Sort by volume descending, return top 5 per expiry
    candidates.sort(key=lambda x: x["volume"] or 0, reverse=True)
    return candidates[:5]


def fetch_options_for_ticker(ticker: str, direction: str, headers: dict) -> dict:
    """
    Full options lookup for one ticker across all valid expiry dates.
    R-14 Fix: Returns empty structure with explanation if no options found.
    """
    tradier_ticker = normalize_ticker_for_tradier(ticker)
    print(f"  📈 {ticker} ({tradier_ticker}) – direction: {direction}")

    expiries = get_expiration_dates(tradier_ticker, headers)
    if not expiries:
        print(f"    ⚠️  No valid expiry dates found for {ticker}")
        return {
            "ticker":    ticker,
            "direction": direction,
            "error":     "no_valid_expiries",
            "options":   [],
        }

    print(f"    Valid expiries ({OPTION_MIN_DAYS}-{OPTION_MAX_DAYS} days): {expiries}")

    all_options = []
    for expiry in expiries:
        chain   = get_option_chain(tradier_ticker, expiry, headers)
        filtered = filter_options(chain, direction=direction)
        all_options.extend(filtered)
        print(f"    {expiry}: {len(chain)} total options → {len(filtered)} after filter")

    if not all_options:
        print(f"    ⚠️  No options passed filters for {ticker}. Relaxing volume threshold.")
        # R-14 Fallback: relax volume filter
        for expiry in expiries[:2]:  # try first 2 expiries
            chain = get_option_chain(tradier_ticker, expiry, headers)
            for opt in chain:
                if opt.get("option_type", "").lower() == ("call" if direction == "BULLISH" else "put"):
                    all_options.append({
                        "symbol":          opt.get("symbol"),
                        "option_type":     opt.get("option_type"),
                        "strike":          opt.get("strike"),
                        "expiration_date": opt.get("expiration_date"),
                        "bid":             opt.get("bid"),
                        "ask":             opt.get("ask"),
                        "last":            opt.get("last"),
                        "volume":          opt.get("volume", 0),
                        "open_interest":   opt.get("open_interest", 0),
                        "implied_volatility": None,
                        "delta":           None,
                        "greeks_available": False,
                        "note":            "fallback_relaxed_filter",
                    })
            if all_options:
                break

    # Sort all options by volume
    all_options.sort(key=lambda x: x.get("volume") or 0, reverse=True)

    return {
        "ticker":    ticker,
        "direction": direction,
        "expiries_checked": expiries,
        "options":   all_options[:10],  # top 10 across all expiries for Claude
    }


def run():
    today_str = date.today().isoformat()

    print(f"\n{'='*60}")
    print(f"Tradier Options Lookup – {today_str}")
    print(f"{'='*60}")

    # Load Claude Round 1 results
    r1_path = DATA_DIR / f"{today_str}_claude_round1.json"
    if not r1_path.exists():
        raise FileNotFoundError(f"Claude Round 1 results not found: {r1_path}")

    with open(r1_path) as f:
        r1 = json.load(f)

    headers = get_headers()
    top5    = r1.get("top5", [])

    if not top5:
        raise ValueError("Claude Round 1 returned no top5 picks")

    results = {}
    for stock in top5:
        ticker    = stock["ticker"]
        direction = stock.get("direction", "BULLISH")
        result    = fetch_options_for_ticker(ticker, direction, headers)
        results[ticker] = result

    output = {
        "date":         today_str,
        "top5_tickers": [s["ticker"] for s in top5],
        "options":      results,
    }

    output_path = DATA_DIR / f"{today_str}_options.json"
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print(f"\n✅ Options data saved to {output_path}")
    for ticker, data in results.items():
        count = len(data.get("options", []))
        print(f"   {ticker}: {count} option candidates")


if __name__ == "__main__":
    run()
