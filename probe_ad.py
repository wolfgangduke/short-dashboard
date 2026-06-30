#!/usr/bin/env python3
"""probe_ad.py - test A/D data sources from GitHub Actions environment.
Run via: python probe_ad.py
DELETE this file before merging to main.
"""
import urllib.request
import urllib.error
import json
import sys
import os

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
FMP = os.environ.get("FMP_API_KEY", "")

def get(url, headers=None, timeout=15):
    hdrs = dict(UA)
    if headers:
        hdrs.update(headers)
    try:
        req = urllib.request.Request(url, headers=hdrs)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")[:2000]
    except urllib.error.HTTPError as e:
        return e.code, str(e)
    except Exception as e:
        return 0, str(e)

print("=== A/D SOURCE PROBE ===")

# 1. Stooq CSV ^ADVN
print("\n--- 1. Stooq ^ADVN CSV ---")
code, body = get("https://stooq.com/q/d/l/?s=%5Eadvn&i=d")
print(f"HTTP {code}")
if code == 200:
    lines = body.strip().splitlines()
    print(f"rows={len(lines)}, header={lines[0] if lines else 'none'}")
    print("last 5:", lines[-5:] if len(lines) >= 5 else lines)

# 2. Stooq CSV ^DECN
print("\n--- 2. Stooq ^DECN CSV ---")
code2, body2 = get("https://stooq.com/q/d/l/?s=%5Edecn&i=d")
print(f"HTTP {code2}")
if code2 == 200:
    lines2 = body2.strip().splitlines()
    print(f"rows={len(lines2)}, header={lines2[0] if lines2 else 'none'}")
    print("last 5:", lines2[-5:] if len(lines2) >= 5 else lines2)

# 3. FMP v3 historical (legacy, different path)
if FMP:
    print("\n--- 3. FMP v3 historical-price-full ^ADVN ---")
    url = f"https://financialmodelingprep.com/api/v3/historical-price-full/%5EADVN?timeseries=60&apikey={FMP}"
    code3, body3 = get(url)
    print(f"HTTP {code3}")
    if code3 == 200:
        try:
            d = json.loads(body3[:5000] if len(body3) > 5000 else body3)
            hist = d.get("historical", [])
            print(f"rows={len(hist)}")
            if hist:
                print("last 5:", [{"date": r["date"], "close": r.get("close")} for r in hist[:5]])
        except Exception as e:
            print(f"parse error: {e}, body[:200]={body3[:200]}")
    
    print("\n--- 4. FMP stable historical-price-full ^ADVN (current, wrong?) ---")
    url4 = f"https://financialmodelingprep.com/stable/historical-price-full/%5EADVN?from=2026-01-01&apikey={FMP}"
    code4, body4 = get(url4)
    print(f"HTTP {code4}, body[:300]={body4[:300]}")

    print("\n--- 5. FMP stable quote ^ADVN ---")
    url5 = f"https://financialmodelingprep.com/stable/quote?symbol=%5EADVN&apikey={FMP}"
    code5, body5 = get(url5)
    print(f"HTTP {code5}, body[:300]={body5[:300]}")

# 6. WSJ markets NYSE A/D
print("\n--- 6. WSJ markets breadth ---")
wsj_url = "https://www.wsj.com/market-data/stocks/marketsdiary"
code6, body6 = get(wsj_url)
print(f"HTTP {code6}, len={len(body6)}, body[:300]={body6[:300]}")

# 7. NYSE Daily Summary (nyse.com)
print("\n--- 7. NYSE Daily Summary ---")
nyse_url = "https://www.nyse.com/api/quotes/internal/market-summary"
code7, body7 = get(nyse_url)
print(f"HTTP {code7}, body[:500]={body7[:500]}")

# 8. Yahoo Finance v8 chart (may 404 for index symbols)
print("\n--- 8. Yahoo v8 chart ^ADVN ---")
yf_url = "https://query1.finance.yahoo.com/v8/finance/chart/%5EADVN?range=3mo&interval=1d"
code8, body8 = get(yf_url)
print(f"HTTP {code8}, body[:300]={body8[:300]}")

print("\n=== PROBE COMPLETE ===")
