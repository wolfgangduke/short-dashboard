#!/usr/bin/env python3
"""
MacroSage SHORT dashboard.

Two gates, both must pass to fire INITIATE SHORT (else WATCHING):

  1. Breadth gate  -- % of S&P 500 constituents above their own 200-day
     moving average must be BELOW 50% for each of the last 3 closes.
     ($S5TH is not available via yfinance, so the constituent list is
     light-scraped from Wikipedia and the breadth figure is computed
     from per-name closes pulled via yfinance.)

  2. Liquidity gate -- net liquidity = WALCL - WTREGEN - RRPONTSYD
     (Fed balance sheet minus Treasury General Account minus Reverse
     Repo), pulled from the FRED API, must be DECLINING across the
     last 3 prints.

Result is emailed to EMAIL_TO via Gmail SMTP.

Data sources: yfinance + FRED + a light Wikipedia scrape. No MCP.

Required environment variables:
  FRED_API_KEY    FRED API key
  EMAIL_ADDRESS   Gmail address used to send (SMTP login)
  EMAIL_PASSWORD  Gmail App Password (not the normal password)
  EMAIL_TO        comma-separated recipient list
"""

import os
import sys
import smtplib
import datetime as dt
from io import StringIO
from email.mime.text import MIMEText

import requests
import pandas as pd
import yfinance as yf

# A browser-like User-Agent: Wikipedia returns HTTP 403 to the bare
# urllib agent that pandas.read_html uses by default.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

WIKI_SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
FRED_OBS_URL = "https://api.stlouisfed.org/fred/series/observations"

BREADTH_THRESHOLD = 50.0   # percent
LOOKBACK_CLOSES = 3        # number of recent closes / prints to test


# --------------------------------------------------------------------------
# Breadth gate
# --------------------------------------------------------------------------
def get_sp500_tickers():
    """Light-scrape the S&P 500 constituent list from Wikipedia."""
    resp = requests.get(WIKI_SP500_URL, headers={"User-Agent": USER_AGENT}, timeout=60)
    resp.raise_for_status()
    tables = pd.read_html(StringIO(resp.text))
    df = tables[0]
    tickers = df["Symbol"].astype(str).str.strip().tolist()
    # yfinance uses '-' where Wikipedia uses '.' (e.g. BRK.B -> BRK-B)
    return [t.replace(".", "-") for t in tickers]


def compute_breadth(tickers):
    """
    Return a pandas Series (indexed by date) giving the % of constituents
    trading above their own 200-day moving average, for recent dates.
    """
    data = yf.download(
        tickers,
        period="1y",
        interval="1d",
        auto_adjust=True,
        progress=False,
        threads=True,
    )
    close = data["Close"]
    if isinstance(close, pd.Series):          # single-ticker edge case
        close = close.to_frame()

    ma200 = close.rolling(window=200, min_periods=200).mean()
    above = close > ma200                      # bool DataFrame

    # Only count names that actually have a 200-DMA on a given day.
    valid = ma200.notna()
    pct_above = (above & valid).sum(axis=1) / valid.sum(axis=1) * 100.0
    return pct_above.dropna()


def breadth_gate():
    tickers = get_sp500_tickers()
    pct = compute_breadth(tickers)
    last = pct.tail(LOOKBACK_CLOSES)
    passed = len(last) == LOOKBACK_CLOSES and bool((last < BREADTH_THRESHOLD).all())
    detail = ", ".join(
        f"{idx.date()}: {val:.1f}%" for idx, val in last.items()
    )
    return passed, last, detail


# --------------------------------------------------------------------------
# Liquidity gate
# --------------------------------------------------------------------------
def fred_series(series_id, api_key):
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "observation_start": "2022-01-01",
    }
    r = requests.get(FRED_OBS_URL, params=params, timeout=60)
    r.raise_for_status()
    obs = r.json()["observations"]
    idx, vals = [], []
    for o in obs:
        if o["value"] in (".", "", None):
            continue
        idx.append(pd.to_datetime(o["date"]))
        vals.append(float(o["value"]))
    return pd.Series(vals, index=idx, name=series_id).sort_index()


def liquidity_gate(api_key):
    walcl = fred_series("WALCL", api_key)        # Fed balance sheet
    tga = fred_series("WTREGEN", api_key)        # Treasury General Account
    rrp = fred_series("RRPONTSYD", api_key)      # Overnight reverse repo

    df = pd.concat([walcl, tga, rrp], axis=1).sort_index().ffill().dropna()
    # WALCL is in $millions; WTREGEN ($billions) and RRPONTSYD ($billions)
    # are converted to $millions so the subtraction is on a common unit.
    net = df["WALCL"] - df["WTREGEN"] * 1000.0 - df["RRPONTSYD"] * 1000.0
    net = net.dropna()

    last = net.tail(LOOKBACK_CLOSES)
    passed = len(last) == LOOKBACK_CLOSES and bool(
        all(last.iloc[i] < last.iloc[i - 1] for i in range(1, len(last)))
    )
    detail = ", ".join(
        f"{idx.date()}: ${val/1e6:.3f}T" for idx, val in last.items()
    )
    return passed, last, detail


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------
def send_email(subject, body):
    sender = os.environ["EMAIL_ADDRESS"].strip()
    password = os.environ["EMAIL_PASSWORD"].strip()
    recipients = [a.strip() for a in os.environ["EMAIL_TO"].split(",") if a.strip()]

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(sender, password)
        server.sendmail(sender, recipients, msg.as_string())


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    today = dt.date.today().isoformat()
    fred_key = os.environ["FRED_API_KEY"].strip()

    breadth_pass, breadth_vals, breadth_detail = breadth_gate()
    liq_pass, liq_vals, liq_detail = liquidity_gate(fred_key)

    signal = "INITIATE SHORT" if (breadth_pass and liq_pass) else "WATCHING"

    body = (
        f"MacroSage SHORT dashboard -- {today}\n"
        f"==================================================\n\n"
        f"SIGNAL: {signal}\n\n"
        f"Gate 1 -- Breadth (% S&P 500 > 200-DMA, need <{BREADTH_THRESHOLD:.0f}% "
        f"for last {LOOKBACK_CLOSES} closes)\n"
        f"  Status : {'PASS' if breadth_pass else 'FAIL'}\n"
        f"  Last {LOOKBACK_CLOSES}: {breadth_detail}\n\n"
        f"Gate 2 -- Liquidity (net liq = WALCL - WTREGEN - RRPONTSYD, "
        f"need declining for last {LOOKBACK_CLOSES} prints)\n"
        f"  Status : {'PASS' if liq_pass else 'FAIL'}\n"
        f"  Last {LOOKBACK_CLOSES}: {liq_detail}\n\n"
        f"Rule: INITIATE SHORT only when BOTH gates PASS, else WATCHING.\n"
    )

    print(body)
    send_email(f"MacroSage SHORT {signal} -- {today}", body)


if __name__ == "__main__":
    sys.exit(main())
