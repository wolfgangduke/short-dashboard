#!/usr/bin/env python3
# run_fmp.py - Run this to test FMP data and dump macro snapshot to fmp_snapshot.json
# Usage: python run_fmp.py

import os
import json

# Load .env if present (pure stdlib - no python-dotenv needed)
env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(env_path):
    for line in open(env_path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from fmp_client import get_macro_snapshot, FMPError

def main():
    print("Fetching macro snapshot from FMP...")
    try:
        snapshot = get_macro_snapshot()
    except FMPError as e:
        print(f"ERROR: {e}")
        return

    with open("fmp_snapshot.json", "w") as f:
        json.dump(snapshot, f, indent=2)
    print("Saved to fmp_snapshot.json")

    q = snapshot.get("spy_quote", {})
    v = snapshot.get("vix_quote", {})
    rt = (snapshot.get("treasury_rates") or [{}])[0]
    fed = (snapshot.get("fed_funds_rate") or [{}])[0]
    cpi = (snapshot.get("cpi") or [{}])[0]

    print("\n=== CRASH SIGNAL SUMMARY ===")
    print(f"SPY:  ${q.get('price','?')} ({q.get('changePercentage','?')}% today)")
    print(f"VIX:  {v.get('price','?')} ")
    y10 = float(rt.get("year10") or 0)
    y2  = float(rt.get("year2")  or 0)
    spread = round(y10 - y2, 3)
    print(f"Yield curve 10Y-2Y: {spread}% -- INVERTED" if spread < 0 else f"Yield curve 10Y-2Y: {spread}%")
    print(f"Fed Funds: {fed.get('value','?')}%")
    print(f"CPI: {cpi.get('value','?')} ({cpi.get('date','?')})")
    print("\nDone.")

if __name__ == "__main__":
    main()