# fmp_client.py
# Fetches macro data from Financial Modeling Prep (FMP) API.
# Uses only Python stdlib (urllib) - no third-party packages needed.
# Set FMP_API_KEY in your .env file or environment before running.

import os
import json
import urllib.request
import urllib.parse
import urllib.error
from typing import Optional

FMP_BASE = "https://financialmodelingprep.com/stable"


class FMPError(Exception):
    pass


def _key() -> str:
    k = os.environ.get("FMP_API_KEY", "").strip()
    if not k:
        raise FMPError("FMP_API_KEY not set. Add it to your .env file and load it before running.")
    return k


def fmp_get(endpoint: str, params: Optional[dict] = None) -> list | dict:
    """Call any FMP /stable endpoint. Returns parsed JSON."""
    p = dict(params or {})
    p["apikey"] = _key()
    url = f"{FMP_BASE}/{endpoint.lstrip('/')}?" + urllib.parse.urlencode(p)
    req = urllib.request.Request(url, headers={"User-Agent": "short-dashboard/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise FMPError("FMP: Invalid API key (401). Check FMP_API_KEY in your .env.")
        if e.code == 403:
            raise FMPError("FMP: Access denied (403). Check your plan allows this endpoint.")
        if e.code == 429:
            raise FMPError("FMP: Rate limit hit (429). Add a delay between requests.")
        raise FMPError(f"FMP HTTP error {e.code}: {e.reason}")
    except urllib.error.URLError as e:
        raise FMPError(f"FMP connection error: {e.reason}")


# ---------------------------------------------------------------------------
# Macro-focused helpers - aligned with SNP 500 crash alert use case
# ---------------------------------------------------------------------------

def get_economic_indicator(name: str) -> list:
    """Fetch an economic time series.
    Common names: GDP, CPI, inflationRate, federalFunds, unemploymentRate,
                  retailSales, consumerSentiment, durableGoods, initialJoblessClaims
    """
    return fmp_get("economic-indicators", {"name": name})


def get_treasury_rates() -> list:
    """Yield curve data - key crash signal."""
    return fmp_get("treasury-rates")


def get_sector_performance() -> list:
    """Sector rotation data."""
    return fmp_get("sector-performance")


def get_quote(symbol: str) -> dict:
    """Real-time quote for a ticker (e.g. 'SPY', 'QQQ', 'VIX')."""
    data = fmp_get("quote", {"symbol": symbol})
    return data[0] if data else {}


def get_market_risk_premium() -> list:
    return fmp_get("market-risk-premium")


def get_macro_snapshot() -> dict:
    """Pull all key crash-signal macro data in one call."""
    return {
        "gdp":             get_economic_indicator("GDP"),
        "cpi":             get_economic_indicator("CPI"),
        "inflation_rate":  get_economic_indicator("inflationRate"),
        "fed_funds_rate":  get_economic_indicator("federalFunds"),
        "unemployment":    get_economic_indicator("unemploymentRate"),
        "jobless_claims":  get_economic_indicator("initialJoblessClaims"),
        "retail_sales":    get_economic_indicator("retailSales"),
        "treasury_rates":  get_treasury_rates(),
        "spy_quote":       get_quote("SPY"),
        "vix_quote":       get_quote("VIX"),
        "sector_perf":     get_sector_performance(),
    }


if __name__ == "__main__":
    # Quick test - load .env manually if running directly
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        for line in open(env_path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

    print("Testing FMP connection...")
    try:
        spy = get_quote("SPY")
        print(f"SPY price: {spy.get('price')} | change: {spy.get('changePercentage')}%")
        vix = get_quote("VIX")
        print(f"VIX: {vix.get('price')}")
        rates = get_treasury_rates()
        if rates:
            r = rates[0]
            print(f"Treasury 10Y: {r.get('year10')}% | 2Y: {r.get('year2')}% | spread: {round((r.get('year10',0) or 0) - (r.get('year2',0) or 0), 3)}")
        print("FMP connection OK")
    except FMPError as e:
        print(f"ERROR: {e}")
