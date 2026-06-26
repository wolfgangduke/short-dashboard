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

Result is emailed (color-coded HTML) to EMAIL_TO via Gmail SMTP.

Data sources: FMP (constituents) + yfinance (closes) + FRED + Wikipedia
fallback. No MCP.

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
from email.mime.multipart import MIMEMultipart

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
FMP_SP500_URL = "https://financialmodelingprep.com/stable/sp500-constituent"
FRED_OBS_URL = "https://api.stlouisfed.org/fred/series/observations"

BREADTH_THRESHOLD = 50.0   # percent
LOOKBACK_CLOSES = 3        # number of recent closes / prints to test


# --------------------------------------------------------------------------
# Breadth gate
# --------------------------------------------------------------------------
def _tickers_from_fmp():
    """Primary source: Financial Modeling Prep S&P 500 constituents."""
    key = os.environ.get("FMP_API_KEY", "").strip()
    if not key:
        return None
    r = requests.get(FMP_SP500_URL, params={"apikey": key}, timeout=60)
    r.raise_for_status()
    data = r.json()
    syms = [d["symbol"].strip() for d in data if d.get("symbol")]
    return syms or None


def _tickers_from_wikipedia():
    """Fallback source: light-scrape the constituent list from Wikipedia."""
    resp = requests.get(WIKI_SP500_URL, headers={"User-Agent": USER_AGENT}, timeout=60)
    resp.raise_for_status()
    df = pd.read_html(StringIO(resp.text))[0]
    return df["Symbol"].astype(str).str.strip().tolist()


def get_sp500_tickers():
    """S&P 500 tickers from FMP, falling back to Wikipedia on any failure."""
    try:
        syms = _tickers_from_fmp()
    except Exception as e:
        print(f"FMP constituent fetch failed ({e}); using Wikipedia.", file=sys.stderr)
        syms = None
    if not syms:
        syms = _tickers_from_wikipedia()
    # yfinance uses '-' where some sources use '.' (e.g. BRK.B -> BRK-B)
    return [s.replace(".", "-") for s in syms]


def compute_breadth(tickers):
    """% of constituents above their own 200-day MA, indexed by date."""
    data = yf.download(
        tickers, period="1y", interval="1d",
        auto_adjust=True, progress=False, threads=True,
    )
    close = data["Close"]
    if isinstance(close, pd.Series):
        close = close.to_frame()
    ma200 = close.rolling(window=200, min_periods=200).mean()
    above = close > ma200
    valid = ma200.notna()
    pct_above = (above & valid).sum(axis=1) / valid.sum(axis=1) * 100.0
    return pct_above.dropna()


def breadth_gate():
    pct = compute_breadth(get_sp500_tickers())
    last = pct.tail(LOOKBACK_CLOSES)
    passed = len(last) == LOOKBACK_CLOSES and bool((last < BREADTH_THRESHOLD).all())
    rows = [(idx.date().isoformat(), f"{val:.1f}%") for idx, val in last.items()]
    return passed, rows


# --------------------------------------------------------------------------
# Liquidity gate
# --------------------------------------------------------------------------
def fred_series(series_id, api_key):
    params = {
        "series_id": series_id, "api_key": api_key,
        "file_type": "json", "observation_start": "2022-01-01",
    }
    r = requests.get(FRED_OBS_URL, params=params, timeout=60)
    r.raise_for_status()
    idx, vals = [], []
    for o in r.json()["observations"]:
        if o["value"] in (".", "", None):
            continue
        idx.append(pd.to_datetime(o["date"]))
        vals.append(float(o["value"]))
    return pd.Series(vals, index=idx, name=series_id).sort_index()


def liquidity_gate(api_key):
    walcl = fred_series("WALCL", api_key)        # Fed balance sheet ($M)
    tga = fred_series("WTREGEN", api_key)        # Treasury General Account ($M)
    rrp = fred_series("RRPONTSYD", api_key)      # Overnight reverse repo ($B)
    df = pd.concat([walcl, tga, rrp], axis=1).sort_index().ffill().dropna()
    # Everything in $millions: RRP is reported in $billions, so *1000.
    net = (df["WALCL"] - df["WTREGEN"] - df["RRPONTSYD"] * 1000.0).dropna()
    last = net.tail(LOOKBACK_CLOSES)
    passed = len(last) == LOOKBACK_CLOSES and bool(
        all(last.iloc[i] < last.iloc[i - 1] for i in range(1, len(last)))
    )
    rows = [(idx.date().isoformat(), f"${val/1e6:.3f}T") for idx, val in last.items()]
    return passed, rows


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def _badge(passed):
    color = "#1e7e34" if passed else "#c0392b"
    text = "PASS" if passed else "FAIL"
    return (f'<span style="background:{color};color:#ffffff;padding:3px 12px;'
            f'border-radius:12px;font-size:12px;font-weight:bold;'
            f'letter-spacing:.5px;">{text}</span>')


