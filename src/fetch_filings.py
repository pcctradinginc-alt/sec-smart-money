"""
fetch_filings.py
Fetches the latest 13F-HR filings for all configured filers from SEC EDGAR.

Fixes implemented (from architecture audit):
  R-04  ZeroDivisionError / New positions → handled downstream
  R-05  Amendment detection → checks for 13F-HR/A and flags it
  R-08  Rate limiting → enforces 0.15s sleep between requests
  R-02  CUSIP→Ticker mapping → via OpenFIGI API after parsing
"""

import json
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path

import requests

from config import (
    DATA_DIR, EDGAR_SUBMISSIONS_URL, EDGAR_ARCHIVES_URL,
    FILERS, OPENFIGI_BATCH, OPENFIGI_URL, SEC_HEADERS,
    SEC_RATE_LIMIT_SLEEP,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sleep():
    time.sleep(SEC_RATE_LIMIT_SLEEP)


def edgar_get(url: str) -> requests.Response:
    """GET with SEC-compliant User-Agent and rate limiting."""
    _sleep()
    resp = requests.get(url, headers=SEC_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp


# ── Step 1: Get latest 13F filing accession for a CIK ────────────────────────

def get_latest_13f_filing(cik: str) -> dict | None:
    """
    Returns metadata for the most recent 13F-HR or 13F-HR/A filing.
    Returns None if the filer has no 13F on record.

    Pre-condition:  cik is a zero-padded 10-digit string
    Post-condition: returned dict contains 'accessionNumber', 'filingDate',
                    'form', 'isAmendment'
    """
    url = EDGAR_SUBMISSIONS_URL.format(cik=cik)
    try:
        data = edgar_get(url).json()
    except Exception as e:
        print(f"  ⚠️  Could not fetch submissions for CIK {cik}: {e}")
        return None

    filings = data.get("filings", {}).get("recent", {})
    forms       = filings.get("form", [])
    accessions  = filings.get("accessionNumber", [])
    dates       = filings.get("filingDate", [])
    primary_docs = filings.get("primaryDocument", [])

    # Find the most recent 13F-HR or 13F-HR/A
    for i, form in enumerate(forms):
        if form in ("13F-HR", "13F-HR/A"):
            return {
                "cik":             cik,
                "accessionNumber": accessions[i],
                "filingDate":      dates[i],
                "form":            form,
                "isAmendment":     form == "13F-HR/A",
                "primaryDocument": primary_docs[i] if i < len(primary_docs) else "",
            }

    print(f"  ℹ️  No 13F-HR found for CIK {cik}")
    return None


# ── Step 2: Download the infotable XML ───────────────────────────────────────

def download_infotable(filing_meta: dict) -> str | None:
    """
    Downloads the infotable XML (the actual holdings list) for a filing.

    The primary document is often the cover page. We search the index
    for the file with 'informationtable' in the name.

    Pre-condition:  filing_meta has valid accessionNumber and cik
    Post-condition: returns raw XML string or None on failure
    """
    cik_int    = int(filing_meta["cik"])
    accession  = filing_meta["accessionNumber"]
    acc_dashes = accession.replace("-", "")

    # Fetch the index to find the infotable filename
    # SEC correct format: /Archives/edgar/data/{cik}/{accession_nodashes}/index.json
    index_url = (
        f"https://www.sec.gov/Archives/edgar/data/{cik_int}/"
        f"{acc_dashes}/index.json"
    )
    try:
        index_data = edgar_get(index_url).json()
    except Exception as e:
        print(f"    ⚠️  Index fetch failed for {accession}: {e}")
        return None

    # Find the infotable file - SEC stores it in "directory" → "item" list
    infotable_filename = None

    # Format 1: index.json with "directory" key (most common)
    items = index_data.get("directory", {}).get("item", [])
    if isinstance(items, dict):
        items = [items]
    for item in items:
        name = item.get("name", "").lower()
        if "informationtable" in name and name.endswith(".xml"):
            infotable_filename = item["name"]
            break

    # Format 2: flat "documents" list (older filings)
    if not infotable_filename:
        for doc in index_data.get("documents", []):
            name = doc.get("name", "").lower()
            if "informationtable" in name and name.endswith(".xml"):
                infotable_filename = doc["name"]
                break

    # Format 3: search by type
    if not infotable_filename:
        for item in items:
            t = item.get("type", "").lower()
            if "information table" in t or "13f" in t:
                if item.get("name", "").endswith(".xml"):
                    infotable_filename = item["name"]
                    break

    if not infotable_filename:
        print(f"    ⚠️  No infotable XML found in index for {accession}")
        print(f"    Index keys: {list(index_data.keys())}")
        return None

    xml_url = EDGAR_ARCHIVES_URL.format(
        cik_int=cik_int,
        accession_dashes=acc_dashes,
        filename=infotable_filename,
    )
    try:
        xml_text = edgar_get(xml_url).text
        return xml_text
    except Exception as e:
        print(f"    ⚠️  Could not download infotable XML: {e}")
        return None


# ── Step 3: Parse XML holdings ───────────────────────────────────────────────

# SEC uses two possible XML namespaces across filing history
_NS_OPTIONS = [
    {"ns": "com/xbrl/cd/mr/edgar/a-2010-09-30"},
    {"ns": ""},  # no namespace
]

def parse_infotable(xml_text: str) -> list[dict]:
    """
    Parses 13F infotable XML → list of holding dicts.

    Each dict contains:
      cusip, nameOfIssuer, titleOfClass, value (USD thousands),
      sshPrnamt (shares/principal amount), sshPrnamtType,
      putCall (PUT/CALL/None), investmentDiscretion, votingAuthority

    Handles both namespaced and non-namespaced SEC XML variants.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"    ⚠️  XML parse error: {e}")
        return []

    # Auto-detect namespace
    ns_uri = ""
    tag = root.tag
    if tag.startswith("{"):
        ns_uri = tag[1:tag.index("}")]

    ns = {"ns": ns_uri} if ns_uri else {}
    prefix = "ns:" if ns_uri else ""

    holdings = []
    for entry in root.findall(f".//{prefix}infoTable", ns):
        def _t(tag_name: str) -> str:
            el = entry.find(f"{prefix}{tag_name}", ns)
            return el.text.strip() if el is not None and el.text else ""

        try:
            value_raw = _t("value")
            shares_raw = _t("sshPrnamt")
            holding = {
                "cusip":               _t("cusip"),
                "nameOfIssuer":        _t("nameOfIssuer"),
                "titleOfClass":        _t("titleOfClass"),
                "value_usd_thousands": int(value_raw.replace(",", "")) if value_raw else 0,
                "shares":              int(shares_raw.replace(",", "")) if shares_raw else 0,
                "sshPrnamtType":       _t("sshPrnamtType"),
                "putCall":             _t("putCall") or None,
                "investmentDiscretion":_t("investmentDiscretion"),
            }
            if holding["cusip"]:  # skip malformed entries
                holdings.append(holding)
        except (ValueError, AttributeError) as e:
            print(f"    ⚠️  Skipping malformed holding entry: {e}")
            continue

    return holdings


# ── Step 4: CUSIP → Ticker mapping via OpenFIGI ───────────────────────────────

def map_cusips_to_tickers(cusips: list[str]) -> dict[str, str]:
    """
    Batch-maps CUSIP identifiers to ticker symbols using OpenFIGI (free, no key needed).

    R-02 Fix: Without this, Claude receives meaningless CUSIP strings.
    R-11 Fix: Uses ISIN/CUSIP at the instrument level, not the exchange level,
              which works for both ordinary shares and ADRs.

    Pre-condition:  cusips is a list of 9-character CUSIP strings
    Post-condition: returns dict {cusip: ticker}, missing ones are absent
    """
    mapping = {}
    if not cusips:
        return mapping

    # Deduplicate
    unique_cusips = list(set(cusips))

    # Batch requests (max 100 per request)
    for i in range(0, len(unique_cusips), OPENFIGI_BATCH):
        batch = unique_cusips[i:i + OPENFIGI_BATCH]
        payload = [{"idType": "ID_CUSIP", "idValue": c} for c in batch]

        try:
            resp = requests.post(
                OPENFIGI_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=20,
            )
            if resp.status_code == 429:
                print("  ⏳ OpenFIGI rate limit, sleeping 60s...")
                time.sleep(60)
                resp = requests.post(OPENFIGI_URL, json=payload, headers={"Content-Type": "application/json"}, timeout=20)

            results = resp.json()
            for cusip, result in zip(batch, results):
                if "data" in result and result["data"]:
                    # Prefer US equity ticker; fall back to first result
                    for figi_item in result["data"]:
                        ticker = figi_item.get("ticker", "")
                        exch   = figi_item.get("exchCode", "")
                        if exch in ("US", "UN", "UW", "UA"):  # US exchanges
                            mapping[cusip] = ticker
                            break
                    else:
                        mapping[cusip] = result["data"][0].get("ticker", "")
        except Exception as e:
            print(f"  ⚠️  OpenFIGI batch {i//OPENFIGI_BATCH + 1} failed: {e}")

        time.sleep(0.5)  # OpenFIGI rate limit is more lenient but be polite

    return mapping


# ── Step 5: Corporate action check (stock split detection) ────────────────────

def check_recent_splits(tickers: list[str]) -> dict[str, float]:
    """
    Returns a dict of {ticker: split_ratio} for any ticker that had a
    forward stock split in the past 120 days.

    R-01 Fix: Prevents false positive Delta signals after splits.

    Uses yfinance which is free and requires no API key.
    Returns empty dict if yfinance unavailable.
    """
    try:
        import yfinance as yf
        from datetime import timedelta
    except ImportError:
        print("  ⚠️  yfinance not installed, skipping split check")
        return {}

    splits = {}
    cutoff = date.today() - timedelta(days=120)

    for ticker in tickers:
        if not ticker:
            continue
        try:
            hist = yf.Ticker(ticker).splits
            if hist.empty:
                continue
            recent = hist[hist.index.date >= cutoff]  # type: ignore
            if not recent.empty:
                ratio = float(recent.iloc[-1])
                splits[ticker] = ratio
                print(f"  ⚠️  Split detected: {ticker} ratio {ratio}")
        except Exception:
            pass

    return splits


# ── Main orchestration ────────────────────────────────────────────────────────

def run():
    today_str = date.today().isoformat()
    output_path = DATA_DIR / f"{today_str}_raw_holdings.json"

    all_data = {}
    all_cusips = set()

    print(f"\n{'='*60}")
    print(f"SEC EDGAR Fetch – {today_str}")
    print(f"{'='*60}")

    for name, cik in FILERS.items():
        print(f"\n▶ {name} (CIK: {cik})")

        # 1. Get filing metadata
        filing_meta = get_latest_13f_filing(cik)
        if not filing_meta:
            all_data[name] = {"error": "no_filing", "cik": cik}
            continue

        print(f"  Filing: {filing_meta['form']} on {filing_meta['filingDate']}"
              + (" ⚠️ AMENDMENT" if filing_meta["isAmendment"] else ""))

        # 2. Download XML
        xml_text = download_infotable(filing_meta)
        if not xml_text:
            all_data[name] = {"error": "no_xml", "cik": cik, "meta": filing_meta}
            continue

        # 3. Parse holdings
        holdings = parse_infotable(xml_text)
        if not holdings:
            all_data[name] = {"error": "no_holdings_parsed", "cik": cik, "meta": filing_meta}
            continue

        print(f"  ✅ {len(holdings)} positions parsed")

        # Collect CUSIPs for batch mapping
        for h in holdings:
            if h["cusip"]:
                all_cusips.add(h["cusip"])

        all_data[name] = {
            "cik":          cik,
            "meta":         filing_meta,
            "holdings":     holdings,
            "total_value":  sum(h["value_usd_thousands"] for h in holdings),
            "fetched_at":   datetime.utcnow().isoformat(),
        }

    # 4. CUSIP → Ticker mapping (batch, once for all filers)
    print(f"\n🔍 Mapping {len(all_cusips)} unique CUSIPs to tickers via OpenFIGI...")
    cusip_to_ticker = map_cusips_to_tickers(list(all_cusips))
    print(f"   Mapped: {len(cusip_to_ticker)} / {len(all_cusips)}")

    # Enrich holdings with ticker
    all_tickers = set()
    for name, filer_data in all_data.items():
        if "holdings" not in filer_data:
            continue
        for h in filer_data["holdings"]:
            h["ticker"] = cusip_to_ticker.get(h["cusip"], "")
            if h["ticker"]:
                all_tickers.add(h["ticker"])

    # 5. Stock split check
    print(f"\n🔀 Checking for recent stock splits on {len(all_tickers)} tickers...")
    splits = check_recent_splits(list(all_tickers))
    if splits:
        print(f"   ⚠️  Splits detected: {splits}")
    else:
        print("   ✅ No recent splits detected")

    # Save everything
    output = {
        "date":             today_str,
        "cusip_to_ticker":  cusip_to_ticker,
        "recent_splits":    splits,
        "filers":           all_data,
    }

    with open(output_path, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print(f"\n✅ Raw holdings saved to {output_path}")
    print(f"   Filers with data: {sum(1 for v in all_data.values() if 'holdings' in v)} / {len(FILERS)}")

    # Fail loudly if too many filers missing (> 30%)
    missing = sum(1 for v in all_data.values() if "holdings" not in v)
    if missing > len(FILERS) * 0.3:
        raise RuntimeError(f"Too many filers missing data: {missing}/{len(FILERS)}")


if __name__ == "__main__":
    run()