def _rows_html(rows):
    cells = "".join(
        f'<tr><td style="padding:2px 14px 2px 0;color:#666;'
        f'font-family:monospace;">{d}</td>'
        f'<td style="padding:2px 0;font-family:monospace;'
        f'font-weight:bold;color:#222;">{v}</td></tr>'
        for d, v in rows
    )
    return f'<table style="border-collapse:collapse;margin-top:6px;">{cells}</table>'


def build_html(today, signal, b_pass, b_rows, l_pass, l_rows):
    sig_color = "#c0392b" if signal == "INITIATE SHORT" else "#b8860b"
    sig_sub = ("Both gates passed." if signal == "INITIATE SHORT"
               else "At least one gate not met.")

    def card(title, subtitle, passed, rows):
        border = "#1e7e34" if passed else "#c0392b"
        return f"""
        <div style="border:1px solid #e2e2e2;border-left:5px solid {border};
                    border-radius:6px;padding:14px 16px;margin:12px 0;
                    background:#fafafa;">
          <div style="display:flex;justify-content:space-between;">
            <div style="font-size:15px;font-weight:bold;color:#222;">{title}</div>
            <div>{_badge(passed)}</div>
          </div>
          <div style="font-size:12px;color:#777;margin-top:2px;">{subtitle}</div>
          {_rows_html(rows)}
        </div>"""

    return f"""<html><body style="margin:0;padding:0;background:#f0f0f0;">
    <div style="max-width:560px;margin:0 auto;padding:20px;
                font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;
                background:#ffffff;">
      <div style="font-size:12px;letter-spacing:2px;color:#999;
                  text-transform:uppercase;">MacroSage</div>
      <div style="font-size:22px;font-weight:bold;color:#1a1a1a;margin:2px 0 2px;">
        SHORT Dashboard</div>
      <div style="font-size:13px;color:#888;">{today}</div>

      <div style="background:{sig_color};border-radius:8px;padding:18px 20px;
                  margin:18px 0;text-align:center;">
        <div style="font-size:11px;letter-spacing:2px;color:#ffffffcc;
                    text-transform:uppercase;">Signal</div>
        <div style="font-size:28px;font-weight:bold;color:#ffffff;
                    margin-top:4px;">{signal}</div>
        <div style="font-size:12px;color:#ffffffcc;margin-top:4px;">{sig_sub}</div>
      </div>

      {card("Gate 1 &middot; Breadth",
            f"% of S&amp;P 500 above 200-DMA &mdash; need &lt;{BREADTH_THRESHOLD:.0f}% "
            f"for last {LOOKBACK_CLOSES} closes", b_pass, b_rows)}
      {card("Gate 2 &middot; Liquidity",
            "Net liquidity (WALCL &minus; WTREGEN &minus; RRP) &mdash; "
            f"need declining for last {LOOKBACK_CLOSES} prints", l_pass, l_rows)}

      <div style="font-size:11px;color:#aaa;margin-top:18px;
                  border-top:1px solid #eee;padding-top:10px;">
        Rule: INITIATE SHORT only when BOTH gates PASS, else WATCHING.<br>
        Sources: S&amp;P 500 constituents (Wikipedia) + closes (yfinance),
        net liquidity (FRED).
      </div>
    </div></body></html>"""


def build_text(today, signal, b_pass, b_rows, l_pass, l_rows):
    def block(title, passed, rows):
        lines = "\n".join(f"    {d}: {v}" for d, v in rows)
        return f"{title}\n  Status : {'PASS' if passed else 'FAIL'}\n  Last {LOOKBACK_CLOSES}:\n{lines}"
    return (
        f"MacroSage SHORT dashboard -- {today}\n"
        f"{'=' * 50}\n\n"
        f"SIGNAL: {signal}\n\n"
        f"{block('Gate 1 -- Breadth (% S&P 500 > 200-DMA, need <%.0f%%)' % BREADTH_THRESHOLD, b_pass, b_rows)}\n\n"
        f"{block('Gate 2 -- Liquidity (net liq declining)', l_pass, l_rows)}\n\n"
        f"Rule: INITIATE SHORT only when BOTH gates PASS, else WATCHING.\n"
    )


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------
def send_email(subject, text_body, html_body):
    sender = os.environ["EMAIL_ADDRESS"].strip()
    password = "".join(os.environ["EMAIL_PASSWORD"].split())
    recipients = [a.strip() for a in os.environ["EMAIL_TO"].split(",") if a.strip()]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

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

    b_pass, b_rows = breadth_gate()
    l_pass, l_rows = liquidity_gate(fred_key)
    signal = "INITIATE SHORT" if (b_pass and l_pass) else "WATCHING"

    text_body = build_text(today, signal, b_pass, b_rows, l_pass, l_rows)
    html_body = build_html(today, signal, b_pass, b_rows, l_pass, l_rows)

    print(text_body)
    send_email(f"MacroSage SHORT {signal} -- {today}", text_body, html_body)


if __name__ == "__main__":
    sys.exit(main())
